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
import re
from urllib.parse import parse_qs, urlsplit
from .requests import RequestLedger
import secrets
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
# 队列里最多排这么多个；再多人就先谢客。
# 为什么：她单线程、每条要 1~5 秒，排到 10 个人最后一个要等 40 秒以上，
# 而 HTTP 那边 60 秒就超时 —— 让人干等 60 秒再看到"504"是最差的第一印象。
# 直接说"她正忙着"反而体面，而且前端能立刻重试。
MAX_PENDING = 5
# HTTP 那边最多等多久（秒）。超过就回 504——她可能正在跟 QQ 那边说话
JOB_TIMEOUT = 60.0
# 聊天接口每个 IP 每分钟最多几次（防 token 被烧）
DEFAULT_MAX_PER_MINUTE = 12


@dataclass
class Job:
    """一个待处理的请求。结果通过 `done` 事件回传。"""

    text: str
    request_id: str = ""
    principal: str = ""
    recover_only: bool = False
    enqueued_at: float = field(default_factory=time.monotonic)
    # ★ 由**主线程**在 drain() 里填。HTTP 线程不许自己去查——
    #   查 handler_id 会碰 SQLite，而那个连接是主线程建的
    #   （踩过：ProgrammingError: SQLite objects created in a thread can only be used
    #     in that same thread）。这就是"HTTP 线程绝不碰她"那条铁律的具体代价。
    user_id: str = ""
    # "owner" = 搭档本人（共享 QQ 那边的记忆）；"friend" = 访客（独立分区）
    role: str = "owner"
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
        friend_pass: str = "",
        data_dir: Path | None = None,
    ) -> None:
        self.port = int(port)
        self.host = host
        self.secret = (secret or "").strip()
        # 朋友用的另一套口令。**必须和搭档那套分开**：
        # 用同一个口令的话，朋友进来就是"以搭档的身份"——她会当着他的面
        # 提起你的私事，他的话还会写进你的会话，心情/关系/寿命也被陌生人推进。
        self.friend_pass = (friend_pass or "").strip()
        self.static_dir = Path(static_dir)
        self.max_per_minute = max(1, int(max_per_minute))
        # 推送订阅 + 最后一条消息（只给搭档用，访客连订阅接口都进不去）
        self._data_dir = Path(data_dir) if data_dir else Path(static_dir).parent / "data"
        from .push import LastMessage, SubscriptionStore

        self.push_subs = SubscriptionStore(self._data_dir / "push_subs.json")
        self.push_last = LastMessage(self._data_dir / "push_last.json")
        self.jobs: queue.Queue[Job] = queue.Queue(maxsize=MAX_PENDING)
        self.ledger = RequestLedger(self._data_dir / 'web_requests.json')
        self._sessions = {}
        self._owner_epoch = ''
        self._requests_lock = threading.Lock()
        self._live_jobs = {}
        for key, row in list(self.ledger.rows.items()):
            if row.get('status') not in ('queued', 'running'):
                continue
            job = Job(text=row['text'], request_id=row['request_id'], principal=row['principal'],
                      role=row['role'], user_id=row['user_id'], recover_only=row['status']=='running')
            try:
                self.jobs.put_nowait(job)
                self._live_jobs[key] = job
            except queue.Full:
                self.ledger.update(key, status='failed', error='服务重启时队列已满；此请求没有自动重发')
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # 每个 IP 的请求时间戳（限流用）
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self.served = 0          # 处理过几个请求（心跳/日志用）
        self.rejected = 0        # 被限流挡掉几个

    def principal(self, role, visitor='web-guest'):
        identity = self.token(role) + ':' + self._owner_epoch
        if role == 'friend':
            identity += ':' + visitor
        return hashlib.sha256(identity.encode()).hexdigest()

    def submit(self, text, role, user_id, request_id, principal):
        key = principal + ':' + request_id
        with self._requests_lock:
            old = self.ledger.get(key)
            if old:
                if old['text'] != text:
                    raise ValueError('同一请求编号不能用于不同消息')
                if key in self._live_jobs:
                    return self._live_jobs[key]
                job = Job(text=text, request_id=request_id, principal=principal, role=role, user_id=user_id)
                job.finish(reply=old.get('reply', ''), error=old.get('error', ''))
                return job
            if self.jobs.full():
                raise queue.Full
            row = dict(text=text, role=role, user_id=user_id, request_id=request_id, principal=principal)
            self.ledger.reserve(key, row)
            job = Job(**row)
            self._live_jobs[key] = job
            self.jobs.put_nowait(job)
            return job

    # ── 口令 ──────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        """没设口令就不开——**不允许无口令的公网入口**（会烧 token）。"""
        return bool(self.secret)

    def token(self, role: str = "owner") -> str:
        """某种角色的 cookie 值（带角色前缀，一眼能看出是哪套）。
        改口令 = 该角色的人需要重新登录。"""
        key = self.secret if role == "owner" else self.friend_pass
        digest = hmac.new(key.encode("utf-8"), b"aila-web", hashlib.sha256).hexdigest()
        return f"{role}.{digest}"

    def role_of(self, passphrase: str) -> str:
        """这个口令是谁的。constant-time 比较，别让它能被逐字符猜。"""
        given = (passphrase or "").strip()
        if not given:
            return ""
        if self.secret and hmac.compare_digest(given, self.secret):
            return "owner"
        if self.friend_pass and hmac.compare_digest(given, self.friend_pass):
            return "friend"
        return ""

    def role_of_cookie(self, value: str) -> str:
        """cookie 对应哪个角色。空串 = 没登录。"""
        raw = value or ""
        if not raw:
            return ""
        for role in ("owner", "friend"):
            if role == 'friend' and not self.friend_pass:
                continue
            if hmac.compare_digest(raw, self.token(role)):
                return role
        return ""

    @property
    def has_friends(self) -> bool:
        return bool(self.friend_pass)

    # ── 限流 ──────────────────────────────────────────────
    def allow(self, ip: str, *, limit: int | None = None) -> bool:
        cap = self.max_per_minute if limit is None else max(1, int(limit))
        now = time.time()
        with self._lock:
            stamps = [t for t in self._hits.get(ip, []) if now - t < 60.0]
            if len(stamps) >= cap:
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
                if path == "/api/chat/result":
                    return self._guard(self._chat_result)
                if path == "/api/session":
                    return self._guard(self._session)
                if path == "/api/push/status":
                    return self._guard(self._push_status)
                if path == "/api/push/last":
                    return self._guard(self._push_last)
                return self._guard(lambda: self._static(path))

            def do_POST(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path == "/api/login":
                    return self._guard(self._login)
                if path == "/api/chat":
                    return self._guard(self._chat)
                if path == "/api/push/subscribe":
                    return self._guard(self._push_subscribe)
                if path == "/api/push/test":
                    return self._guard(self._push_test)
                return self._json(HTTPStatus.NOT_FOUND, {"error": "没有这个接口"})

            def _login(self) -> None:
                data = self._body()
                role = gate.role_of(str(data.get("pass") or ""))
                if not role:
                    # 失败也慢一点，别让它成为一个高速爆破口
                    time.sleep(0.4)
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": "口令不对"})
                cookies = [
                    f"aila_auth={gate.token(role)}; Path=/; HttpOnly; "
                    "SameSite=Strict; Max-Age=31536000"
                ]
                # 访客给一个随机身份 → 他在她记忆里是独立的一份，看得见自己的、
                # 看不见搭档的（视角闸门在 brain 里，这里只负责给个稳定 id）
                if role == "friend" and not self._cookie("aila_visitor"):
                    cookies.append(
                        f"aila_visitor={secrets.token_hex(8)}; Path=/; HttpOnly; "
                        "SameSite=Strict; Max-Age=31536000"
                    )
                body = json.dumps({"ok": True, "role": role}, ensure_ascii=False).encode()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                for c in cookies:
                    self.send_header("Set-Cookie", c)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _chat(self) -> None:
                role = gate.role_of_cookie(self._cookie("aila_auth"))
                if not role:
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": "还没登录"})
                ip = self.client_address[0] if self.client_address else "?"
                # 访客的额度只有搭档的一半：他们花的是你的 token
                cap = gate.max_per_minute if role == "owner" else max(
                    2, gate.max_per_minute // 2
                )
                if not gate.allow(ip, limit=cap):
                    return self._json(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        {"error": "你说得太快了，等一下下。"},
                    )
                data = self._body()
                text = str(data.get('text') or '').strip()[:2000]
                if not text:
                    return self._json(HTTPStatus.BAD_REQUEST, {'error': '空消息'})
                request_id = str(data.get('request_id') or secrets.token_hex(16))
                if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', request_id):
                    return self._json(HTTPStatus.BAD_REQUEST, {'error': '请求编号无效'})
                principal = self._principal(role)
                visitor = self._cookie('aila_visitor') or 'web-guest'
                try:
                    job = gate.submit(text, role, visitor if role == 'friend' else '', request_id, principal)
                except ValueError as exc:
                    return self._json(HTTPStatus.CONFLICT, {'error': str(exc)})
                except (queue.Full, OverflowError):
                    return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {'error': '她正忙着，稍后再试。', 'busy': True})
                job.wait(JOB_TIMEOUT)
                return self._request_payload(gate.ledger.get(principal + ':' + request_id))

            def _principal(self, role):
                visitor = self._cookie('aila_visitor') or 'web-guest'
                return gate.principal(role, visitor)

            def _request_payload(self, row):
                if not row:
                    return self._json(HTTPStatus.NOT_FOUND, {'error': '没有这个请求'})
                payload = {'request_id': row['request_id'], 'status': row['status']}
                if row['status'] == 'completed':
                    payload.update(reply=row['reply'], parts=split_parts(row['reply']))
                    return self._json(HTTPStatus.OK, payload)
                if row['status'] == 'failed':
                    payload['error'] = row.get('error') or '这条消息未能完成'
                    return self._json(HTTPStatus.BAD_GATEWAY, payload)
                return self._json(HTTPStatus.ACCEPTED, payload)

            def _chat_result(self):
                role = gate.role_of_cookie(self._cookie('aila_auth'))
                if not role:
                    return self._json(HTTPStatus.UNAUTHORIZED, {'error': '还没登录'})
                request_id = parse_qs(urlsplit(self.path).query).get('request_id', [''])[0]
                return self._request_payload(gate.ledger.get(self._principal(role) + ':' + request_id))

            def _session(self):
                role = gate.role_of_cookie(self._cookie('aila_auth'))
                if not role:
                    return self._json(HTTPStatus.UNAUTHORIZED, {'error': '还没登录'})
                payload = gate._sessions.get(role, {'known': True, 'greeting': '你来了。'})
                return self._json(HTTPStatus.OK, dict(payload, role=role, view_id=self._principal(role)[:20]))

            # ── 推送（**只给搭档**）────────────────────────────
            def _owner_only(self) -> bool:
                return gate.role_of_cookie(self._cookie("aila_auth")) == "owner"

            def _push_status(self) -> None:
                if not self._owner_only():
                    # 访客一律 403：她主动发消息只对搭档，绝不打扰访客
                    return self._json(HTTPStatus.FORBIDDEN, {"error": "只有搭档能收她的通知"})
                return self._json(HTTPStatus.OK, {
                    "owner": True,
                    "subscribed": len(gate.push_subs) > 0,
                    "count": len(gate.push_subs),
                    "public_key": gate.push_public_key(),
                })

            def _push_subscribe(self) -> None:
                if not self._owner_only():
                    return self._json(HTTPStatus.FORBIDDEN, {"error": "只有搭档能收她的通知"})
                try:
                    gate.push_subs.add(self._body())
                except ValueError as exc:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return self._json(HTTPStatus.OK, {"ok": True, "count": len(gate.push_subs)})

            def _push_last(self) -> None:
                if not self._owner_only():
                    return self._json(HTTPStatus.FORBIDDEN, {"error": "只有搭档能看"})
                return self._json(HTTPStatus.OK, gate.push_last.get() or {})

            def _push_test(self) -> None:
                if not self._owner_only():
                    return self._json(HTTPStatus.FORBIDDEN, {"error": "只有搭档能收她的通知"})
                good, total = gate.push_notify("这是一条测试通知 ✓")
                return self._json(HTTPStatus.OK, {
                    "sent": good, "total": total,
                    "error": "" if total else "还没有任何设备订阅",
                })

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

    # ── 推送（给适配器调用）──────────────────────────────────
    def push_public_key(self) -> str:
        from .push import public_key_from_private

        priv = self._push_private()
        return public_key_from_private(priv) if priv else ""

    def _push_private(self) -> str:
        import os

        return str(os.environ.get("VAPID_PRIVATE_KEY") or "").strip()

    def push_notify(self, text: str) -> tuple[int, int]:
        """记下消息并推给所有搭档设备。**绝不能抛异常** ——
        推送是锦上添花，不能因为它让她连"主动联系"都做不了。"""
        try:
            import os

            from .push import notify_all

            priv = self._push_private()
            if not priv:
                return 0, 0
            return notify_all(
                self.push_subs, self.push_last, text,
                private_b64=priv,
                public_b64=self.push_public_key(),
                subject=str(os.environ.get("VAPID_SUBJECT") or "mailto:aila@giftia.icu"),
                # 大陆服务器直连不到 fcm，走 Cloudflare Worker 中转。
                # 没配就直连（在墙外的机器上依然能用）。
                relay_url=str(os.environ.get("PUSH_RELAY_URL") or "").strip(),
                relay_secret=str(os.environ.get("PUSH_RELAY_SECRET") or "").strip(),
            )
        except Exception as exc:
            print(f"[push] 推送失败（不影响她别的功能）：{type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 0, 0

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
        self._owner_epoch = json.dumps([hub.instance_no, hub.handler_id,
                                       hub.handler_bound_at, hub._int_meta('handler_bound_msg_id')])
        latest_user = hub.store._one("SELECT * FROM messages WHERE instance_no=? AND scope='private' AND role='user' ORDER BY id DESC LIMIT 1", (hub.instance_no,))
        known = bool(hub._since_floor([dict(latest_user)] if latest_user else [], private=True))
        self._sessions = {'owner': {'known': known, 'greeting': '你来了。' if known else settings.persona.line('opening_line')},
                          'friend': {'known': True, 'greeting': '你好。'}}
        if not hasattr(hub, '_maintenance'):
            hub.schedule_maintenance()
        hub.poll_maintenance()
        handled = 0
        while handled < limit:
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                break
            handled += 1
            key = job.principal + ':' + job.request_id
            mid = 'web:' + key
            self.ledger.update(key, status='running')
            try:
                if job.principal != self.principal(job.role, job.user_id or 'web-guest'):
                    raise RuntimeError('会话身份已经改变，旧请求没有重新执行')
                old_reply = hub.store._one("SELECT content FROM messages WHERE mid=? ORDER BY id DESC LIMIT 1", (mid + ':reply',))
                if old_reply:
                    reply = old_reply['content']
                elif job.recover_only:
                    raise RuntimeError('服务重启时上一轮已中断，没有自动重复发送；请检查聊天记录后再继续')
                else:
                    reply = self._run_job(hub, settings, job, mid)
                self.ledger.update(key, status='completed', reply=reply, error='')
                job.finish(reply=reply)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self.ledger.update(key, status='failed', error=error)
                job.finish(error=error)
            finally:
                self._live_jobs.pop(key, None)
                self.jobs.task_done()
        if handled:
            self.served += handled
        hub.poll_maintenance()
        return handled

    def _run_job(self, hub, settings, job, mid):
        from .brain import handle_message
        # ★ 唯一的执行点，都在主线程（她不是线程安全的）
        if job.role == "friend":
            # 访客：scene="guest" + 他自己的 id →
            #   · 视角只有 [("person", 他自己)]，**检索不到搭档的私事**
            #   · 写进他自己的记忆分区，不进你的会话
            #   · is_handler=False → 心情/关系/寿命一点都不动
            reply = handle_message(
                job.text,
                job.user_id or "web-guest",
                settings=settings,
                scene="guest",
                mid=mid, defer_maintenance=True,
            )
        else:
            # 搭档：scene="c2c" → 和 QQ 那边共享同一份记忆、同一个会话上下文。
            # user_id 在这里才解析（读 handler_id 要碰 SQLite，HTTP 线程不能碰）
            reply = handle_message(
                job.text,
                job.user_id or hub.handler_id or "handler",
                settings=settings,
                scene="c2c",
                mid=mid, defer_maintenance=True,
                # QQ 那边一直传了这个（她的昵称），网页之前没传 →
                # 她"知道你是谁"的程度会差一点，表现就是"感觉不太一样"
                speaker_name=getattr(hub, "handler_name", "") or "",
            )
        return reply


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
