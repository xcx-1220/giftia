#!/usr/bin/env python
r"""Stream C 探针 1：把 giftia/ws.py（手写 RFC6455 客户端）往死里打。

本地假服务端 + 畸形帧，不联网、不用凭据。
运行： python .review\probes\pa_ws_probe.py
"""
from __future__ import annotations

import base64
import gc
import hashlib
import socket
import struct
import sys
import threading
import time
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from giftia.ws import (  # noqa: E402
    OP_CLOSE, OP_CONT, OP_PING, OP_PONG, OP_TEXT,
    WSClosed, WSError, WSTimeout, WebSocket,
)
from ws_testserver import make_listener, read_frame  # noqa: E402

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PASS, FAIL = 0, 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  -> {detail}" if detail else ""))


def raw_frame(conn: socket.socket, b1: int, payload: bytes = b"", *, b2_len: int | None = None) -> None:
    """完全按字节控制的一帧（服务端→客户端，不加掩码）。"""
    n = len(payload) if b2_len is None else b2_len
    head = bytearray([b1])
    if b2_len is None:
        if n < 126:
            head.append(n)
        elif n < 1 << 16:
            head.append(126)
            head += struct.pack("!H", n)
        else:
            head.append(127)
            head += struct.pack("!Q", n)
    else:  # 手工指定长度字段（用于畸形编码）
        head.append(126)
        head += struct.pack("!H", n)
    conn.sendall(bytes(head) + payload)


class Server(threading.Thread):
    """接受一个连接、按脚本行事。"""

    def __init__(self, script, *, handshake="ok") -> None:
        super().__init__(daemon=True)
        self.script = script
        self.handshake = handshake
        self.listener = make_listener()
        self.port = self.listener.getsockname()[1]
        self.ready = threading.Event()      # 客户端可以开始连了
        self.upgraded = threading.Event()   # 握手已回
        self.request_head = ""
        self.wire: list[tuple[int, bytes, bool]] = []   # 服务端收到的客户端帧
        self.notes: list[str] = []
        self.eof = None
        self.error = ""

    def run(self) -> None:
        conn = None
        try:
            self.ready.set()
            conn, _ = self.listener.accept()
            conn.settimeout(30)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            self.request_head = buf.split(b"\r\n\r\n")[0].decode("latin-1")
            key = ""
            for line in self.request_head.split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            good = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            if self.handshake == "ok":
                resp = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                        "Connection: Upgrade\r\n")
            elif self.handshake == "bad_accept":
                good = "AAAA" + good[4:]
                resp = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                        "Connection: Upgrade\r\n")
            elif self.handshake == "200_ok_accept":
                resp = "HTTP/1.1 200 OK\r\nContent-Length: 0\r\n"
            elif self.handshake == "no_upgrade_header":
                resp = "HTTP/1.1 101 Switching Protocols\r\n"
            else:
                resp = ""
            conn.sendall((resp + f"Sec-WebSocket-Accept: {good}\r\n\r\n").encode())
            self.upgraded.set()
            self.script(self, conn)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def drain(conn: socket.socket, seconds: float = 1.5) -> tuple[bytes, bool]:
    """读到超时为止；返回 (收到的字节, 是否读到 EOF)。EOF = 对端真的把 TCP 关了。"""
    conn.settimeout(seconds)
    out = b""
    while True:
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            return out, False
        except OSError:
            return out, True
        if not chunk:
            return out, True
        out += chunk


def client_frames(server: Server) -> list[tuple[int, bytes, bool]]:
    """把服务端已经读到的客户端帧排空（最多再读几帧）。"""
    return server.wire


# ══════════════════════════════════════════════════════════════════
def s_handshake_tamper(server: Server, conn: socket.socket) -> None:
    time.sleep(1.5)


def s_hold(server: Server, conn: socket.socket) -> None:
    drain(conn, 3.0)


def s_ping_fragmented(server: Server, conn: socket.socket) -> None:
    """控制帧违规：FIN=0 的 ping + 200 字节 payload（RFC 要求断开）。"""
    conn.sendall(bytes([0x09]) + bytes([126]) + struct.pack("!H", 200) + b"P" * 200)
    time.sleep(0.4)
    server.wire.append(read_frame(conn))          # 客户端回了什么
    raw_frame(conn, 0x80 | OP_TEXT, "AFTER".encode())
    time.sleep(0.4)


