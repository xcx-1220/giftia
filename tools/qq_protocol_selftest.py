#!/usr/bin/env python
"""用一个本地假 QQ 网关，把适配器协议客户端一侧完整跑一遍——不需要任何凭据。

    python tools/qq_protocol_selftest.py

覆盖：op10 hello → op2 identify（token 格式 / intents / shard）→ op0 READY →
op1 心跳带 seq → op11 ack → op0 事件派发 → 她 → OpenAPI 回复
（路径 / msg_type / content / msg_id / msg_seq）→ 事件重复推送去重 →
群聊 @ 标记剥离 → QQ_OWNER_OPENID 隔离 → SIGTERM 优雅收尾并整理记忆。

唯一没被覆盖的，只剩"远程对端真的换成腾讯的服务器"这件事。
"""

from __future__ import annotations

import dataclasses
import json
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

import adapters.qq_official as adapter  # noqa: E402


def recording_send(calls: list, *, fail: bool = False, partial: str = ""):
    """假的发送实现：记录调用，并按要求返回 SendResult。

    新接口要求 send() 回报"哪几段真的送达了"——测试桩也得跟上，
    否则调用方拿不到结果。（这个接口本身就是为了修
    "发送失败被吞掉、她却以为自己说过"这个 bug。）
    """

    def _send(kind, target, text, msg_id):
        calls.append((kind, target, text))
        if fail:
            return adapter.SendResult(sent=[], failed=[(text, "测试：整条发送失败")])
        if partial:
            return adapter.SendResult(sent=[partial], failed=[(text, "测试：后半段失败")])
        return adapter.SendResult(sent=[text])

    return _send

from giftia import brain as brain_mod  # noqa: E402
from giftia.config import load_settings as _raw_load_settings  # noqa: E402
from giftia.store import Store  # noqa: E402
from ws_testserver import accept_handshake, make_listener, read_frame, send_frame  # noqa: E402


def load_settings(*args, **kwargs):  # noqa: F811
    """测试专用：**关掉群聊聚批**，让"事件 → 回复"是同步的，断言才好写。

    聚批（等 wake_delay 秒把一段时间里的话一起处理）本身单独测，
    见第 9 节；这里关心的是接线对不对。
    """
    base = _raw_load_settings(*args, **kwargs)
    return dataclasses.replace(
        base, runtime=dataclasses.replace(base.runtime, wake_delay_seconds=0)
    )

OP_TEXT = 0x1
OP_CLOSE = 0x8
# 注意：QQ 的心跳是**应用层**的——一个 opcode=1 的文本帧，内容是 {"op":1,"d":seq}，
# 和 WebSocket 协议自身的 ping（opcode 0x9）完全是两回事。别搞混。
QQ_OP_HEARTBEAT = 1
QQ_OP_HEARTBEAT_ACK = 11

PASS, FAIL = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  → {detail}" if detail else ""))


