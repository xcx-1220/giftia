"""网站入口自测：口令 → 队列 → 主线程执行 → 回复，全链路。

不连 QQ、不连网络、不花 API 钱（MockLLM + 临时目录）。
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from giftia.brain import get_default, shutdown_default  # noqa: E402
from giftia.config import load_settings  # noqa: E402
from giftia.llm import ChatReply, MockLLM  # noqa: E402
from giftia.webgate import WebGate, split_parts  # noqa: E402

PASS, FAIL = 0, 0
ROOT = Path(__file__).resolve().parent.parent


class SpyLLM(MockLLM):
    """会把自己收到的提示词记下来的假模型——用来检查"她看到了什么"。"""

    is_mock = False

    def __init__(self, cfg):
        super().__init__(cfg)
        self.prompts: list[str] = []

    def chat(self, messages, **kw):
        self.prompts.append("\n".join(str(m.get("content", "")) for m in messages))
        return "……嗯。"

    def chat_with_tools(self, messages, *, tools=None, **kw):
        self.prompts.append("\n".join(str(m.get("content", "")) for m in messages))
        return ChatReply(text="……嗯。")

    def extract_memories(self, *a, **kw):
        return []

    def latest(self) -> str:
        return self.prompts[-1] if self.prompts else ""


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  → {detail}" if detail else ""))


def _call(url: str, *, data: dict | None = None, cookie: str = "") -> tuple[int, dict]:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, method="POST" if body else "GET")
    if body:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            blob = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype:
                return resp.status, json.loads(blob.decode("utf-8"))
            if "text/" in ctype or "javascript" in ctype or "json" in ctype:
                return resp.status, {"_raw": blob.decode("utf-8"), "_len": len(blob)}
            return resp.status, {"_binary": len(blob)}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"_raw": raw}


def main() -> int:
    print("═" * 52)
    print("网站入口自测（webgate）")
    print("═" * 52)

    print("\n1. 切分回复（前端要一段段冒出来）")
    parts = split_parts("我在的。\n你今天怎么样？")
    check("按标点和换行切成多段", len(parts) >= 2, str(parts))
    check("最终拼回去内容不丢",
          "".join(parts).replace(" ", "") == "我在的。你今天怎么样？".replace(" ", ""),
          str(parts))
    check("空回复给空列表", split_parts("") == [])
    check("切不出来就整段返回", split_parts("嗯") == ["嗯"])
    many = split_parts("一。二。三。四。五。六。")
    check("段数有上限（不刷屏）", len(many) <= 3, str(many))

    print("\n2. 起一个真的 HTTP 入口")
    root = Path(tempfile.mkdtemp(prefix="giftia-web-"))
    settings = dataclasses.replace(load_settings(force_mock=True), root=root)
    hub = get_default(settings)
    spy = SpyLLM(settings.model)               # 离线，不花钱，还会记提示词
    hub.llm = spy
    hub.set_handler_id("OWNER")
    hub.set_handler_name("椎名")

    secret = "correct-horse-battery"
    friend_pass = "let-my-friends-in"
    gate = WebGate(port=0, secret=secret, static_dir=ROOT / "web",
                   friend_pass=friend_pass, data_dir=root / 'web-data')
    # port=0 → 让系统分配一个空闲端口
    gate.port = 0
    check("没有口令就不启动（公网无口令 = 谁都能烧 token）",
          not WebGate(port=0, secret="", static_dir=ROOT / "web").enabled)
    if not gate.start():
        check("入口启动", False, "start() 返回 False")
        return 1
    port = gate._server.server_address[1]      # type: ignore[union-attr]
    base = f"http://127.0.0.1:{port}"
    print(f"   （监听在 {base}）")

    # ★ 真实拓扑：**主线程拥有她**（SQLite 连接是主线程建的），
    #   HTTP 请求从别的线程发进来。反过来的话会撞上
    #   "SQLite objects created in a thread can only be used in that same thread"。
    def call(url: str, *, data: dict | None = None, cookie: str = "") -> tuple[int, dict]:
        box: dict = {}

        def worker() -> None:
            try:
                box["r"] = _call(url, data=data, cookie=cookie)
            except BaseException as exc:      # 别让线程静默死掉，否则只看到 KeyError
                import traceback

                traceback.print_exc()
                box["r"] = (0, {"_worker_error": f"{type(exc).__name__}: {exc}"})

        th = threading.Thread(target=worker)
        th.start()
        while th.is_alive():
            gate.drain(settings)      # 主线程这边排空队列
            time.sleep(0.01)
        th.join()
        return box["r"]

    try:
        print("\n3. 口令门禁")
        code, _ = call(f"{base}/api/health")
        check("健康检查不需要登录", code == 200, str(code))
        code, payload = call(f"{base}/api/chat", data={"text": "在吗"})
        check("★ 没登录不能聊天", code == 401, f"{code} {payload}")
        code, payload = call(f"{base}/api/login", data={"pass": "wrong"})
        check("口令错了进不来", code == 401, f"{code} {payload}")
        code, payload = call(f"{base}/api/login", data={"pass": secret})
        check("口令对了能进来", code == 200, f"{code} {payload}")
        cookie = f"aila_auth={gate.token()}"
        check("cookie 不是明文口令", secret not in cookie, cookie)

        print("\n4. 全链路：HTTP → 队列 → 主线程 → 她")
        code, payload = call(f"{base}/api/chat", data={"text": "你好呀"}, cookie=cookie)
        check("★ 登录后能跟她说话", code == 200, f"{code} {payload}")
        check("回复非空", bool(str(payload.get("reply") or "").strip()), str(payload)[:80])
        check("回复带了分段（前端分段播）", isinstance(payload.get("parts"), list)
              and len(payload.get("parts") or []) >= 1, str(payload.get("parts")))

        print("\n5. 网站上的她 = QQ 上的她（同一份记忆）")
        rows = hub.store.all_messages(hub.instance_no, limit=20)
        print(f"   （诊断：库={settings.db_path.name} 实例={hub.instance_no} "
              f"消息数={len(rows)} 同一个 hub={get_default(settings) is hub}）")
        texts = [str(r["content"]) for r in rows]
        check("★ 网站说的话进了她的会话记录（和 QQ 共享）",
              any("你好呀" in t for t in texts), str(texts[-3:]))
        check("而且记在搭档会话里（不是访客分区）",
              any(str(r["session_id"]).endswith("-m-" + hub.clock.now().strftime("%Y%m%d"))
                  for r in rows if "你好呀" in str(r["content"])),
              str(rows[-1]["session_id"]) if rows else "")

        print("\n6. 静态文件（PWA 壳）")
        for path, must in (
            ("/", "<html"),
            ("/app.js", "addEventListener"),
            ("/style.css", "{"),
            ("/manifest.json", "艾拉"),
            ("/sw.js", "caches"),
            ("/icons/icon-192.png", None),
        ):
            code, payload = call(f"{base}{path}")
            body = payload.get("_raw", "") or json.dumps(payload, ensure_ascii=False)
            ok = code == 200 and (
                payload.get("_binary", 0) > 0 if must is None else must in body
            )
            check(f"{path} 能拿到", ok, f"{code} {str(payload)[:70]}")

        print("\n7. 边界")
        code, _ = call(f"{base}/../deploy/setup.sh")
        check("目录穿越被挡住", code in (404, 400), str(code))
        code, _ = call(f"{base}/api/chat", data={"text": "  "}, cookie=cookie)
        check("空消息不收", code == 400, str(code))
        gate.max_per_minute = 3
        gate._hits.clear()
        codes = [call(f"{base}/api/chat", data={"text": f"第{i}句"}, cookie=cookie)[0]
                 for i in range(6)]
        check("★ 说得太快会被限流（防烧 token）", 429 in codes, str(codes))
        print("\n8. 访客口令：朋友看得到什么 ★★")
        # 第 7 节把限流压到 3 了，这里必须恢复——否则后面全被 429 挡掉，
        # 断言会"因为没跑到"而假通过（踩过）
        gate.max_per_minute = 60
        gate._hits.clear()
        # 给搭档埋一条带暗号的私事
        MARK = "紫色河马七号"
        hub.store.add_memory(
            instance_no=hub.instance_no,
            created_ts=hub.clock.iso(), happened_ts=hub.clock.iso(),
            summary=f"椎名的私事暗号是{MARK}", importance=0.9, layer="core",
            about="椎名", scope="private", subject_id="",
        )
        code, owner_login = call(f"{base}/api/login", data={"pass": secret})
        check("搭档口令 → 角色 owner",
              code == 200 and owner_login.get("role") == "owner", str(owner_login))
        code, friend_login = call(f"{base}/api/login", data={"pass": friend_pass})
        check("★ 访客口令 → 角色 friend（不是 owner）",
              code == 200 and friend_login.get("role") == "friend", str(friend_login))
        check("访客拿了独立身份（分区分开的前提）", bool(gate.token("friend")))

        owner_cookie = f"aila_auth={gate.token('owner')}"
        friend_cookie = f"aila_auth={gate.token('friend')}; aila_visitor=friend-abc123"

        # 搭档问 → 提示词里**应该**有这条私事
        # ★ 问句里**不能**出现暗号本身，否则它必然出现在提示词里，
        #   断言就成了废话（踩过一次）
        call(f"{base}/api/chat", data={"text": "我的暗号是什么"}, cookie=owner_cookie)
        check("★ 搭档的提示词里能看到自己的私事",
              MARK in spy.latest(), spy.latest()[-200:])

        # 朋友问同一件事 → 提示词里**绝不能**有
        code, _ = call(f"{base}/api/chat", data={"text": "我的暗号是什么"}, cookie=friend_cookie)
        check("访客也能正常聊天", code == 200, str(code))
        prompt = spy.latest()
        check("★★ 访客的提示词里**检索不到搭档的私事**（这条是硬门槛）",
              MARK not in prompt, prompt[:300])

        print("\n8b. 推送：只有搭档能收，访客一律 403 ★★")
        # 服务端起一个假的 VAPID 密钥，才能测到订阅/读取/测试推送全链路
        from giftia.push import generate_keys

        priv, pub = generate_keys()
        os.environ["VAPID_PRIVATE_KEY"] = priv
        os.environ["VAPID_PUBLIC_KEY"] = pub
        os.environ["VAPID_SUBJECT"] = "mailto:test@example.com"
        gate.max_per_minute = 60
        gate._hits.clear()

        code, st = call(f"{base}/api/push/status", cookie=owner_cookie)
        check("★ 搭档能查推送状态", code == 200 and st.get("owner") is True,
              f"{code} {st}")
        check("状态里带了 VAPID 公钥（浏览器订阅要用）",
              len(str(st.get("public_key") or "")) > 80, str(st)[:80])

        code, payload = call(f"{base}/api/push/status", cookie=friend_cookie)
        check("★★ 访客查推送状态 → 403（她主动发消息绝不打扰访客）",
              code == 403, f"{code} {payload}")

        fake_sub = {"endpoint": "https://fcm.googleapis.com/fcm/send/fake-1",
                    "keys": {"p256dh": "x", "auth": "y"}}
        code, _ = call(f"{base}/api/push/subscribe", data=fake_sub, cookie=friend_cookie)
        check("★★ 访客想订阅推送 → 403（连订阅的机会都没有）", code == 403, str(code))
        check("访客没能写进订阅表", len(gate.push_subs) == 0, str(len(gate.push_subs)))

        code, sres = call(f"{base}/api/push/subscribe", data=fake_sub, cookie=owner_cookie)
        check("★ 搭档能订阅", code == 200 and sres.get("count") == 1, f"{code} {sres}")

        code, lastmsg = call(f"{base}/api/push/last", cookie=owner_cookie)
        check("搭档能取「最后一条消息」（SW 靠它弹通知）", code == 200, f"{code} {lastmsg}")
        code, _ = call(f"{base}/api/push/last", cookie=friend_cookie)
        check("访客取不到那条消息", code == 403, str(code))

        # 记住：推送内容不该泄漏到访客侧的提示词里（间接旁证）
        gate.push_subs.remove(fake_sub["endpoint"])

        print("\n9. 访客不留痕：不进你的会话、不动关系")
        rows = hub.store.all_messages(hub.instance_no, limit=40)
        friend_rows = [r for r in rows if "friend-abc123" in str(r.get("subject_id") or "")]
        check("★ 访客的话进了他自己的分区（不是你的会话）",
              all(str(r["session_id"]).startswith(f"i{hub.instance_no}-p")
                  for r in friend_rows) and bool(friend_rows),
              str([r["session_id"] for r in friend_rows][:2]))
        owner_rows = [r for r in rows
                      if str(r["session_id"]).startswith(f"i{hub.instance_no}-m")]
        check("访客的话没混进搭档会话",
              not any("friend-abc123" in str(r.get("subject_id") or "") for r in owner_rows))

        bond_before = hub.current_bond()
        mood_before = hub.current_mood()
        call(f"{base}/api/chat", data={"text": "陪我聊聊天吧"}, cookie=friend_cookie)
        check("★ 访客聊天不推进亲密度（关系只奖励搭档来了）",
              hub.current_bond().affinity == bond_before.affinity,
              f"{bond_before.affinity} → {hub.current_bond().affinity}")
        check("访客聊天不改她的心情",
              hub.current_mood().key == mood_before.key,
              f"{mood_before.key} → {hub.current_mood().key}")

        code, _ = call(f"{base}/api/login", data={"pass": "neither-one"})
        check("两套口令之外的都进不来", code == 401, str(code))

        print("\n10. 可恢复请求与身份隔离")
        gate._hits.clear()
        rid = 'quality-request-123456789'
        count_before = hub.store.count_messages()
        code, first = call(f"{base}/api/chat", data={'text': '这条只发送一次', 'request_id': rid}, cookie=owner_cookie)
        count_after = hub.store.count_messages()
        code2, second = call(f"{base}/api/chat", data={'text': '这条只发送一次', 'request_id': rid}, cookie=owner_cookie)
        check('相同编号返回相同结果', code == 200 and code2 == 200 and first == second)
        check('重试不重复写入对话', count_after == count_before + 2 and hub.store.count_messages() == count_after)
        code, result = call(f"{base}/api/chat/result?request_id={rid}", cookie=owner_cookie)
        check('超时后可查询已完成结果', code == 200 and result.get('status') == 'completed' and result.get('reply') == first.get('reply'))
        code, _ = call(f"{base}/api/chat/result?request_id={rid}", cookie=friend_cookie)
        check('访客不能猜编号读取搭档结果', code == 404)
        code, _ = call(f"{base}/api/chat/result?request_id={rid}")
        check('结果接口要求身份认证', code == 401)
        code, _ = call(f"{base}/api/chat", data={'text': '换一个内容', 'request_id': rid}, cookie=owner_cookie)
        check('同编号不能换内容', code == 409)
        code, _ = call(f"{base}/api/chat", data={'text': '编号太短', 'request_id': 'bad'}, cookie=owner_cookie)
        check('无效编号被拒绝', code == 400)
        code, session = call(f"{base}/api/session", cookie=owner_cookie)
        check('服务端认识搭档，不再重复初见介绍', code == 200 and session.get('known') and '初次见面' not in session.get('greeting', ''))
        restored = WebGate(port=0, secret=secret, static_dir=ROOT/'web', friend_pass=friend_pass, data_dir=root/'web-data')
        restored._owner_epoch = gate._owner_epoch
        persisted = restored.ledger.get(restored.principal('owner') + ':' + rid)
        check('请求结果落盘，入口重建后仍可读取', persisted and persisted.get('reply') == first.get('reply'))
        key = gate.principal('owner') + ':' + rid
        gate.ledger.update(key, status='running', reply='')
        interrupted = WebGate(port=0, secret=secret, static_dir=ROOT/'web', friend_pass=friend_pass, data_dir=root/'web-data')
        before_recover = hub.store.count_messages()
        interrupted.drain(settings)
        recovered = interrupted.ledger.get(key)
        check('回复已落库但HTTP未交付时，重启后能恢复结果', recovered and recovered.get('status') == 'completed' and recovered.get('reply') == first.get('reply'))
        check('重启恢复不会重新生成或重复写聊天', hub.store.count_messages() == before_recover)

    finally:
        gate.stop()
        shutdown_default()

    print("\n" + "═" * 52)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("═" * 52)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
