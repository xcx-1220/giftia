"""极简 WebSocket 客户端（RFC 6455），只用标准库。

为什么自己写：这个项目坚持零第三方依赖，而 QQ 官方机器人推荐的接入方式恰好是 WebSocket。
本机 Python 也没装 `websockets`，与其让你去动运行时环境，不如把需要的这一小块实现掉。

只实现接网关真正用到的部分：握手、文本帧、分片、ping/pong、关闭握手。
不支持任何扩展（permessage-deflate 之类），协商时也不请求它们。
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
from urllib.parse import urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WSError(RuntimeError):
    """协议层错误。"""


class WSClosed(WSError):
    """对端关闭或连接中断。"""


class WSTimeout(WSError):
    """在等待数据时超时——连接本身仍然可用（常用于心跳间隙）。"""


class WebSocket:
    def __init__(
        self,
        url: str,
        *,
        timeout: float = 60.0,
        connect_timeout: float = 20.0,
        max_frame: int = 8 << 20,
    ) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise WSError(f"不支持的协议：{parsed.scheme}")
        if not parsed.hostname:
            raise WSError(f"无法解析地址：{url}")
        self.url = url
        self.host = parsed.hostname
        self.secure = parsed.scheme == "wss"
        self.port = parsed.port or (443 if self.secure else 80)
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.max_frame = max_frame
        self.sock: socket.socket | None = None
        self._buf = b""
        self.closed = False

    # ── 连接 ──────────────────────────────────────────────
    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        if self.secure:
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=self.host)
        self.sock = raw

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode("ascii"))

        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSError("握手过程中连接被关闭")
            head += chunk
            if len(head) > 1 << 16:
                raise WSError("握手响应异常过大")
        header_blob, _, rest = head.partition(b"\r\n\r\n")
        self._buf = rest  # 有可能已经捎带了第一帧

        lines = header_blob.decode("latin-1").split("\r\n")
        if "101" not in lines[0]:
            raise WSError(f"握手失败：{lines[0].strip()}")

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + GUID).encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise WSError("Sec-WebSocket-Accept 校验不通过")

    # ── 底层读写 ──────────────────────────────────────────
    def _read(self, n: int, timeout: float | None = None) -> bytes:
        if self.sock is None:
            raise WSClosed("尚未连接")
        out = bytearray()
        if self._buf:
            take = self._buf[:n]
            self._buf = self._buf[len(take) :]
            out += take
        while len(out) < n:
            self.sock.settimeout(self.timeout if timeout is None else timeout)
            try:
                chunk = self.sock.recv(n - len(out))
            except socket.timeout as exc:
                if not out:
                    raise WSTimeout("等待数据超时") from exc
                raise WSClosed("读取帧中途超时，连接状态已不可信") from exc
            except OSError as exc:
                raise WSClosed(f"连接中断：{exc}") from exc
            if not chunk:
                raise WSClosed("连接已被对端关闭")
            out += chunk
        return bytes(out)

    def _read_frame(self, timeout: float | None = None) -> tuple[bool, int, bytes]:
        b1, b2 = self._read(2, timeout)
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read(8))[0]
        if length > self.max_frame:
            raise WSError(f"帧过大：{length} 字节")
        mask = self._read(4) if masked else None
        payload = self._read(length) if length else b""
        if mask:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return fin, opcode, payload

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        if self.sock is None or self.closed:
            raise WSClosed("连接已关闭")
        header = bytearray([0x80 | opcode])  # FIN=1
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        mask = os.urandom(4)  # 客户端发出的帧必须掩码
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    # ── 对外接口 ──────────────────────────────────────────
    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def ping(self, payload: bytes = b"") -> None:
        self._send_frame(OP_PING, payload)

    def recv_text(self, timeout: float | None = None) -> str:
        """收一条完整的文本消息。会自动回 pong、忽略 pong、处理分片。

        超时抛 WSTimeout（连接仍可用），对端关闭抛 WSClosed。
        """
        chunks: list[bytes] = []
        while True:
            fin, opcode, payload = self._read_frame(timeout)
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
                try:
                    self._send_frame(OP_CLOSE, payload[:2])
                except Exception:
                    pass
                self.closed = True
                raise WSClosed(f"对端关闭连接（code={code}）")
            if opcode in (OP_TEXT, OP_CONT, OP_BIN):
                chunks.append(payload)
            else:
                raise WSError(f"未知 opcode：{opcode}")
            if fin:
                return b"".join(chunks).decode("utf-8", "replace")

    def close(self, code: int = 1000) -> None:
        if self.sock is None or self.closed:
            return
        try:
            self._send_frame(OP_CLOSE, struct.pack("!H", code))
        except Exception:
            pass
        self.closed = True
        try:
            self.sock.close()
        except Exception:
            pass

    def __enter__(self) -> "WebSocket":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
