"""测试用的 WebSocket 服务端工具（RFC 6455 的另一半）。

`tools/ws_selftest.py` 和 `tools/qq_protocol_selftest.py` 共用这一份实现，
免得两份测试服务端各写一套帧编解码、然后慢慢漂移。
"""

from __future__ import annotations

import base64
import hashlib
import socket
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def recv_exact(conn: socket.socket, n: int) -> bytes:
    out = b""
    while len(out) < n:
        chunk = conn.recv(n - len(out))
        if not chunk:
            raise ConnectionError("连接已关闭")
        out += chunk
    return out


def read_frame(conn: socket.socket) -> tuple[int, bytes, bool]:
    """读取一帧，返回 (opcode, payload, 是否带掩码)。"""
    b1, b2 = recv_exact(conn, 2)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", recv_exact(conn, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv_exact(conn, 8))[0]
    mask = recv_exact(conn, 4) if masked else None
    payload = recv_exact(conn, length) if length else b""
    if mask:
        payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
    return opcode, payload, masked


def send_frame(conn: socket.socket, opcode: int, payload: bytes = b"") -> None:
    """服务端发出的帧按 RFC 不加掩码。"""
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 1 << 16:
        header.append(126)
        header += struct.pack("!H", n)
    else:
        header.append(127)
        header += struct.pack("!Q", n)
    conn.sendall(bytes(header) + payload)


def send_fragmented(conn: socket.socket, text: str, pieces: int = 3) -> None:
    data = text.encode()
    size = max(1, len(data) // pieces)
    chunks = [data[i : i + size] for i in range(0, len(data), size)]
    for index, chunk in enumerate(chunks):
        first = index == 0
        last = index == len(chunks) - 1
        header = bytearray([(0x80 if last else 0x00) | (OP_TEXT if first else OP_CONT)])
        n = len(chunk)
        if n < 126:
            header.append(n)
        else:
            header.append(126)
            header += struct.pack("!H", n)
        conn.sendall(bytes(header) + chunk)


def accept_handshake(listener: socket.socket) -> socket.socket:
    """接受一个连接并完成 WebSocket 握手，返回已升级的 socket。"""
    conn, _ = listener.accept()
    conn.settimeout(15)
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += conn.recv(4096)
    head = buf.split(b"\r\n\r\n")[0].decode("latin-1")
    key = ""
    for line in head.split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    conn.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode()
    )
    return conn


def make_listener() -> socket.socket:
    """监听 127.0.0.1 的随机端口。"""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    return listener
