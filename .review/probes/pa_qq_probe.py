r"""Stream C 探针 2：adapters/qq_official.py 的协议/限流/去重路径。

用一个本地假 OpenAPI（http.server）+ 假 send 桩，不发任何真实请求、不用任何凭据。
运行： python .review\probes\pa_qq_probe.py
"""
from __future__ import annotations

import dataclasses
import json
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

import adapters.qq_official as adapter  # noqa: E402
from giftia import brain as brain_mod  # noqa: E402
from giftia.config import load_settings as raw_load_settings  # noqa: E402

TMPROOT = ROOT / ".review" / "tmp"
PASS, FAIL = 0, 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  -> {detail}" if detail else ""))


def new_root(name: str) -> Path:
    p = TMPROOT / f"protocol-{name}-{int(time.time() * 1000) % 100000}"
    p.mkdir(parents=True, exist_ok=True)   # 默认 mode，沙箱可访问
    return p


def settings_for(name: str, **over):
    base = raw_load_settings(force_mock=True)
    if over:
        base = dataclasses.replace(base, **over)
    return dataclasses.replace(base, root=new_root(name))


# ── 假 OpenAPI ────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    def _go(self):
        srv = self.server.owner  # type: ignore[attr-defined]
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = {"_raw": raw.decode("utf-8", "replace")}
        srv.requests.append({"method": self.command, "path": self.path, "body": body})

        mode = srv.mode
        for prefix, m in srv.mode_map.items():
            if prefix in self.path:
                mode = m
                break
        if mode == "html":
            payload, ctype = b"<html><body>502 Bad Gateway</body></html>", "text/html"
        elif mode == "list":
            payload, ctype = b"[1, 2, 3]", "application/json"
        else:
            if "getAppAccessToken" in self.path:
                data = {"access_token": "FAKE_TOKEN", "expires_in": "7200"}
            else:
                data = {"id": "SENT-MSG", "timestamp": "2026-10-10T08:00:00+08:00"}
            payload = json.dumps(data).encode()
            ctype = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_POST = _go
    do_GET = _go


class Api(threading.Thread):
    def __init__(self, mode: str = "json", mode_map: dict | None = None) -> None:
        super().__init__(daemon=True)
        self.mode = mode
        self.mode_map = mode_map or {}
        self.requests: list[dict] = []
        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.owner = self  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def run(self) -> None:
        self.httpd.serve_forever()

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
        except Exception:
            pass

    def sends(self, needle: str) -> list[dict]:
        return [r for r in self.requests if needle in r["path"]]