def wait_for(predicate, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


# ── 假的 QQ 网关 ──────────────────────────────────────────────────
class FakeGateway(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.listener = make_listener()
        self.port = self.listener.getsockname()[1]
        self.ready = threading.Event()
        self.identify: dict | None = None
        self.heartbeats: list[dict] = []
        self.error: str = ""

    def _event(self, seq: int, event: str, data: dict) -> bytes:
        return json.dumps({"op": 0, "s": seq, "t": event, "d": data}).encode()

    def run(self) -> None:
        conn = None
        try:
            conn = accept_handshake(self.listener)
            self.ready.set()

            # 1) 连接成功 → op10 hello
            send_frame(conn, OP_TEXT, json.dumps(
                {"op": 10, "d": {"heartbeat_interval": 500}}
            ).encode())

            # 2) 客户端应发 op2 identify
            opcode, payload, _ = read_frame(conn)
            self.identify = json.loads(payload)
            if self.identify.get("op") != 2:
                self.error = f"期望 op2，收到 {self.identify.get('op')}"
                return

            # 3) 鉴权通过 → READY
            send_frame(conn, OP_TEXT, self._event(1, "READY", {
                "version": 1,
                "session_id": "FAKE_SESSION",
                "user": {"id": "1", "username": "测试机", "bot": True},
                "shard": [0, 1],
            }))

            # 4) 事件流：单聊 / 同一 id 重复推送 / 群聊@ / 陌生人来信
            for blob in (
                self._event(2, "C2C_MESSAGE_CREATE", {
                    "id": "MSG-1",
                    "author": {"user_openid": "OWNER", "username": "搭档"},
                    "content": "在吗？今天加班到很晚，回来路上下了点雨",
                    "message_type": 0,
                }),
                self._event(3, "C2C_MESSAGE_CREATE", {   # 平台可能重复推送同一条
                    "id": "MSG-1",
                    "author": {"user_openid": "OWNER"},
                    "content": "在吗？今天加班到很晚，回来路上下了点雨",
                    "message_type": 0,
                }),
                self._event(4, "GROUP_AT_MESSAGE_CREATE", {
                    "id": "MSG-2",
                    "author": {"user_openid": "OWNER"},
                    "group_openid": "GROUP-1",
                    "content": "<@!123456> 我下周三要考驾照，有点紧张",
                    "message_type": 0,
                }),
                self._event(5, "C2C_MESSAGE_CREATE", {
                    "id": "MSG-3",
                    "author": {"user_openid": "OTHER"},
                    "content": "你好呀，你是谁",
                    "message_type": 0,
                }),
                self._event(6, "GROUP_AT_MESSAGE_CREATE", {   # 群里陌生人 @ 她
                    "id": "MSG-4",
                    "author": {"user_openid": "OTHER"},
                    "group_openid": "GROUP-1",
                    "content": "<@!999999> 你也会理我吗",
                    "message_type": 0,
                }),
            ):
                send_frame(conn, OP_TEXT, blob)

            # 5) 之后持续应答心跳，直到客户端断开
            while True:
                opcode, payload, _ = read_frame(conn)
                if opcode == OP_CLOSE:
                    break
                if opcode != OP_TEXT:
                    continue
                try:
                    msg = json.loads(payload)
                except ValueError:
                    continue
                if msg.get("op") == QQ_OP_HEARTBEAT:
                    self.heartbeats.append(msg)
                    send_frame(
                        conn, OP_TEXT,
                        json.dumps({"op": QQ_OP_HEARTBEAT_ACK}).encode(),
                    )
        except ConnectionError:
            # 客户端断开是**正常收尾**，不是网关出错。
            # （以前这里一概记成 error，Windows 上偶发 ConnectionAborted
            #   会让"假网关没有报错"这条随机失败——测试偶尔假报错就不可信了。）
            pass
        except Exception as exc:
            if not self.error:
                self.error = f"{type(exc).__name__}: {exc}"
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def main() -> int:
    print("QQ 适配器协议自测（本地假网关，无凭据、不联网）\n")

    tmp = Path(tempfile.mkdtemp(prefix="giftia-qq-proto-"))
    settings = dataclasses.replace(load_settings(force_mock=True), root=tmp)

    gateway = FakeGateway()
    gateway.start()
    gateway.ready.wait(5)

    # OpenAPI 调用全部换成假的，只记录，不真的发出去
    api_calls: list[tuple[str, str, dict]] = []

    def fake_request(method, path, payload=None, *, auth=True):
        api_calls.append((method, path, payload or {}))
        if path.endswith("getAppAccessToken"):
            return {"access_token": "FAKE_TOKEN", "expires_in": "7200"}
        if path == "/gateway":
            return {"url": f"ws://127.0.0.1:{gateway.port}/"}
        return {"id": "SENT-MSG", "timestamp": "2026-10-07T22:00:00+08:00"}

    bot = adapter.QQBot(
        "FAKE_APPID", "FAKE_SECRET",
        owner_openid="OWNER",          # 只服务 OWNER，别人叫她不进记忆
        api_base="https://api.bot.qq.com",
        settings=settings,
    )
    bot._request = fake_request  # type: ignore[method-assign]

    # ── 信号接线（零风险地验证：handler 真的被换掉了）──────────
    bot.install_signal_handlers()
    check("SIGTERM 已接到 request_stop（systemd 停止服务时走这条路）",
          signal.getsignal(signal.SIGTERM) == bot.request_stop)
    check("SIGINT 已接到 request_stop", signal.getsignal(signal.SIGINT) == bot.request_stop)

    def checker() -> None:
        """等事件跑完，再请求停止。

        刻意不碰数据库：SQLite 连接是主线程创建的，跨线程访问会直接抛
        ProgrammingError。所有库内断言都放到 bot.run() 返回之后再读。
        """
        try:
            # 三条回复：MSG-1（单聊）、MSG-2（群聊）、MSG-3（陌生人 → 挡回）
            wait_for(lambda: len([c for c in api_calls if c[1].endswith("/messages")]) >= 3)
            time.sleep(1.5)  # 留出时间发心跳，并确认没有多余的第 4 条回复
        finally:
            bot.request_stop()

    checker_thread = threading.Thread(target=checker, daemon=True)
    checker_thread.start()

    bot.run()               # 主线程跑循环（信号处理器必须在主线程安装）
    checker_thread.join(5)

    # 运行结束后再读数据库（此时 shutdown_default 已经把记忆整理好并关闭了连接）
    results: dict[str, object] = {}
    store = Store(settings.db_path)
    try:
        rows = store.all_messages()
        results["sends"] = [c for c in api_calls if c[1].endswith("/messages")]
        results["heartbeats"] = list(gateway.heartbeats)
        results["session_id"] = bot.session_id
        results["bot_name"] = bot.bot_name
        results["msg_count"] = len(rows)
        results["sessions"] = {str(r["session_id"]) for r in rows}
        results["speakers"] = [str(r.get("speaker_name") or "") for r in rows]
        results["memories"] = len(store.all_memories())
        results["assistant_text"] = next(
            (r["content"] for r in rows if r["role"] == "assistant"), ""
        )
        results["user_texts"] = [r["content"] for r in rows if r["role"] == "user"]
    finally:
        store.close()

    # ══ 断言 ═══════════════════════════════════════════════════
    ident = gateway.identify or {}
    d = ident.get("d", {}) if ident else {}
    print("\n1. 握手与鉴权")
    check("收到了 op2 identify", ident.get("op") == 2, str(ident)[:120])
    check("token 格式为 'QQBot {access_token}'", d.get("token") == "QQBot FAKE_TOKEN",
          str(d.get("token")))
    check("intents = GROUP_AND_C2C_EVENT (1<<25)",
          d.get("intents") == adapter.INTENT_GROUP_AND_C2C, str(d.get("intents")))
    check("shard = [0, 1]（不分片）", d.get("shard") == [0, 1], str(d.get("shard")))
    check("鉴权走了 /app/getAppAccessToken",
          any(c[1].endswith("getAppAccessToken") for c in api_calls))
    check("网关地址来自 GET /gateway", any(c[1] == "/gateway" for c in api_calls))
    check("READY 里记录了 session_id", results.get("session_id") == "FAKE_SESSION",
          str(results.get("session_id")))
    check("READY 里记录了机器人名字", results.get("bot_name") == "测试机",
          str(results.get("bot_name")))
    check("假网关没有报错", not gateway.error, gateway.error)

    print("\n2. 心跳与 seq")
    beats = results.get("heartbeats") or []
    check("服务端至少收到一次心跳", len(beats) >= 1, f"{len(beats)} 次")
    check("心跳 op 正确", all(b.get("op") == 1 for b in beats), str(beats[:2]))
    check("心跳带着最新的 seq", any(isinstance(b.get("d"), int) and b["d"] >= 2 for b in beats),
          str([b.get("d") for b in beats]))

    print("\n3. 事件派发与回复")
    sends = results.get("sends") or []
    c2c = [c for c in sends if "/v2/users/OWNER/messages" in c[1]]
    group = [c for c in sends if "/v2/groups/GROUP-1/messages" in c[1]]
    other = [c for c in sends if "/v2/users/OTHER/messages" in c[1]]
    check("单聊回复发到了 /v2/users/OWNER/messages", len(c2c) == 1, str([c[1] for c in sends]))
    check("群聊回复发到了 /v2/groups/GROUP-1/messages", len(group) == 2, str([c[1] for c in sends]))
    if c2c:
        body = c2c[0][2]
        check("msg_type = 0（纯文本）", body.get("msg_type") == 0, str(body))
        check("带上了被动回复的 msg_id", body.get("msg_id") == "MSG-1", str(body))
        check("带上了 msg_seq（平台按它去重）", body.get("msg_seq") == 1, str(body))
        check("回复内容非空", bool(str(body.get("content", "")).strip()), str(body))
        check("发出去的就是她说的那句（与库里一致）",
              body.get("content") == results.get("assistant_text"),
              f"{body.get('content')!r} vs {results.get('assistant_text')!r}")
    check("重复推送的那条事件没有导致重复回复（去重生效）", len(c2c) == 1,
          f"收到 {len(c2c)} 条回复")
    check("全流程一共发出 4 条消息（搭档单聊 + 搭档群聊 + 陌生人单聊挡回 + 陌生人群聊客套）",
          len(sends) == 4, str([c[1] for c in sends]))
    check("群里陌生人 @ 她 → 她会以「客人」身份回一句（默认策略）",
          len([c for c in sends if "GROUP-1" in c[1]]) == 2,
          str([c[1] for c in sends]))

    print("\n4. 群聊 @ 标记与陌生人隔离")
    user_texts = results.get("user_texts") or []
    check("群里的 <@!123456> 已被剥掉，她才不会对着符号说话",
          "我下周三要考驾照，有点紧张" in user_texts, str(user_texts))
    check("群友说的话记在他自己的会话里，没混进搭档的会话",
          any("-g" in s for s in (results.get("sessions") or set()))
          and all(
              "-m-" in s or "-g" in s
              for s in (results.get("sessions") or set())
          ),
          str(results.get("sessions")))
    check("陌生人被挡回，且挡回话术是戏内的",
          len(other) == 1 and str(other[0][2].get("content", "")).startswith("……"),
          str(other))

    print("\n4b. 群里还有别人时的两种策略")
    # group_stranger_reply = false → 装作没看见（安静）
    quiet_settings = dataclasses.replace(
        load_settings(force_mock=True),
        root=Path(tempfile.mkdtemp(prefix="giftia-quiet-")),
        runtime=dataclasses.replace(
            load_settings(force_mock=True).runtime, group_stranger_reply=False
        ),
    )
    brain_mod.reset_default()
    quiet = adapter.QQBot("X", "Y", owner_openid="", settings=quiet_settings)
    quiet_calls: list[tuple[str, str, str]] = []
    quiet.send = recording_send(quiet_calls)  # type: ignore[method-assign]
    quiet.handle_event(adapter.EVENT_C2C, {
        "id": "Q-1", "author": {"user_openid": "ME"}, "content": "你好",
    })  # 私聊自动认主
    quiet.handle_event(adapter.EVENT_GROUP_AT, {
        "id": "Q-2", "author": {"user_openid": "SOMEONE", "member_role": "member"},
        "group_openid": "G2", "content": "<@!1> 我叫阿伟",
    })
    check("group_stranger_reply = false → 群里别人 @ 她，她装作没看见",
          len(quiet_calls) == 1, str([c[2] for c in quiet_calls]))
    quiet_hub = brain_mod.get_default(quiet_settings)
    check("但名字还是被记下来了（她认得群里的人，只是不说话）",
          quiet_hub.known_people().get("SOMEONE") == "阿伟",
          str(quiet_hub.known_people()))

    # 默认（true）→ 以客人身份回答，且不写记忆
    brain_mod.reset_default()
    loud_settings = dataclasses.replace(
        load_settings(force_mock=True), root=Path(tempfile.mkdtemp(prefix="giftia-loud-"))
    )
    loud = adapter.QQBot("X", "Y", owner_openid="", settings=loud_settings)
    loud_calls: list[tuple[str, str, str]] = []
    loud.send = recording_send(loud_calls)  # type: ignore[method-assign]
    loud.handle_event(adapter.EVENT_C2C, {
        "id": "L-1", "author": {"user_openid": "ME"}, "content": "我叫小林",
    })
    loud_hub = brain_mod.get_default(loud_settings)
    before_private = len(
        loud_hub.store.active_memories(loud_hub.instance_no, view=[("private", "")])
    )
    loud.handle_event(adapter.EVENT_GROUP_AT, {
        "id": "L-2", "author": {"user_openid": "FRIEND", "member_role": "member"},
        "group_openid": "G3", "content": "<@!1> 你好呀，我叫阿伟",
    })
    check("默认策略下，群里别人 @ 她会得到回复",
          len(loud_calls) == 2 and "抱歉" not in loud_calls[1][2], str([c[2] for c in loud_calls]))
    check("客人说的话不会变成「你们之间」的私事",
          len(loud_hub.store.active_memories(loud_hub.instance_no, view=[("private", "")]))
          == before_private,
          str(before_private))
    check("她记住了客人的名字",
          loud_hub.known_people().get("FRIEND") == "阿伟",
          str(loud_hub.known_people()))
    check("最近活跃的群被记下来（她主动发群里要用）",
          loud_hub.last_group_id == "G3", repr(loud_hub.last_group_id))
    brain_mod.reset_default()

    print("\n5. 优雅退出（SIGTERM 路径）")
    mems = results.get("memories")
    check("退出时把未整理的对话存成了记忆（shutdown_default 生效）",
          isinstance(mems, int) and mems > 0, str(mems))
    check("退出后单例已释放", brain_mod.current() is None)

    print("\n6. 自动认主：不写 /bind 也能认人")
    brain_mod.reset_default()  # 前面的测试用过全局单例
    auto_settings = dataclasses.replace(
        load_settings(force_mock=True), root=Path(tempfile.mkdtemp(prefix="giftia-auto-"))
    )
    auto = adapter.QQBot("X", "Y", owner_openid="", settings=auto_settings)
    auto_calls: list[tuple[str, str, str]] = []
    auto.send = recording_send(auto_calls)  # type: ignore[method-assign]

    auto.handle_event(adapter.EVENT_C2C, {
        "id": "AUTO-1", "author": {"user_openid": "ME"}, "content": "你好",
    })
    auto_hub = brain_mod.get_default(auto_settings)
    check("私聊第一句话就自动成为搭档（不用 /bind）",
          auto_hub.handler_id == "ME", repr(auto_hub.handler_id))
    check("自动认主之后是正常回复，不是被挡回",
          len(auto_calls) == 1 and "抱歉" not in auto_calls[0][2], str(auto_calls))

    auto.handle_event(adapter.EVENT_C2C, {
        "id": "AUTO-2", "author": {"user_openid": "OTHER"}, "content": "你好呀",
    })
    check("认主之后别人会被挡回",
          len(auto_calls) == 2 and "抱歉" in auto_calls[1][2], str(auto_calls[-1:]))

    brain_mod.reset_default()
    group_settings = dataclasses.replace(
        load_settings(force_mock=True), root=Path(tempfile.mkdtemp(prefix="giftia-auto-g-"))
    )
    grp_auto = adapter.QQBot("X", "Y", owner_openid="", settings=group_settings)
    grp_calls: list[tuple[str, str, str]] = []
    grp_auto.send = recording_send(grp_calls)  # type: ignore[method-assign]
    grp_auto.handle_event(adapter.EVENT_GROUP_AT, {
        "id": "AUTO-3", "author": {"user_openid": "SOMEONE", "member_role": "member"},
        "group_openid": "G", "content": "<@!1> 你好",
    })
    grp_hub = brain_mod.get_default(group_settings)
    check("群里普通成员不会认主（防抢注）", grp_hub.handler_id == "", repr(grp_hub.handler_id))
    check("群里的普通成员会被以客人身份回应（默认策略）",
          len(grp_calls) == 1 and "抱歉" not in grp_calls[0][2], str(grp_calls))

    grp_auto.handle_event(adapter.EVENT_GROUP_AT, {
        "id": "AUTO-4", "author": {"user_openid": "OWNER-X", "member_role": "owner"},
        "group_openid": "G", "content": "<@!1> 你好",
    })
    check("群主 @ 她会自动认主（陌生人冒充不了群主）",
          grp_hub.handler_id == "OWNER-X", repr(grp_hub.handler_id))

    # 关掉 auto_bind 之后回到显式 /bind 的老行为
    brain_mod.reset_default()
    strict_settings = dataclasses.replace(
        load_settings(force_mock=True),
        root=Path(tempfile.mkdtemp(prefix="giftia-auto-off-")),
        runtime=dataclasses.replace(load_settings(force_mock=True).runtime, auto_bind=False),
    )
    strict = adapter.QQBot("X", "Y", owner_openid="", settings=strict_settings)
    strict_calls: list[tuple[str, str, str]] = []
    strict.send = recording_send(strict_calls)  # type: ignore[method-assign]
    strict.handle_event(adapter.EVENT_C2C, {
        "id": "AUTO-5", "author": {"user_openid": "ME"}, "content": "你好",
    })
    strict_hub = brain_mod.get_default(strict_settings)
    check("auto_bind = false 时不再自动认主", strict_hub.handler_id == "",
          repr(strict_hub.handler_id))
    check("关掉自动认主后仍然可以手动 /bind",
          strict.handle_command("c2c", "ME", "/bind", "ME", "AUTO-6"))
    check("手动 /bind 生效", strict_hub.handler_id == "ME", repr(strict_hub.handler_id))

    brain_mod.reset_default()

    print("\n7. 全量群消息：听得见每一句话，但只在被点名时才说话")
    full_settings = dataclasses.replace(
        load_settings(force_mock=True), root=Path(tempfile.mkdtemp(prefix="giftia-full-"))
    )
    full = adapter.QQBot("X", "Y", owner_openid="", settings=full_settings)
    full_calls: list[tuple[str, str, str]] = []
    full.send = recording_send(full_calls)  # type: ignore[method-assign]
    full.handle_event(adapter.EVENT_C2C, {
        "id": "F-1", "author": {"user_openid": "ME", "username": "小林"}, "content": "你好",
    })
    full_hub = brain_mod.get_default(full_settings)

    full.handle_event(adapter.EVENT_GROUP_MSG_RECEIVE, {
        "group_openid": "GROUP-X", "op_member_openid": "ME", "timestamp": 1,
    })
    check("群里打开「接收所有消息」时她会收到通知",
          int(full_hub.store.state_get("fullmode:GROUP-X", 0)) == 1)

    before_full = len(full_calls)
    full.handle_event(adapter.EVENT_GROUP_MESSAGE, {
        "id": "F-2",
        "author": {"user_openid": "F1", "username": "阿伟", "member_role": "member"},
        "group_openid": "GROUP-X",
        "content": "今天中午吃什么",
    })
    check("全量模式下，没点到她的话一个字都不回",
          len(full_calls) == before_full, str(full_calls[-1:]))
    log_rows = full_hub.store.messages_by_prefix(f"i{full_hub.instance_no}-g", 20)
    check("但那句话进了她的聊天记录（这就是「往上翻」的来源）",
          any("今天中午吃什么" in str(r["content"]) for r in log_rows),
          str([r["content"][:12] for r in log_rows]))
    check("聊天记录里标了是谁说的",
          any(str(r.get("speaker_name")) == "阿伟" for r in log_rows),
          str([r.get("speaker_name") for r in log_rows]))
    check("平台的昵称直接进了花名册（群里没人喜欢每次都被问「你是哪位」）",
          full_hub.known_people().get("F1") == "阿伟",
          str(full_hub.known_people()))

    full.handle_event(adapter.EVENT_GROUP_MESSAGE, {
        "id": "F-3",
        "author": {"user_openid": "F1", "username": "阿伟", "member_role": "member"},
        "group_openid": "GROUP-X",
        "content": "艾拉你说呢",
        "mentions": [{"id": "BOT", "bot": True, "username": "艾拉"}],
    })
    check("全量模式下，@ 她的消息她会回",
          len(full_calls) == before_full + 1, str(full_calls[-1:]))

    full.handle_event(adapter.EVENT_GROUP_MESSAGE, {
        "id": "F-4", "author": {"user_openid": "F2", "username": "小雨"},
        "group_openid": "GROUP-X", "content": "在吗",
    })
    n_before = len(full_calls)
    full.handle_event(adapter.EVENT_GROUP_MESSAGE, {
        "id": "F-4", "author": {"user_openid": "F2", "username": "小雨"},
        "group_openid": "GROUP-X", "content": "在吗",
        "mentions": [{"id": "BOT", "bot": True}],
    })
    check("先去重只旁听过、随后按 @ 补推 → 她仍然会回（只挡真重复）",
          len(full_calls) == n_before + 1, str(full_calls[-1:]))

    forwarded = adapter.extract_content({
        "content": "",
        "message_type": 102,
        "msg_elements": [
            {"content": "=== 消息 1 ===\n[消息内容] 今天的学习计划已完成\n\n=== 消息 2 ==="}
        ],
    })
    check("别人**转发聊天记录**给她时，她读得到里面的内容（message_type=102）",
          "学习计划已完成" in forwarded, forwarded[:40])

    quoted = adapter.extract_content({
        "content": "他说的这句",
        "message_type": 103,
        "msg_elements": [{"content": "被引用的原话"}],
    })
    check("引用消息时，引用内容也一起读得到（message_type=103）",
          "被引用的原话" in quoted and "他说的这句" in quoted, quoted)

    brain_mod.reset_default()

    print("\n8. 发送失败：她不该以为自己说过")
    fail_settings = dataclasses.replace(
        load_settings(force_mock=True), root=Path(tempfile.mkdtemp(prefix="giftia-fail-"))
    )
    fbot = adapter.QQBot("X", "Y", owner_openid="", settings=fail_settings)
    fcalls: list[tuple[str, str, str]] = []
    fbot.send = recording_send(fcalls)
    fbot.handle_event(adapter.EVENT_C2C, {
        "id": "S-1", "author": {"user_openid": "ME", "username": "小林"}, "content": "你好",
    })
    fhub = brain_mod.get_default(fail_settings)

    def assistant_count() -> int:
        return len([
            m for m in fhub.store.all_messages(fhub.instance_no, limit=80)
            if m["role"] == "assistant"
        ])

    check("发送成功时，她的回复正常留档", assistant_count() == 1, f"{assistant_count()} 条")

    fbot.send = recording_send(fcalls, fail=True)
    fbot.handle_event(adapter.EVENT_C2C, {
        "id": "S-2", "author": {"user_openid": "ME", "username": "小林"}, "content": "在吗",
    })
    check("★ 整条没送出去 → 那条回复不留在她的记忆里（她不会以为自己说过）",
          assistant_count() == 1, f"{assistant_count()} 条")
    diary = fhub.store.list_diary(limit=6)
    check("但会记进她的日志（发不出去也算发生过）",
          any("没能送出去" in str(r["content"]) for r in diary),
          str([str(r["title"]) for r in diary][:4]))

    fbot.send = recording_send(fcalls, partial="前半句")
    fbot.handle_event(adapter.EVENT_C2C, {
        "id": "S-3", "author": {"user_openid": "ME", "username": "小林"}, "content": "在忙吗",
    })
    last_assistant = [
        m for m in fhub.store.all_messages(fhub.instance_no, limit=80)
        if m["role"] == "assistant"
    ][-1]
    check("★ 只送出去一部分 → 她只记实际送达的那部分（和用户看到的一致）",
          str(last_assistant["content"]) == "前半句", str(last_assistant["content"])[:24])
    check("并且明确提示了这次不完整",
          any("[发送]" in n for n in fhub.notices), str(fhub.notices[-1:]))
    check("用户说的话不受影响，仍然完整留档",
          any(str(m["content"]) == "在忙吗"
              for m in fhub.store.all_messages(fhub.instance_no, limit=80)))

    brain_mod.reset_default()

    print("\n9. 聚批：她等你把话说完再回")
    bsettings = dataclasses.replace(
        load_settings(force_mock=True),
        root=Path(tempfile.mkdtemp(prefix="giftia-batch-")),
        runtime=dataclasses.replace(
            load_settings(force_mock=True).runtime, wake_delay_seconds=0.4
        ),
    )
    bbot = adapter.QQBot("X", "Y", owner_openid="ME", settings=bsettings)
    bcalls: list[tuple[str, str, str]] = []
    bbot.send = recording_send(bcalls)
    bbot.handle_event(adapter.EVENT_GROUP_AT, {
        "id": "B-1",
        "author": {"user_openid": "ME", "username": "小林", "member_role": "owner"},
        "group_openid": "G-B", "content": "<@!1> 在吗",
    })
    check("被 @ 之后先不急着回（等她把话说完）", bcalls == [], str(bcalls))
    check("消息进了待处理批次",
          len(bbot._batch.get("G-B", {}).get("items", [])) == 1,
          str(bbot._batch))
    deadline_before = bbot._batch["G-B"]["deadline"]
    bbot.handle_event(adapter.EVENT_GROUP_MESSAGE, {
        "id": "B-2", "author": {"user_openid": "ME", "username": "小林"},
        "group_openid": "G-B", "content": "顺便问一下明天有空吗",
    })
    check("★ 窗口内追加的话并入同一批（没 @ 也算，不然会被漏掉）",
          len(bbot._batch["G-B"]["items"]) == 2, str(bbot._batch["G-B"]["items"]))
    check("★ 而且倒计时**不重置**（滑动窗口会让她永远等下去）",
          bbot._batch["G-B"]["deadline"] == deadline_before)

    bbot._batch["G-B"]["deadline"] = 0.0
    bbot._flush_batches()
    check("到点后一次性回复一条", len(bcalls) == 1, str(bcalls))
    hub_b = brain_mod.get_default(bsettings)
    rows_b = hub_b.store.messages_by_prefix(f"i{hub_b.instance_no}-g", 20)
    check("这一批的每一句都落进了聊天记录（她上下文里看得到）",
          any("明天有空吗" in str(r["content"]) for r in rows_b)
          and any("在吗" in str(r["content"]) for r in rows_b),
          str([str(r["content"])[:12] for r in rows_b]))

    brain_mod.reset_default()

    print("\n10. 发送管道：限频与去重")
    osend = dataclasses.replace(
        load_settings(force_mock=True), root=Path(tempfile.mkdtemp(prefix="giftia-send-"))
    )
    obot = adapter.QQBot("X", "Y", owner_openid="ME", settings=osend)
    obot.dry_run = True
    key = "group:G-X"
    for _ in range(osend.runtime.send_max_per_minute):
        obot._note_send(key)
    check("超过每分钟上限就被拦下（防平台风控）",
          "每分钟" in obot._rate_limit_wait(key), obot._rate_limit_wait(key))
    check("没超限时畅通", obot._rate_limit_wait("group:G-Y") == "")

    ocalls: list = []
    obot._request = lambda *a, **kw: ocalls.append(a)  # type: ignore[method-assign]
    obot.dry_run = False
    obot._sent_recently["c2c:U-1"] = ("同一句话", time.time())
    dedup_result = obot.send("c2c", "U-1", "同一句话", None)
    check("★ 8 秒内发过同样的话就不再发（防重复回复）",
          ocalls == [] and dedup_result.delivered == "同一句话", str(ocalls))
    obot._request = lambda *a, **kw: ocalls.append(a)  # type: ignore[method-assign]
    obot._request = lambda *a, **kw: (_ for _ in ()).throw(adapter.QQError("测试：发送失败"))
    obot._sent_recently.clear()
    fail_result = obot.send("c2c", "U-2", "这句话发不出去", None)
    check("发送失败时如实上报（不再静默吞掉）",
          fail_result.ok is False and fail_result.delivered == ""
          and len(fail_result.failed) == 1,
          f"ok={fail_result.ok} delivered={fail_result.delivered!r}")

    brain_mod.reset_default()

    print("\n11. 自己插嘴的日上限")
    spk = dataclasses.replace(
        load_settings(force_mock=True),
        root=Path(tempfile.mkdtemp(prefix="giftia-speak-")),
        runtime=dataclasses.replace(
            load_settings(force_mock=True).runtime, response_tier=100, speak_max_per_day=3
        ),
    )
    spbot = adapter.QQBot("X", "Y", owner_openid="ME", settings=spk)
    allowed = [spbot._may_speak_up("G-S") for _ in range(5)]
    check("★ 自己插嘴有每日上限（3 档是按每条消息掷骰子的，不封顶就是刷屏）",
          allowed == [True, True, True, False, False], str(allowed))
    check("换个群是另一本账", spbot._may_speak_up("G-T") is True)
    check("被 @ 的不占这个额度（那是回应，不是插嘴）",
          spbot._may_speak_up("G-S") is False)

    brain_mod.reset_default()

    print("\n" + "═" * 52)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("═" * 52)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
