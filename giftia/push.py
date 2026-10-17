"""Web Push —— **零第三方依赖**，只用标准库。

## 为什么是"无载荷推送"

Web Push 的标准加密载荷要 **AES-128-GCM**，而 Python 标准库没有 AES ✗
（`hashlib`/`hmac` 有，对称加密没有）。项目的铁律是零第三方依赖，所以：

    服务器 → 推一个**空的响铃**（无 body）→ 她的 service worker 被唤醒
           → SW 去后端取那条消息（GET /api/push/last）→ 弹出通知

这不需要加密任何内容 ✓ 只需要 VAPID 签名（ES256）✓ 而 ECDSA P-256
用标准库的整数运算就能实现（下面那几十行）。

代价：多一次本地请求（几毫秒），换来"不加依赖"。

## VAPID 是什么

推送服务（Chrome/Firefox/Apple 的服务器）要确认"是谁在推"。做法是每次带一个
用私钥签的 JWT（ES256）。公钥放在 `Authorization` 头里，服务端用它验签。
口令对不上 → 401。

## 你的要求（已经落在设计里）

**只有搭档能收到推送。** 订阅接口只允许 `role=owner`，访客连订阅都做不到 ✗
所以"她主动来找你"这件事，永远不会打扰到访客。

## 用法

    python3 -m giftia.push --gen     # 生成一对 VAPID 密钥（写进 .env）
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path

# ── P-256 曲线参数（NIST 标准，不是随便选的）────────────────────
_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (
    0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
)


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ── 椭圆曲线点运算（仿射坐标，用 pow 求逆）──────────────────────
def _inv(x: int) -> int:
    return pow(x, -1, _P)


def _add(p1: tuple[int, int] | None, p2: tuple[int, int] | None):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % _P == 0:
            return None                    # 互为逆元 → 无穷远点
        lam = (3 * x1 * x1 - 3) * _inv(2 * y1) % _P    # 自己的切线
    else:
        lam = (y2 - y1) * _inv(x2 - x1) % _P
    x3 = (lam * lam - x1 - x2) % _P
    y3 = (lam * (x1 - x3) - y1) % _P
    return (x3, y3)


def _mul(k: int, point: tuple[int, int]) -> tuple[int, int] | None:
    result = None
    addend = point
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        k >>= 1
    return result


# ── 密钥 ────────────────────────────────────────────────────────
def generate_keys() -> tuple[str, str]:
    """返回 (私钥, 公钥)，都是 base64url。

    私钥 = 32 字节标量；公钥 = 65 字节非压缩点（0x04 || X || Y），
    正是浏览器 `applicationServerKey` 要的格式。
    """
    d = secrets.randbelow(_N - 1) + 1
    q = _mul(d, _G)
    assert q is not None
    priv = _b64e(d.to_bytes(32, "big"))
    pub = _b64e(b"\x04" + q[0].to_bytes(32, "big") + q[1].to_bytes(32, "big"))
    return priv, pub


def public_key_from_private(priv_b64: str) -> str:
    d = int.from_bytes(_b64d(priv_b64), "big")
    q = _mul(d, _G)
    assert q is not None
    return _b64e(b"\x04" + q[0].to_bytes(32, "big") + q[1].to_bytes(32, "big"))


def sign_es256(private_b64: str, message: bytes) -> bytes:
    """ECDSA P-256 + SHA-256，返回 64 字节裸签名 r||s（JWS 要的格式）。

    注意 JWS 用的是 **raw r||s**，不是 DER。多一个字节都验不过。
    """
    d = int.from_bytes(_b64d(private_b64), "big")
    z = int.from_bytes(hashlib.sha256(message).digest(), "big")
    for _ in range(32):
        k = secrets.randbelow(_N - 1) + 1
        point = _mul(k, _G)
        assert point is not None
        r = point[0] % _N
        if r == 0:
            continue
        s = (pow(k, -1, _N) * (z + r * d)) % _N
        if s == 0:
            continue
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")
    raise RuntimeError("签名失败（概率极低，重试）")


# ── VAPID ──────────────────────────────────────────────────────
def make_vapid_headers(
    endpoint: str,
    private_b64: str,
    public_b64: str,
    *,
    subject: str,
    now: int | None = None,
) -> dict[str, str]:
    """构造 Authorization 头：`vapid t=<jwt>, k=<公钥>`。"""
    from urllib.parse import urlparse

    parsed = urlparse(endpoint)
    aud = f"{parsed.scheme}://{parsed.netloc}"
    exp = int(now if now is not None else time.time()) + 12 * 3600
    header = _b64e(json.dumps({"alg": "ES256", "typ": "JWT"},
                              separators=(",", ":")).encode())
    payload = _b64e(json.dumps({"aud": aud, "exp": exp, "sub": subject},
                               separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    sig = sign_es256(private_b64, signing_input)
    jwt = f"{header}.{payload}.{_b64e(sig)}"
    return {"Authorization": f"vapid t={jwt}, k={public_b64}"}


def send_push(
    endpoint: str,
    private_b64: str,
    public_b64: str,
    *,
    subject: str,
    ttl: int = 86400,
    urgency: str = "normal",
    timeout: float = 20.0,
    relay_url: str = "",
    relay_secret: str = "",
) -> tuple[bool, int, str]:
    """发一个**空载荷**推送。返回 (成功, HTTP 码, 说明)。

    201 = 推送服务收下了；404/410 = 这个订阅已经失效（该删掉）。

    `relay_url`：**大陆服务器必须用**。实测服务器连不上 fcm.googleapis.com
    （6 秒超时），但能连 Cloudflare —— 所以推送先发给一个 Cloudflare Worker，
    由它代发（见 deploy/cf-push-relay/worker.js）。为空时直连。
    """
    headers = make_vapid_headers(endpoint, private_b64, public_b64, subject=subject)

    if relay_url:
        from urllib.parse import quote

        sep = "&" if "?" in relay_url else "?"
        target = f"{relay_url}{sep}to={quote(endpoint, safe='')}"
        req = urllib.request.Request(target, data=b"", method="POST")
        req.add_header("X-Relay-Secret", relay_secret)
        req.add_header("X-Push-Authorization", headers["Authorization"])
        req.add_header("X-Push-TTL", str(ttl))
        req.add_header("X-Push-Urgency", urgency)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                try:
                    code = int(__import__("json").loads(body).get("status") or 0)
                except ValueError:
                    code = 0
                return code in (200, 201, 202), code, body[:200]
        except urllib.error.HTTPError as exc:
            return False, exc.code, exc.read().decode("utf-8", "replace")[:200]
        except Exception as exc:
            return False, 0, f"{type(exc).__name__}: {exc}"

    headers.update({"TTL": str(ttl), "Urgency": urgency, "Content-Length": "0"})
    req = urllib.request.Request(endpoint, data=b"", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, resp.status, ""
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return False, exc.code, body
    except Exception as exc:                      # 网络问题
        return False, 0, f"{type(exc).__name__}: {exc}"


# ── 订阅存储 ───────────────────────────────────────────────────
class SubscriptionStore:
    """把浏览器给的订阅存成 JSON。只读/写一个小文件，不碰她的数据库。"""

    def __init__(self, path: Path, *, limit: int = 12) -> None:
        self.path = Path(path)
        self.limit = limit

    def load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def save(self, subs: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 只留最近 limit 个（换手机/重装会不断产生新订阅）
        self.path.write_text(
            json.dumps(subs[-self.limit:], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add(self, sub: dict) -> None:
        endpoint = str(sub.get("endpoint") or "")
        if not endpoint:
            raise ValueError("订阅里没有 endpoint")
        subs = [s for s in self.load() if s.get("endpoint") != endpoint]
        subs.append({
            "endpoint": endpoint,
            "keys": sub.get("keys") or {},
            "added": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        self.save(subs)

    def remove(self, endpoint: str) -> None:
        self.save([s for s in self.load() if s.get("endpoint") != endpoint])

    def __len__(self) -> int:
        return len(self.load())


class LastMessage:
    """最近一条要弹出来的消息。

    为什么要有它：推送本身是**空的**（无载荷），SW 被唤醒后要来取内容。
    落盘而不是放内存，是因为"推完到取内容"之间万一进程重启，消息不能丢。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def set(self, text: str, *, title: str = "艾拉") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "title": title,
            "text": text,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False), encoding="utf-8")

    def get(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}