def make_bot(api: Api, name: str, **kw):
    brain_mod.reset_default()
    st = settings_for(name)
    bot = adapter.QQBot("FAKE_APPID", "FAKE_SECRET", owner_openid="OWNER",
                        api_base=api.base, settings=st, **kw)
    return bot


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    print("Stream C 探针 2：QQ 适配器\n")

    # ── 1. HTTP 200 但不是 JSON ─────────────────────────────────
    print("1. 业务返回 HTTP 200 + 非 JSON（代理页/网关页）")
    api = Api(mode_map={"/v2/": "html"})
    api.start()
    bot = make_bot(api, "html")
    out = ""
    try:
        res = bot.send("c2c", "U-1", "你好呀", None)
        out = f"正常返回 {res}"
    except Exception as exc:
        out = f"{type(exc).__name__}: {exc}"
    print(f"  [INFO] send() 抛出：{out}")
    check("_request 的契约是「失败一律 QQError」", out.startswith("QQError"), out)

    # 从事件入口走一遍：异常会不会穿透 handle_event
    escaped = ""
    try:
        bot.handle_event(adapter.EVENT_C2C, {
            "id": "HTML-1", "author": {"user_openid": "OWNER"}, "content": "在吗",
        })
        escaped = "没有异常"
    except Exception as exc:
        escaped = f"{type(exc).__name__}: {str(exc)[:60]}"
    print(f"  [INFO] 一条 c2c 事件走完 handle_event：{escaped}")
    check("非 JSON 响应不会把异常抛穿事件处理（会拆掉整个 WS 会话）",
          escaped == "没有异常", escaped)

    api.mode_map = {"/app/": "list"}
    out2 = ""
    try:
        tok = bot.access_token(force=True)
        out2 = f"拿到 token={tok!r}"
    except Exception as exc:
        out2 = f"{type(exc).__name__}: {exc}"
    print(f"  [INFO] 鉴权接口返回 JSON 数组时 access_token()：{out2}")
    check("鉴权接口返回非对象 JSON 时也是 QQError", out2.startswith("QQError"), out2)
    api.stop()

    # ── 2. 8 秒同文去重：合法回复被静默丢弃 ─────────────────────
    print("\n2. 8 秒同文去重（两条不同 msg_id 的合法回复）")
    api2 = Api()
    api2.start()
    bot2 = make_bot(api2, "dedup")
    bot2.dry_run = False
    r1 = bot2.send("c2c", "U-2", "嗯，我在。", "MSG-A")
    n_after_first = len(api2.sends("/v2/users/U-2/messages"))
    time.sleep(0.2)
    r2 = bot2.send("c2c", "U-2", "嗯，我在。", "MSG-B")   # 另一个 msg_id = 另一条被动回复
    n_after_second = len(api2.sends("/v2/users/U-2/messages"))
    print(f"  [INFO] 第一次 send：{n_after_first} 个 HTTP 请求，delivered={r1.delivered!r} ok={r1.ok}")
    print(f"  [INFO] 第二次 send（msg_id=MSG-B）：{n_after_second} 个 HTTP 请求，"
          f"delivered={r2.delivered!r} ok={r2.ok} failed={r2.failed}")
    check("两次合法回复都会真的发出去", n_after_second == 2, f"只发出 {n_after_second} 条")
    check("没发出去时 SendResult 不会谎报 delivered",
          not (n_after_second == 1 and r2.delivered), f"delivered={r2.delivered!r}")
    api2.stop()

    # ── 3. 一个 msg_id 的回复条数 vs 平台上限 ───────────────────
    print("\n3. 一个 msg_id 能回几条（平台：群 5 / 单聊 4）")
    api3 = Api()
    api3.start()
    bot3 = make_bot(api3, "seq")
    long_reply = "今天想跟你说的事情有点多。" * 130          # 约 1700 字
    pieces = adapter.chunk_text(long_reply)
    print(f"  [INFO] {len(long_reply)} 字 → chunk_text 切成 {len(pieces)} 段")
    bot3.send("c2c", "U-3", long_reply, "MSG-LONG")
    sent = api3.sends("/v2/users/U-3/messages")
    seqs = [r["body"].get("msg_seq") for r in sent]
    print(f"  [INFO] 单聊：同一 msg_id 发出 {len(sent)} 条，msg_seq={seqs}")
    bot3.send("c2c", "U-3", "[终端服务] 还有 72 小时。", "MSG-LONG")   # _respond 会再补一条系统通知
    sent2 = api3.sends("/v2/users/U-3/messages")
    print(f"  [INFO] 再加一条系统通知 → 同一 msg_id 共 {len(sent2)} 条，"
          f"msg_seq={[r['body'].get('msg_seq') for r in sent2]}")
    check("单聊里同一个 msg_id 的回复条数不超过 4（平台硬限）",
          len(sent2) <= 4, f"{len(sent2)} 条")
    api3.stop()

    # ── 4. 限频命中时是「等」还是「丢」──────────────────────────
    print("\n4. 限频命中：注释说「超了就等（不硬丢）」，实际呢")
    api4 = Api()
    api4.start()
    bot4 = make_bot(api4, "rate")
    bot4._send_times["c2c:U-4"] = [time.time()] * bot4.settings.runtime.send_max_per_minute
    r4 = bot4.send("c2c", "U-4", "这句能不能发出去", None)
    n4 = len(api4.sends("/v2/users/U-4/messages"))
    print(f"  [INFO] 限频表已满 → ok={r4.ok} failed={r4.failed} 实际 HTTP 请求数={n4}")
    check("限频命中时不丢消息（或至少如实上报为失败）",
          n4 == 1, f"被硬丢：ok={r4.ok}")
    api4.stop()

    # ── 5. 主动消息绕过发送限频 ─────────────────────────────────
    print("\n5. 主动消息有没有走 send 的三道闸（同限频）")
    api5 = Api()
    api5.start()
    st5 = settings_for("outreach", outreach=dataclasses.replace(
        raw_load_settings(force_mock=True).outreach, enabled=True))
    brain_mod.reset_default()
    bot5 = adapter.QQBot("FAKE_APPID", "FAKE_SECRET", owner_openid="OWNER",
                         api_base=api5.base, settings=st5)
    hub5 = brain_mod.get_default(st5)
    hub5.compose_outreach = lambda **kw: (types.SimpleNamespace(reason_label="测试"), "主动来找你了")
    adapter.can_send = lambda *a, **kw: True          # 只隔离「发送限频」这一层
    bot5._send_times["c2c:OWNER"] = [time.time()] * (bot5.settings.runtime.send_max_per_hour + 5)
    print(f"  [INFO] 发送限频表：每小时上限 {bot5.settings.runtime.send_max_per_hour}，"
          f"已塞 {len(bot5._send_times['c2c:OWNER'])} 条")
    bot5._maybe_outreach()
    n5 = len(api5.sends("/v2/users/OWNER/messages"))
    print(f"  [INFO] 主动消息实际 HTTP 请求数={n5}，body={api5.sends('/v2/users/OWNER/messages')}")
    check("主动消息也受 send_max_per_minute/hour 约束", n5 == 0, f"绕过了限频，仍发出 {n5} 条")
    api5.stop()

    # ── 6. 引用消息里的 poke 误判 ──────────────────────────────
    print("\n6. extract_content：msg_elements 里的 poke 判定")
    got_poke = adapter.extract_content({
        "content": "", "message_type": 103,
        "msg_elements": [{"content": "我最近在玩 Pokemon 卡牌，挺上头的"}],
    })
    got_poker = adapter.extract_content({
        "content": "", "message_type": 103,
        "msg_elements": [{"content": "他昨天 poker 赢了两百"}],
    })
    got_real = adapter.extract_content({
        "content": "", "message_type": 103,
        "msg_elements": [{"content": "被引用的原话"}],
    })
    print(f"  [INFO] 元素文本含 'Pokemon'  → {got_poke!r}")
    print(f"  [INFO] 元素文本含 'poker'    → {got_poker!r}")
    print(f"  [INFO] 普通引用文本          → {got_real!r}")
    check("poke 识别不会吃掉正常文本", "Pokemon" in got_poke, got_poke)

    # ── 7. 群里普通成员发 /bind ─────────────────────────────────
    print("\n7. 群里普通成员发 /bind（README：认人只有群主做得到）")
    brain_mod.reset_default()
    st7 = settings_for("bind")
    bot7 = adapter.QQBot("FAKE_APPID", "FAKE_SECRET", owner_openid="", settings=st7)
    calls: list[tuple] = []
    bot7.send = lambda kind, target, text, msg_id: (calls.append((kind, target, text)),
                                                    adapter.SendResult(sent=[text]))[1]
    hub7 = brain_mod.get_default(st7)
    bot7.handle_event(adapter.EVENT_GROUP_AT, {
        "id": "SQUAT-1", "author": {"user_openid": "ATTACKER", "member_role": "member"},
        "group_openid": "G-1", "content": "<@!1> /bind",
    })
    print(f"  [INFO] 群里一个 member 发 /bind 之后 handler_id = {hub7.handler_id!r}")
    print(f"  [INFO] 她的回复 = {(calls[-1][2] if calls else None)!r}")
    check("群里普通成员发 /bind 不会成为搭档", hub7.handler_id == "", repr(hub7.handler_id))

    bot7.handle_event(adapter.EVENT_C2C, {
        "id": "SQUAT-2", "author": {"user_openid": "REAL_OWNER"}, "content": "/bind",
    })
    print(f"  [INFO] 真搭档私聊发 /bind → {calls[-1][2]!r}（handler_id={hub7.handler_id!r}）")
    check("真搭档还能自己绑回来", hub7.handler_id == "REAL_OWNER", repr(hub7.handler_id))

    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
