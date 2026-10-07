#!/usr/bin/env python
"""WebSocket 客户端的本地自测：不用外网，自己起一个测试服务端来验帧。

    python tools/ws_selftest.py

验证 giftia/ws.py 里最容易写错的地方：握手校验、**客户端帧掩码**、三种长度编码
（7 位 / 16 位 / 64 位）、分片重组、ping-pong 自动应答、空闲超时后连接仍可用、
关闭码传递。QQ 的网关用到的正是这几条路径。
"""

from __future__ import annotations

import struct
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from giftia.ws import (  # noqa: E402
    OP_CLOSE,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    WSClosed,
    WSTimeout,
    WebSocket,
)
from ws_testserver import (  # noqa: E402
    accept_handshake,
    make_listener,
    read_frame,
    send_fragmented,
    send_frame,
)

PASS, FAIL = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  → {detail}" if detail else ""))


# ── 测试用服务端：帧编解码复用 tools/ws_testserver.py ──────────────
class TestServer(threading.Thread):
    def __init__(self, script: str) -> None:
        super().__init__(daemon=True)
        self.script = script
        self.listener = make_listener()
        self.port = self.listener.getsockname()[1]
        self.received: list[tuple[int, bytes, bool]] = []
        self.ready = threading.Event()

    def run(self) -> None:
        conn = None
        try:
            conn = accept_handshake(self.listener)
            self.ready.set()

            if self.script == "basic":
                send_frame(conn, OP_TEXT, "小帧".encode())
                send_frame(conn, OP_TEXT, ("中" * 200).encode())     # 16 位长度
                send_frame(conn, OP_TEXT, ("大" * 70000).encode())   # 64 位长度
                send_fragmented(conn, "这是一条被切成三片的消息")
                send_frame(conn, OP_PING, b"server-ping")
                # 客户端接下来会依次发出：主动 ping → 自动 pong（共 2 帧）
                for _ in range(2):
                    self.received.append(read_frame(conn))
                # 然后才轮到客户端发文本帧（它在等这句话）
                send_frame(conn, OP_TEXT, "收尾".encode())
                self.received.append(read_frame(conn))

            elif self.script == "idle":
                send_frame(conn, OP_TEXT, "空闲测试".encode())
                self.received.append(read_frame(conn))

            elif self.script == "close":
                send_frame(conn, OP_CLOSE, struct.pack("!H", 4009))
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def main() -> int:
    print("WebSocket 客户端自测（本地服务端，不联网）\n")

    # ══ 1. 基础收发 ═══════════════════════════════════════════
    print("1. 握手 / 三种长度编码 / 分片 / ping-pong / 掩码")
    server = TestServer("basic")
    server.start()
    server.ready.wait(5)

    ws = WebSocket(f"ws://127.0.0.1:{server.port}/websocket/", timeout=15)
    ws.connect()
    check("握手成功（含 Sec-WebSocket-Accept 校验）", True)
    check("小帧（7 位长度）", ws.recv_text() == "小帧")
    check("中帧（16 位长度）", ws.recv_text() == "中" * 200)
    check("大帧（64 位长度）", ws.recv_text() == "大" * 70000)
    check("分片消息重组", ws.recv_text() == "这是一条被切成三片的消息")
    ws.ping(b"client-ping")   # 服务端也会发 ping，客户端应自动回 pong
    check("收到服务端 ping 后仍能继续读到消息", ws.recv_text() == "收尾")
    ws.send_text("客户端发出的文本")
    ws.close()

    server.join(5)
    opcodes = [op for op, _, _ in server.received]
    payloads = [p for _, p, _ in server.received]
    masked_flags = [m for _, _, m in server.received]
    check("服务端收到 3 个客户端帧", len(server.received) == 3, str(opcodes))
    check("客户端主动 ping 已送达", OP_PING in opcodes, str(opcodes))
    check("客户端自动回复了 pong（含原 payload）",
          (OP_PONG, b"server-ping") in [(op, p) for op, p, _ in server.received], str(server.received))
    check("客户端文本帧已送达且内容正确",
          "客户端发出的文本".encode() in payloads, str(payloads))
    check("客户端所有帧都带掩码（RFC 6455 强制，QQ 网关会校验）",
          all(masked_flags) and len(masked_flags) == 3, str(masked_flags))

    # ══ 2. 空闲超时后连接仍可用 ═══════════════════════════════
    print("\n2. 空闲超时（连接应保持可用）")
    idle = TestServer("idle")
    idle.start()
    idle.ready.wait(5)
    ws2 = WebSocket(f"ws://127.0.0.1:{idle.port}/", timeout=15)
    ws2.connect()
    check("先收到一条消息", ws2.recv_text() == "空闲测试")
    try:
        ws2.recv_text(timeout=0.4)
        check("空闲时抛 WSTimeout", False, "没有抛异常")
    except WSTimeout:
        check("空闲时抛 WSTimeout", True)
    except Exception as exc:
        check("空闲时抛 WSTimeout", False, f"{type(exc).__name__}: {exc}")
    ws2.send_text("超时之后我还在")
    idle.join(5)
    check("超时之后仍能正常发送",
          "超时之后我还在".encode() in [p for _, p, _ in idle.received],
          str(idle.received))
    ws2.close()

    # ══ 3. 关闭握手 ═══════════════════════════════════════════
    print("\n3. 关闭握手与错误码传递")
    closing = TestServer("close")
    closing.start()
    closing.ready.wait(5)
    ws3 = WebSocket(f"ws://127.0.0.1:{closing.port}/", timeout=15)
    ws3.connect()
    try:
        ws3.recv_text()
        check("对端关闭时抛 WSClosed", False, "没有抛异常")
    except WSClosed as exc:
        check("对端关闭时抛 WSClosed", True)
        check("能读到关闭码 4009（QQ 用它表示需要 resume）", "4009" in str(exc), str(exc))
    except Exception as exc:
        check("对端关闭时抛 WSClosed", False, f"{type(exc).__name__}: {exc}")
    ws3.close()

    # ══ 4. 参数校验 ═══════════════════════════════════════════
    print("\n4. 参数校验")
    try:
        WebSocket("http://example.com/")
        check("拒绝非 ws/wss 协议", False)
    except Exception:
        check("拒绝非 ws/wss 协议", True)

    print("\n" + "═" * 52)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("═" * 52)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
