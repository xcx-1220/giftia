"""临时：确认 ws.py 在"违规控制帧"与"半包超时"两个场景下的真实状态机。

问题：
 1. 违规控制帧抛 WSError 之后，连接是"已关闭/不可用"还是"还能继续读"？
 2. 帧内超时抛 WSClosed 之后，再次 recv_text 会不会立刻抛（而不是卡住）？
 3. 一个字节都没到 → WSTimeout，连接仍可用？
"""
from __future__ import annotations

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

from giftia.ws import OP_PING, OP_TEXT, WSClosed, WSError, WSTimeout, WebSocket  # noqa: E402
from ws_testserver import accept_handshake, make_listener, read_frame  # noqa: E402


class Srv(threading.Thread):
    def __init__(self, script):
        super().__init__(daemon=True)
        self.script = script
        self.listener = make_listener()
        self.port = self.listener.getsockname()[1]
        self.ready = threading.Event()
        self.wire = []
        self.error = ""
        self.debug = []

    def run(self):
        conn = None
        try:
            self.ready.set()
            conn = accept_handshake(self.listener)
            self.debug.append(f"握手完成 sock={conn}")
            self.script(self, conn)
            self.debug.append("script 正常返回")
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.debug.append(f"脚本异常 {self.error}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


def t_violation(server, conn):
    """违规控制帧：FIN=0 的 ping，然后等客户端反应。"""
    conn.sendall(bytes([0x09]) + bytes([126]) + struct.pack("!H", 200) + b"P" * 200)
    time.sleep(0.5)
    try:
        server.wire.append(read_frame(conn))
    except Exception as exc:
        server.wire.append(("noread", type(exc).__name__))
    conn.settimeout(6.0)
    try:
        while conn.recv(4096):
            pass
    except OSError:
        pass


def t_no_bytes(server, conn):
    """握手成功后一个字节都不发（保持连接开着）。"""
    conn.settimeout(6.0)
    try:
        while conn.recv(4096):
            pass
    except OSError:
        pass


def t_partial_frame(server, conn):
    """只发 2 字节帧头（长度=126），然后卡住（连接保持开着）。"""
    conn.sendall(bytes([0x80 | OP_TEXT, 126]))
    conn.settimeout(6.0)
    try:
        while conn.recv(4096):
            pass
    except OSError:
        pass


print("== 1. 违规控制帧之后的连接状态 ==")
s = Srv(t_violation)
s.start(); s.ready.wait(3)
ws = WebSocket(f"ws://127.0.0.1:{s.port}/", timeout=2)
ws.connect()
try:
    got = ws.recv_text()
    print(f"   recv_text 返回 {got!r}")
except WSError as exc:
    print(f"   抛 {type(exc).__name__}: {exc}")
print(f"   ws.closed={ws.closed} _sock_closed={ws._sock_closed} sock_closed={ws._sock_closed}")
t0 = time.time()
try:
    got2 = ws.recv_text(timeout=0.5)
    print(f"   再读一次 → 返回 {got2!r}（{time.time()-t0:.2f}s）")
except WSError as exc:
    print(f"   再读一次 → {type(exc).__name__}: {exc}（{time.time()-t0:.2f}s）")
print(f"   服务端收到的客户端帧：{[(hex(o), len(pl)) for o, pl, _ in s.wire if isinstance(o, int)]}")
print(f"   srv.debug={s.debug} error={s.error!r}")
ws.close(); s.join(3)

print("\n== 2. 一个字节都没到（干净空闲超时） ==")
s2 = Srv(t_no_bytes)
s2.start(); s2.ready.wait(3)
ws2 = WebSocket(f"ws://127.0.0.1:{s2.port}/", timeout=2)
ws2.connect()
t0 = time.time()
try:
    ws2.recv_text(timeout=0.3)
    print("   返回了？")
except WSError as exc:
    print(f"   {type(exc).__name__}: {exc}（{time.time()-t0:.2f}s）")
print(f"   ws2.closed={ws2.closed} srv.debug={s2.debug} error={s2.error!r}")
print(f"   连接还能用吗（再读一次 0.3s）→ ", end="")
try:
    ws2.recv_text(timeout=0.3)
except WSError as exc:
    print(f"{type(exc).__name__}: {exc}")
ws2.close(); s2.join(3)

print("\n== 3. 帧头已到、后续卡住 ==")
s3 = Srv(t_partial_frame)
s3.start(); s3.ready.wait(3)
ws3 = WebSocket(f"ws://127.0.0.1:{s3.port}/", timeout=2)
ws3.connect()
t0 = time.time()
try:
    ws3.recv_text(timeout=0.3)
except WSError as exc:
    print(f"   第一次 {type(exc).__name__}: {exc}（{time.time()-t0:.2f}s）")
t0 = time.time()
try:
    ws3.recv_text(timeout=0.5)
except WSError as exc:
    print(f"   再读一次 {type(exc).__name__}: {exc}（{time.time()-t0:.2f}s）")
print(f"   ws3.closed={ws3.closed} srv.debug={s3.debug} error={s3.error!r}")
ws3.close(); s3.join(3)
