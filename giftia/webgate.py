"""Web 入口：**一个进程、两个入口**。

她不是线程安全的——单线程、一个 SQLite 连接、虚拟时钟/心情/亲密度全在内存里
（`giftia/brain.py`）。所以这里有一条铁律：

    **HTTP 线程绝不碰她。** 它只把请求塞进队列，然后等主线程来执行。

结构：

    HTTP 线程（辅助）                 主线程（唯一碰她的地方）
    ────────────────                 ──────────────────────
    POST /api/chat
      ├─ 验口令
      ├─ 入队 ───────────────────►   qq 的 WS 循环里每个 tick 调 drain()
      └─ 等结果（带超时）  ◄────────   执行 handle_message(..., scene="c2c")
                                       把结果塞回 job，唤醒等待

为什么不让 HTTP 线程直接调她：Giftia 内部的 `self.xxx` 是一堆可变状态，
两个线程同时进去 = 状态错乱 + SQLite 报 "recursive use of cursors"。
队列是唯一安全的形状。

## 身份：**口令就是身份**

输对门禁口令的人 = 搭档。所以网站上的对话和 QQ 上的对话**共享同一份记忆、
同一个会话上下文**——这正是"一个她"该有的样子。

（一开始我想让访客走 `scene="guest"` 图省事，但看了代码才发现：guest 只有在
带 group_id 时才走群归属，不带的话会落到 `person` 分区、**照样写记忆**。
而且对"只有你自己用"的网站来说，把主人当成访客本来就是错的。）

以后如果要开放给别人用，再加一条"访客模式"（独立 person 分区 / 只读），
那是另一个产品决定，不要顺手加。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# 静态文件白名单：只发这些后缀，别的一律 404
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
}

# 一次 drain 最多处理几个请求（别让网站把 QQ 那边饿死）
DRAIN_PER_TICK = 2
# HTTP 那边最多等多久（秒）。超过就回 504——她可能正在跟 QQ 那边说话
JOB_TIMEOUT = 60.0
# 聊天接口每个 IP 每分钟最多几次（防 token 被烧）
DEFAULT_MAX_PER_MINUTE = 12


@dataclass
class Job:
    """一个待处理的请求。结果通过 `done` 事件回传。"""

    text: str
    # ★ 由**主线程**在 drain() 里填。HTTP 线程不许自己去查——
    #   查 handler_id 会碰 SQLite，而那个连接是主线程建的
    #   （踩过：ProgrammingError: SQLite objects created in a thread can only be used
    #     in that same thread）。这就是"HTTP 线程绝不碰她"那条铁律的具体代价。
    user_id: str = ""
    done: threading.Event = field(default_factory=threading.Event)
    reply: str = ""
    error: str = ""

    def finish(self, reply: str = "", error: str = "") -> None:
        self.reply = reply
        self.error = error
        self.done.set()

    def wait(self, timeout: float) -> bool:
        return self.done.wait(timeout)


class WebGate:
    """HTTP 入口 + 队列。`start()` 起线程，主循环里定期 `drain()`。"""

    def __init__(
        self,
        *,
        port: int,
        secret: str,
        static_dir: Path,
        host: str = "127.0.0.1",
        max_per_minute: int = DEFAULT_MAX_PER_MINUTE,
    ) -> None:
        self.port = int(port)
        self.host = host
        self.secret = (secret or "").strip()
        self.static_dir = Path(static_dir)
        self.max_per_minute = max(1, int(max_per_minute))
        self.jobs: queue.Queue[Job] = queue.Queue()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # 每个 IP 的请求时间戳（限流用）
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self.served = 0          # 处理过几个请求（心跳/日志用）
        self.rejected = 0        # 被限流挡掉几个

    # ── 口令 ──────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        """没设口令就不开——**不允许无口令的公网入口**（会烧 token）。"""
        return bool(self.secret)

    def token(self) -> str:
        """登录成功后发的 cookie 值。改口令 = 所有人重新登录。"""
        return hmac.new(self.secret.encode("utf-8"), b"aila-web", hashlib.sha256).hexdigest()

    def check_pass(self, given: str) -> bool:
        """constant-time 比较，别让口令能被逐字符猜。"""
        return bool(self.secret) and hmac.compare_digest(
            (given or "").strip(), self.secret
        )

    def check_cookie(self, value: str) -> bool:
        return bool(value) and hmac.compare_digest(value, self.token())

    # ── 限流 ──────────────────────────────────────────────
    def allow(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            stamps = [t for t in self._hits.get(ip, []) if now - t < 60.0]
            if len(stamps) >= self.max_per_minute:
                self._hits[ip] = stamps
                self.rejected += 1
                return False
            stamps.append(now)
            self._hits[ip] = stamps
            return True

    # ── 生命周期 ──────────────────────────────────────────
    def start(self) -> bool:
        if not self.enabled:
            print("[web] 没配口令，网站入口不启动（公网无口令 = 谁都能烧你的 token）")
            return False
        gate = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "AilaWeb/1.0"

            def log_message(self, *_args: Any) -> None:  # 别把 access log 灌进 journal
                return

            def log_error(self, fmt: str, *args: Any) -> None:
                # log_message 被我们关掉了，但**出错必须看得见**：
                # handler 里抛异常会让连接直接断掉，客户端只看到
                # "Remote end closed connection"，很难查（踩过）。
                print("[web] " + (fmt % args), file=sys.stderr)

            def _guard(self, fn) -> None:
                try:
                    fn()
                except Exception as exc:
                    import traceback

                    print(f"[web] 处理请求出错：{type(exc).__name__}: {exc}", file=sys.stderr)
                    traceback.print_exc()
                    try:
                        self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "服务端出错"})
                    except Exception:
                        pass

            # —— 工具 ——
            def _json(self, code: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> dict:
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return {}
                if n <= 0 or n > 200_000:
                    return {}
                try:
                    return json.loads(self.rfile.read(n).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    return {}

            def _cookie(self, name: str) -> str:
                raw = self.headers.get("Cookie") or ""
                for part in raw.split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == name:
                        return v
                return ""

            # —— 路由 ——
            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path == "/api/health":
                    return self._json(HTTPStatus.OK, {"ok": True})
                return self._guard(lambda: self._static(path))

            def do_POST(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path == "/api/login":
                    return self._guard(self._login)
                if path == "/api/chat":
                    return self._guard(self._chat)
                return self._json(HTTPStatus.NOT_FOUND, {"error": "没有这个接口"})

            def _login(self) -> None:
                data = self._body()
                if not gate.check_pass(str(data.get("pass") or "")):
                    # 失败也慢一点，别让它成为一个高速爆破口
                    time.sleep(0.4)
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": "口令不对"})
                tok = gate.token()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header(
                    "Set-Cookie",
                    f"aila_auth={tok}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000",
                )
                body = b'{"ok": true}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _chat(self) -> None:
                if not gate.check_cookie(self._cookie("aila_auth")):
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": "还没登录"})
                ip = self.client_address[0] if self.client_address else "?"
                if not gate.allow(ip):
                    return self._json(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        {"error": "你说得太快了，等一下下。"},
                    )
                text = str((self._body().get("text") or "")).strip()
                if not text:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "空消息"})
                job = Job(text=text[:2000])   # user_id 留空，主线程来填
                gate.jobs.put(job)
                if not job.wait(JOB_TIMEOUT):
                    return self._json(
                        HTTPStatus.GATEWAY_TIMEOUT,
                        {"error": "她那边正忙着，稍后再试。"},
                    )
                if job.error:
                    return self._json(HTTPStatus.BAD_GATEWAY, {"error": job.error})
                return self._json(
                    HTTPStatus.OK,
                    {"reply": job.reply, "parts": split_parts(job.reply)},
                )

            def _static(self, path: str) -> None:
                rel = "index.html" if path in ("/", "") else path.lstrip("/")
                target = (gate.static_dir / rel).resolve()
                root = gate.static_dir.resolve()
                # 目录穿越防护：解析后必须还在 web/ 里
                if not str(target).startswith(str(root)) or not target.is_file():
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "没有这个文件"})
                ctype = STATIC_TYPES.get(target.suffix.lower())
                if ctype is None:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "不允许的文件类型"})
                body = target.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                # 壳文件别缓存（改完刷新就能看到）；图标之类可以短缓存
                self.send_header(
                    "Cache-Control",
                    "no-cache" if target.suffix.lower() in (".html", ".js", ".css")
                    else "public, max-age=86400",
                )
                self.end_headers()
                self.wfile.write(body)

        # 关了地址反查（不然每个请求都要等一次 DNS）
        ThreadingHTTPServer.address_family = ThreadingHTTPServer.address_family
        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="aila-web", daemon=True
        )
        self._thread.start()
        print(f"[web] 网站入口已启动：http://{self.host}:{self.port}（口令已设）")
        return True

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # ── 主线程这边 ────────────────────────────────────────
    @property
    def pending(self) -> int:
        """队列里还有几个没处理。主循环用它决定要不要缩短 recv 等待。"""
        return self.jobs.qsize()

    def drain(self, settings: Any, *, limit: int = DRAIN_PER_TICK) -> int:
        """**只能在主线程调。** 把排队的请求交给 brain 执行，返回处理了几个。

        `settings` 必须显式传：适配器里 hub 是懒创建的，用 get_default()
        可能在它还没建好时拿到一份"另一套配置"的她（那就是两个她了）。
        """
        from .brain import handle_message, get_default

        hub = get_default(settings)
        handled = 0
        while handled < limit:
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                break
            handled += 1
            try:
                # ★ 唯一的执行点：主线程 + scene="c2c" + 搭档身份
                #   → 和 QQ 那边共享同一份记忆、同一个会话上下文。
                #   user_id 在这里才解析（读 handler_id 要碰 SQLite，HTTP 线程不能碰）
                reply = handle_message(
                    job.text,
                    job.user_id or hub.handler_id or "handler",
                    settings=settings,
                    scene="c2c",
                )
                job.finish(reply=reply)
            except Exception as exc:  # 网页出问题绝不能拖垮 QQ 那条线
                job.finish(error=f"{type(exc).__name__}: {exc}")
        if handled:
            self.served += handled
        return handled


def split_parts(text: str, *, limit: int = 3, max_chars: int = 60) -> list[str]:
    """把一句回复切成 1~3 段，让前端**一段一段**冒出来（像真人分几条发）。

    按换行和句末标点切，尽量切在语气的停顿处。切不出来就整段返回——
    宁可一次说完，也别把一句话从中间劈开。
    """
    body = (text or "").strip()
    if not body:
        return []
    parts: list[str] = []
    buf = ""
    for ch in body:
        buf += ch
        if ch == "\n":
            # 换行是她自己敲出来的停顿，无条件断
            if buf.strip():
                parts.append(buf.strip())
                buf = ""
        elif ch in "。！？…" and len(buf.strip()) >= 4:
            # 4 个字就够了——"我在的。"就是一句完整的话，
            # 门槛设成 8 的话短句永远切不开（踩过）
            parts.append(buf.strip())
            buf = ""
    if buf.strip():
        parts.append(buf.strip())
    if len(parts) <= 1:
        return parts or [body]
    if len(parts) > limit:
        # 段落太多就合并尾巴，别刷屏
        head = parts[: limit - 1]
        head.append("".join(parts[limit - 1 :]))
        parts = head
    return [p for p in parts if p]