def notify_all(
    store: "SubscriptionStore",
    last: LastMessage,
    text: str,
    *,
    private_b64: str,
    public_b64: str,
    subject: str,
    title: str = "艾拉",
    relay_url: str = "",
    relay_secret: str = "",
) -> tuple[int, int]:
    """把消息记下来，然后给**所有搭档的设备**推一个空响铃。

    返回 (成功数, 总订阅数)。失效的订阅（404/410）会被清掉。
    """
    last.set(text, title=title)
    subs = store.load()
    good = 0
    dead: list[str] = []
    for sub in subs:
        endpoint = str(sub.get("endpoint") or "")
        if not endpoint:
            continue
        sent, code, _ = send_push(
            endpoint, private_b64, public_b64, subject=subject,
            relay_url=relay_url, relay_secret=relay_secret,
        )
        if sent:
            good += 1
        elif code in (404, 410):
            dead.append(endpoint)   # 这个设备已经卸了/订阅过期
    for endpoint in dead:
        store.remove(endpoint)
    return good, len(subs)


# ── 命令行 ─────────────────────────────────────────────────────
def main() -> int:
    import sys

    if "--gen" in sys.argv:
        priv, pub = generate_keys()
        print("VAPID_PRIVATE_KEY=" + priv)
        print("VAPID_PUBLIC_KEY=" + pub)
        print("# ^^^ 写进服务器的 .env，然后重启 giftia-qq")
        return 0
    print("用法: python3 -m giftia.push --gen")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
