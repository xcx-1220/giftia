"""极简 WebSocket 客户端（RFC 6455），只用标准库。

为什么自己写：这个项目坚持零第三方依赖，而 QQ 官方机器人推荐的接入方式恰好是 WebSocket。
本机 Python 也没装 `websockets`，与其让你去动运行时环境，不如把需要的这一小块实现掉。

只实现接网关真正用到的部分：握手、文本帧、分片、ping/pong、关闭握手。
不支持任何扩展（permessage-deflate 之类），协商时也不请求它们。

**超时语义（重要，踩过坑）**：
- 连接空闲、**帧还没开始读**时超时 → `WSTimeout`，连接仍然可用（心跳间隙就靠它）；
- **帧读到一半**超时/EOF → `WSClosed`，因为字节流已经错位：那半个帧头会被当成下一帧的帧头，
  两条独立的消息会被拼成一条。以前这里抛 `WSTimeout` 而适配器正好 `except WSTimeout: continue`，
  于是带着错位的连接继续收消息——所以现在按"帧内 = 连接不可信"处理。
- 调用方给的 `timeout` 是**整帧的 deadline**，不是"每个 recv 各等一次"。
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import time
from urllib.parse import urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

DEFAULT_TIMEOUT = 60.0
DEFAULT_CONNECT_TIMEOUT = 20.0
DEFAULT_MAX_FRAME = 8 << 20          # 单帧上限 8MB
DEFAULT_MAX_MESSAGE = 32 << 20       # 一条消息（所有分片加起来）上限 32MB


class WSError(RuntimeError):
    """协议层错误。"""


class WSClosed(WSError):
    """对端关闭或连接中断（**连接已不可信**）。"""


class WSTimeout(WSError):
    """在等待数据时超时——连接本身仍然可用（常用于心跳间隙）。"""


class WebSocket:
    def __init__(
        self,
        url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        max_frame: int = DEFAULT_MAX_FRAME,
        max_message: int = DEFAULT_MAX_MESSAGE,
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
        self.max_message = max_message
        self.sock: socket.socket | None = None
        self._buf = b""
        # closed = 协议层已关闭（收到过对端 close，或本地 close 过）
        # _sock_closed = **socket 真的关掉了**。两者必须分开：
        # 对端 close 之后本地 close() 如果直接 return，TCP 连接就一直挂着不释放。
        self.closed = False
        self._sock_closed = False

    # ── 连接 ──────────────────────────────────────────────
    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        if self.secure:
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=self.host)
        self.sock = raw

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        # Host 头要带非默认端口，否则严格的虚拟主机校验会拒
        default_port = 443 if self.secure else 80
        host_header = self.host if self.port == default_port else f"{self.host}:{self.port}"
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
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
        status = lines[0].strip()
        # 严格判状态行，别用 `"101" in lines[0]`（"1101" 也会命中）
        if not status.upper().startswith("HTTP/1.1 101"):
            raise WSError(f"握手失败：{status}")

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
        if headers.get("upgrade", "").lower() != "websocket":
            raise WSError(f"缺少 Upgrade: websocket 响应头（{headers.get('upgrade')!r}）")
        if "upgrade" not in headers.get("connection", "").lower():
            raise WSError(f"缺少 Connection: Upgrade 响应头（{headers.get('connection')!r}）")

    # ── 底层读写 ──────────────────────────────────────────
    def _settimeout(self, timeout: float) -> None:
        """给 socket 设置超时。单独包一层，方便测试与复用。"""
        if self.sock is not None:
            self.sock.settimeout(timeout)

    def _recv_until(self, n: int, deadline: float | None) -> tuple[bytes, bool]:
        """尽量读满 n 字节。返回 (数据, 是否中途超时)。

        `deadline` 是 **monotonic 绝对时间**；None = 用 self.timeout 兜底。
        超时时返回已读到的部分（可能是空的），由调用方决定语义。
        """
        if self.sock is None:
            raise WSClosed("尚未连接")
        out = bytearray()
        if self._buf:
            take = self._buf[:n]
            self._buf = self._buf[len(take) :]
            out += take
        while len(out) < n:
            wait = self.timeout if deadline is None else max(0.05, deadline - time.monotonic())
            self._settimeout(wait)
            try:
                chunk = self.sock.recv(n - len(out))
            except socket.timeout:
                return bytes(out), True
            except OSError as exc:
                raise WSClosed(f"连接中断：{exc}") from exc
            if not chunk:
                if out:
                    raise WSClosed("读取帧中途连接被对端关闭，连接状态已不可信")
                raise WSClosed("连接已被对端关闭")
            out += chunk
        return bytes(out), False

    def _read_frame(self, timeout: float | None = None) -> tuple[bool, int, bytes]:
        """读一帧。

        帧**还没开始**读时超时 → WSTimeout（连接仍可用）；
        帧**读到一半**超时/EOF → WSClosed（连接不可信）。
        """
        budget = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + budget

        head, timed_out = self._recv_until(2, deadline)
        if timed_out:
            # 一个字节都没读到 = 干净的"空闲超时"
            raise WSTimeout("等待数据超时")
        b1, b2 = head[0], head[1]

        def rest(n: int) -> bytes:
            if not n:
                return b""
            data, cut = self._recv_until(n, deadline)
            if cut:
                raise WSClosed("读取帧中途超时，连接状态已不可信")
            return data

        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", rest(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", rest(8))[0]
        if length > self.max_frame:
            raise WSError(f"帧过大：{length} 字节（上限 {self.max_frame}）")
        mask = rest(4) if masked else None
        payload = rest(length)
        if mask:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return fin, opcode, payload

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        if self.sock is None or self._sock_closed:
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
        budget = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + budget
        chunks: list[bytes] = []
        total = 0
        while True:
            # 整条消息共享一个 deadline：超时按"这一次 recv_text"算，不随分片数累加
            frame_timeout = max(0.05, deadline - time.monotonic())
            fin, opcode, payload = self._read_frame(frame_timeout)
            if opcode in (OP_PING, OP_PONG, OP_CLOSE):
                # RFC 6455 §5.5：控制帧不能分片，payload ≤ 125
                if not fin:
                    raise WSError(f"控制帧不允许分片（opcode={opcode}）")
                if len(payload) > 125:
                    raise WSError(f"控制帧 payload 过长：{len(payload)} 字节")
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
                total += len(payload)
                if total > self.max_message:
                    raise WSError(
                        f"消息过大：{total} 字节（上限 {self.max_message}）"
                    )
            else:
                raise WSError(f"未知 opcode：{opcode}")
            if fin:
                return b"".join(chunks).decode("utf-8", "replace")

    def close(self, code: int = 1000) -> None:
        """关闭连接。

        注意：对端先发 close 时 `self.closed` 已经是 True，但 socket 还必须真的关——
        否则 `session_loop` 的 `finally: ws.close()` 恰好在最需要它的路径上变成空操作，
        TCP 连接会一直挂着（只能等 GC 兜底）。
        """
        if self._sock_closed or self.sock is None:
            self.closed = True
            return
        if not self.closed:
            try:
                self._send_frame(OP_CLOSE, struct.pack("!H", code))
            except Exception:
                pass
        self.closed = True
        self._sock_closed = True
        try:
            self.sock.close()
        except Exception:
            pass

    def __enter__(self) -> "WebSocket":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