def s_flood_fragments(server: Server, conn: socket.socket) -> None:
    """100 个 4000 字节的续帧，单帧都没超 max_frame，但总量 400KB。"""
    for i in range(100):
        conn.sendall(bytes([0x00 if i else 0x01]) + bytes([126]) + struct.pack("!H", 4000) + b"x" * 4000)
    conn.sendall(bytes([0x80 | OP_CONT]) + bytes([126]) + struct.pack("!H", 10) + b"y" * 10)
    time.sleep(2.0)


_STALL = threading.Event()


def s_stall_midframe(server: Server, conn: socket.socket) -> None:
    """先发 2 字节帧头（长度=126），然后卡住 > self.timeout；之后把这一帧补齐，
    再发一条独立的完整 TEXT 帧——看客户端会不会把两条消息拼成一条。"""
    conn.sendall(bytes([0x80 | OP_TEXT, 126]))
    time.sleep(2.5)                     # 客户端 self.timeout=2.0
    conn.sendall(struct.pack("!H", 9) + b"REALDATA!")   # 补齐被卡住的那一帧
    time.sleep(0.3)
    raw_frame(conn, 0x80 | OP_TEXT, b"AFTER")           # 一条全新的、完整的消息
    time.sleep(2.0)


def s_close_then_wait(server: Server, conn: socket.socket) -> None:
    """对端主动 close(4009)，然后等客户端有没有真的把 TCP 关掉。"""
    raw_frame(conn, 0x80 | OP_CLOSE, struct.pack("!H", 4009) + b"expired")
    time.sleep(0.6)
    try:
        server.wire.append(read_frame(conn))    # 客户端回应的关闭帧
    except Exception as exc:
        server.notes.append(f"没读到回帧：{type(exc).__name__}")
    rest, eof = drain(conn, 2.0)                # eof=True = 对面 FIN 了
    server.eof = eof
    server.notes.append(f"客户端回帧={[(hex(o), p) for o, p, _ in server.wire]} 尾随字节={len(rest)}")


def s_utf8_split(server: Server, conn: socket.socket) -> None:
    """把一个多字节字符切开：① 切在两个分片之间 ② 切在两个 TCP 包里。"""
    payload_a = "你好".encode()          # 6 字节
    payload_b = "世界啊".encode()        # 9 字节
    conn.sendall(bytes([0x01, 3]) + payload_a[:3])          # TEXT, FIN=0, 前 3 字节
    time.sleep(0.2)
    conn.sendall(bytes([0x00, 3]) + payload_a[3:])          # CONT, FIN=0, 后 3 字节
    conn.sendall(bytes([0x80, 9]) + payload_b[:2])          # CONT, FIN=1, 9 字节
    time.sleep(0.2)
    conn.sendall(payload_b[2:])                             # 剩下的 7 字节晚一点到
    time.sleep(1.0)


def s_oversize(server: Server, conn: socket.socket) -> None:
    """64 位长度且最高位为 1（RFC 禁止），以及超过 max_frame 的帧。"""
    conn.sendall(bytes([0x82, 127]) + struct.pack("!Q", 0xFFFFFFFFFFFFFFFF))
    time.sleep(0.5)
    conn.sendall(bytes([0x82, 126]) + struct.pack("!H", 5000))   # 超 max_frame=1000
    time.sleep(0.5)


