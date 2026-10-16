"""网站入口自测：口令 → 队列 → 主线程执行 → 回复，全链路。

不连 QQ、不连网络、不花 API 钱（MockLLM + 临时目录）。
"""

from __future__ import annotations

import dataclasses
import json
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
from giftia.llm import MockLLM  # noqa: E402
from giftia.webgate import WebGate, split_parts  # noqa: E402

PASS, FAIL = 0, 0
ROOT = Path(__file__).resolve().parent.parent


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
    hub.llm = MockLLM(settings.model)          # 离线，不花钱
    hub.set_handler_id("OWNER")
    hub.set_handler_name("椎名")

    secret = "correct-horse-battery"
    gate = WebGate(port=0, secret=secret, static_dir=ROOT / "web")
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
            box["r"] = _call(url, data=data, cookie=cookie)

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
    finally:
        gate.stop()
        shutdown_default()

    print("\n" + "═" * 52)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("═" * 52)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