def s_invalid_utf8(server: Server, conn: socket.socket) -> None:
    raw_frame(conn, 0x80 | OP_TEXT, b"\xff\xfe\x80abc")
    time.sleep(1.0)


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    print("Stream C 探针 1：giftia/ws.py 协议层\n")

    # ── 1. 握手校验 ────────────────────────────────────────────
    print("1. 握手校验")
    s = Server(s_handshake_tamper, handshake="bad_accept")
    s.start(); s.ready.wait(3)
    ws = WebSocket(f"ws://127.0.0.1:{s.port}/websocket/", timeout=2)
    try:
        ws.connect(); check("Sec-WebSocket-Accept 不匹配时拒绝", False, "居然连上了")
    except WSError as exc:
        check("Sec-WebSocket-Accept 不匹配时拒绝", "Accept" in str(exc), str(exc))
    except Exception as exc:
        check("Sec-WebSocket-Accept 不匹配时拒绝", False, f"{type(exc).__name__}: {exc}")
    finally:
        try: ws.close()
        except Exception: pass
    s.join(4)

    s = Server(s_handshake_tamper, handshake="200_ok_accept")
    s.start(); s.ready.wait(3)
    ws = WebSocket(f"ws://127.0.0.1:{s.port}/", timeout=2)
    try:
        ws.connect(); check("HTTP 200（即使 accept 正确）被拒绝", False, "居然连上了")
    except WSError as exc:
        check("HTTP 200（即使 accept 正确）被拒绝", True, str(exc))
    except Exception as exc:
        check("HTTP 200（即使 accept 正确）被拒绝", False, f"{type(exc).__name__}: {exc}")
    finally:
        try: ws.close()
        except Exception: pass
    s.join(4)

    s = Server(s_handshake_tamper, handshake="no_upgrade_header")
    s.start(); s.ready.wait(3)
    ws = WebSocket(f"ws://127.0.0.1:{s.port}/", timeout=2)
    try:
        ws.connect()
        check("缺 Upgrade/Connection 响应头时仍接受（只查 accept）", True, "已接受=按实现")
    except Exception as exc:
        check("缺 Upgrade/Connection 响应头时仍接受（只查 accept）", False,
              f"{type(exc).__name__}: {exc}")
    finally:
        try: ws.close()
        except Exception: pass
    s.join(4)

    s = Server(s_handshake_tamper, handshake="ok")
    s.start(); s.ready.wait(3)
    ws = WebSocket(f"ws://127.0.0.1:{s.port}/", timeout=2)
    ws.connect()
    host_line = next((ln for ln in s.request_head.split("\r\n") if ln.lower().startswith("host:")), "")
    print(f"  [INFO] 非默认端口时客户端发出的 {host_line!r}（服务端端口 {s.port}）")
    ws.close(); s.join(4)

    # ── 2. 控制帧规则 ──────────────────────────────────────────
    print("\n2. 控制帧规则（RFC6455 §5.5：控制帧不得分片、payload ≤ 125）")
    s = Server(s_ping_fragmented)
    s.start(); s.ready.wait(3)
    ws = WebSocket(f"ws://127.0.0.1:{s.port}/", timeout=2)
    ws.connect()
    got = ""
    try:
        got = ws.recv_text()
    except Exception as exc:
        got = f"<{type(exc).__name__}: {exc}>"
    s.join(5)
    echo = [(op, len(p)) for op, p, _ in s.wire]
    print(f"  [INFO] 客户端把 200 字节的 FIN=0 ping 回显成：{echo}")
    check("违规控制帧（FIN=0 / 200 字节）被接受并回显 pong", echo == [(OP_PONG, 200)], str(echo))
    check("违规控制帧之后连接照常工作", got == "AFTER", repr(got))
    ws.close()

    # ── 3. 分片总量无上限 ──────────────────────────────────────
    print("\n3. 分片重组：单帧上限 vs 消息总量")
    s = Server(s_flood_fragments)
    s.start(); s.ready.wait(3)
    ws = WebSocket(f"ws://127.0.0.1:{s.port}/", timeout=5, max_frame=4096)
    ws.connect()
    try:
        text = ws.recv_text()
        check("100 个 4000 字节续帧（总 400KB）被完整重组", len(text) == 400_010,
              f"len={len(text)}")
        print(f"  [INFO] max_frame={ws.max_frame} 单帧上限；实际收到一条 {len(text)} 字节的消息")
    except Exception as exc:
        check("100 个 4000 字节续帧（总 400KB）被完整重组", False, f"{type(exc).__name__}: {exc}")
    ws.close(); s.join(4)

    # ── 4. 帧内超时 ────────────────────────────────────────────
    print("\n4. 半包超时：连接到底还可不可用")
    s = Server(s_stall_midframe)
    s.start(); s.ready.wait(3)
    ws = WebSocket(f"ws://127.0.0.1:{s.port}/", timeout=2.0)
    ws.connect()
    t0 = time.time()
    first = ""
    try:
        first = ws.recv_text(timeout=0.3)
        first = f"<返回 {first!r}>"
    except WSTimeout as exc:
        first = f"WSTimeout({exc})"
    except Exception as exc:
        first = f"{type(exc).__name__}({exc})"
    dt = time.time() - t0
    print(f"  [INFO] recv_text(timeout=0.3) 实际耗时 {dt:.2f}s，抛出 {first}")
    check("帧内超时按调用方给的 timeout 生效（不是 self.timeout）", dt < 1.0, f"{dt:.2f}s")
    check("帧读到一半超时时不抛 WSTimeout（连接已不可信）",
          "WSTimeout" not in first, first)

    second = ""
    try:
        second = ws.recv_text(timeout=2.0)
        second = f"返回 {second!r}"
    except WSTimeout as exc:
        second = f"WSTimeout({exc})"
    except Exception as exc:
        second = f"{type(exc).__name__}({exc})"
    print(f"  [INFO] 恢复后第一次 recv_text → {second}")
    check("超时后收到的两条独立消息不会被拼接错位",
          second == "返回 'REALDATA!'" or second == "返回 'AFTER'", second)
    ws.close(); s.join(5)

    # ── 5. 对端 close 之后 close() 是不是空操作 ────────────────
    print("\n5. 对端主动 close：本地 close() 有没有真的关掉 socket")
    s = Server(s_close_then_wait)
    s.start(); s.ready.wait(3)
    ws = WebSocket(f"ws://127.0.0.1:{s.port}/", timeout=2)
    ws.connect()
    try:
        ws.recv_text()
    except WSClosed as exc:
        print(f"  [INFO] 收到对端 close：{exc}")
    except Exception as exc:
        print(f"  [INFO] 收到其他异常：{type(exc).__name__}: {exc}")
    print(f"  [INFO] ws.closed = {ws.closed}")
    ws.close()
    fileno = ws.sock.fileno() if ws.sock is not None else None
    print(f"  [INFO] close() 之后 socket.fileno() = {fileno}（-1 才是真的关了）")
    time.sleep(0.8)
    print(f"  [INFO] 服务端观察到：{s.notes}")
    print(f"  [INFO] 服务端看到客户端 TCP 关闭（EOF）= {s.eof}")
    check("对端 close 之后调用 close() 会真的关闭 socket",
          fileno == -1 and s.eof is True, f"fileno={fileno} eof={s.eof}")
    check("close() 回应了关闭帧", any(op == OP_CLOSE for op, _, _ in s.wire), str(s.wire))
    sock_kept = ws.sock
    ws = None
    gc.collect()
    print(f"  [INFO] 丢掉 WebSocket 对象并 GC 之后，socket.fileno() = "
          f"{sock_kept.fileno() if sock_kept is not None else None}")
    s.join(4)

    # ── 6. 长度编码 / UTF-8 / 非法字节 ─────────────────────────
    print("\n6. 长度编码、UTF-8 跨包、非法字节")
    s = Server(s_oversize)
    s.start(); s.ready.wait(3)
    ws = WebSocket(f"ws://127.0.0.1:{s.port}/", timeout=2, max_frame=1000)
    ws.connect()
    try:
        ws.recv_text(); out = "没抛异常"
    except WSError as exc:
        out = f"WSError({exc})"
    except Exception as exc:
        out = f"{type(exc).__name__}: {exc}"
    check("最高位为 1 的 64 位长度 / 超 max_frame 的帧被拒绝", "帧过大" in out, out)
    ws.close(); s.join(4)

    s = Server(s_utf8_split)
    s.start(); s.ready.wait(3)
    ws = WebSocket(f"ws://127.0.0.1:{s.port}/", timeout=2)
    ws.connect()
    try:
        got = ws.recv_text()
    except Exception as exc:
        got = f"<{type(exc).__name__}: {exc}>"
    check("UTF-8 多字节字符跨分片 + 跨 TCP 包都不破", got == "你好世界啊", repr(got))
    ws.close(); s.join(4)

    s = Server(s_invalid_utf8)
    s.start(); s.ready.wait(3)
    ws = WebSocket(f"ws://127.0.0.1:{s.port}/", timeout=2)
    ws.connect()
    try:
        got = ws.recv_text()
        print(f"  [INFO] 非法 UTF-8 载荷 → {got!r}（0xfffe 被替换成什么）")
    except Exception as exc:
        print(f"  [INFO] 非法 UTF-8 载荷 → {type(exc).__name__}: {exc}")
    ws.close(); s.join(4)

    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
