#!/usr/bin/env python
"""QQ 官方机器人适配器（v2 API + WebSocket，零第三方依赖）。

    python adapters/qq_official.py --dry-run     # 不需要凭据，验证接线
    python adapters/qq_official.py               # 真正连上 QQ

协议细节已逐条对照官方文档（2026-10）：
  鉴权   POST https://api.bot.qq.com/app/getAppAccessToken   {"appId","clientSecret"}
         → {"access_token","expires_in"}；注意失败时 HTTP 仍是 200，要看响应体里的 code
  请求头 Authorization: QQBot {access_token}
  网关   GET /gateway → {"url":"wss://api.bot.qq.com/websocket"}
  WS     op10 hello(心跳周期) → op2 identify → op0 事件 / op1 心跳 / op11 ack
         断线用 op6 resume(token, session_id, seq) 补发遗漏事件
  发消息 POST /v2/groups/{group_openid}/messages
         POST /v2/users/{user_openid}/messages
         body {"msg_type":0,"content":...,"msg_id":...,"msg_seq":N}

──────────────────────────────────────────────────────────────
平台限制（会直接影响产品设计，务必知道）
──────────────────────────────────────────────────────────────
* **被动回复**：带上事件的 msg_id 才能回。群聊窗口 5 分钟 / 最多 5 条；
  单聊窗口 60 分钟 / 最多 4 条。同一个 msg_id + msg_seq 重复发送会被去重拒掉。
* **主动消息**：另有频控（未认证 30/qpm，认证 60/qpm；每个好友每天最多收 1000 条）。
* **互动召回消息**（is_wakeup）：用户主动对话后 30 天内，机器人可分 4 个周期各发 1 条
  （当天 / 1-3 天 / 3-7 天 / 7-30 天）。这是官方唯一"她可以主动来找你"的口子，
  而且次数用完了就没有了——某种意义上比被动等待更贴合这个项目。
  本文件暂未实现，见 README「下一步」。
* 事件可能重复推送，必须按消息 id 去重，否则她会把同一句话回两遍。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from giftia import brain as brain_mod  # noqa: E402
from giftia.brain import handle_message  # noqa: E402
from giftia.config import load_env_file, load_settings  # noqa: E402
from giftia.outreach import can_send  # noqa: E402
from giftia.trigger import decide as decide_speak  # noqa: E402
from giftia.ws import WebSocket, WSClosed, WSTimeout  # noqa: E402

# ── 常量（鉴权、网关、identify、READY 已用真实凭据验证过）─────────
API_BASE = "https://api.bot.qq.com"
# 注意：这**不是**文档里写的那个地址。
# 文档示例说网关是 wss://api.bot.qq.com/websocket/，但 2026-10 用真实凭据
# 调 GET /gateway 返回的是 wss://api.sgroup.qq.com/websocket/。
# 所以这里始终以 /gateway 的返回为准，这个常量只是拿不到时的兜底。
FALLBACK_WS = "wss://api.sgroup.qq.com/websocket/"

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

INTENT_GROUP_AND_C2C = 1 << 25  # 群聊 @机器人 + 单聊

EVENT_GROUP_AT = "GROUP_AT_MESSAGE_CREATE"
EVENT_C2C = "C2C_MESSAGE_CREATE"
# 群里开启"接收所有消息"之后，**每一条**群消息（不限于 @）都会推这个事件。
# intent 和 @ 事件是同一个（GROUP_AND_C2C_EVENT = 1<<25），不需要多订阅什么，
# 开关在群里的机器人资料页上，由群管理员打开。
EVENT_GROUP_MESSAGE = "GROUP_MESSAGE_CREATE"
# 上面那个开关被打开/关闭时各推一次
EVENT_GROUP_MSG_RECEIVE = "GROUP_MSG_RECEIVE"
EVENT_GROUP_MSG_REJECT = "GROUP_MSG_REJECT"
MSG_TYPE_TEXT = 0
MAX_CHARS = 350          # 超出会被平台以"消息长度超限"拒掉，主动分条
DEDUP_WINDOW = 300.0     # 同一 msg_id 在这个秒数内只处理一次

# 平台的被动回复硬限：**同一个 msg_id 最多能回几条**（超出会被平台拒掉）。
# 见 README「平台限制」小节：群聊 @ 她之后 5 分钟内最多 5 条，单聊 60 分钟内最多 4 条。
PASSIVE_REPLY_LIMIT_GROUP = 5
PASSIVE_REPLY_LIMIT_C2C = 4
# 系统通知（终端服务预告 / 记忆整理 / 记忆强化）要占一个名额
NOTICE_REPLY_RESERVE = 1
# 限频命中时最多等这么久；等不起就如实上报为发送失败（不静默丢消息）
RATE_LIMIT_MAX_WAIT = 20.0

DEFLECT = (
    "……抱歉。我认得的人不多，如果不是你在叫我，我就先安静待着了。"
)

COMMANDS = """\
/bind          绑定为你的搭档（官方 OpenID 与 QQ 号不互通，只能这样认人）
/name 小林      改口：以后她这么叫你
/status        她的寿命、关系与记忆状态
/profile [名字] 她对每个人的印象（每人一份档案），可以只看某个人
/impress 名字 话 手动给她补一条关于某人的印象
/forget 编号    让她忘掉某条记错的（编号见 /profile）
/reconcile     让她把这阵子的记忆理一理（合并重复的，可回滚）
/unbind        解除绑定（需要当前搭档本人）
/help          这条帮助\
"""

OWNER_KEY = "qq_owner_openid"


class QQError(RuntimeError):
    pass


class QQReconnect(QQError):
    """需要重建连接（可带 resume）。"""


@dataclass
class SendResult:
    """一次发送的结果。

    为什么要这个东西：一条回复可能被切成几段发出去，**哪几段真的送达了**
    以前是算不出来的（`send()` 把异常吞掉只打一行日志）。后果很隐蔽：
    她的回复在发送之前就已经写进数据库了，所以发送失败时
    **她的记忆里记着"我说过这句话"，而你从来没看到过**——
    下一轮她可能引用"我刚才说的……"，而你一脸茫然。

    现在把结果交回给编排层：没送出去的话，就从她的记忆里撤回。
    """

    sent: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def delivered(self) -> str:
        """用户实际看到的内容（可能比原文短，也可能为空）。"""
        return "".join(self.sent)

    @property
    def progress(self) -> str:
        total = len(self.sent) + len(self.failed)
        return f"{len(self.sent)}/{total}"


def chunk_text(text: str, limit: int = MAX_CHARS) -> list[str]:
    """按长度切分，尽量切在标点上。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = max(text.rfind(ch, 0, limit) for ch in "。！？…\n，")
        cut = cut + 1 if cut > limit // 2 else limit
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    return parts


def strip_mention(content: str) -> str:
    """去掉群聊 @ 标记（形如 <@!1234> 或 <@1234>）。"""
    while content.startswith("<@") and ">" in content:
        content = content.split(">", 1)[1]
    return content.strip()


def extract_content(payload: dict) -> str:
    """把一条消息的正文取出来。

    QQ 的"聊天记录"转发（message_type=102）和"引用消息"（103）不在 content 里，
    而是放在 msg_elements 里带结构化内容 —— 别人把聊天记录转给她时，
    她读到的就是这里，所以这两类也要能取出来。
    """
    content = strip_mention(str(payload.get("content") or ""))
    elements = payload.get("msg_elements") or []
    if not elements:
        return content
    parts: list[str] = [content] if content.strip() else []
    for el in elements:
        if not isinstance(el, dict):
            continue
        # 拍一拍：官方文档里我没找到它的正式字段名，所以按**结构字段**宽松识别
        # （content_type / ark_type / type —— 也就是 blob）。
        # 注意：以前这里对整个 `str(el)` 做子串匹配，于是正常文本里出现 "poke"
        # （比如 "Pokemon"、"poker"）也会被整段替换成 "[拍了拍]"，用户的话就丢了。
        blob = f"{el.get('content_type', '')}{el.get('ark_type', '')}{el.get('type', '')}".lower()
        element_text = str(el.get("content") or "").strip()
        if ("poke" in blob or "拍了拍" in blob) and not element_text:
            parts.append("[拍了拍]")
            continue
        text = strip_mention(str(el.get("content") or ""))
        if text:
            parts.append(text)
        for sub in el.get("msg_elements") or []:  # 引用里套引用 / 并行消息
            if isinstance(sub, dict):
                sub_text = strip_mention(str(sub.get("content") or ""))
                if sub_text:
                    parts.append(sub_text)
    return "\n".join(parts).strip()


class QQBot:
    def __init__(
        self,
        app_id: str,
        client_secret: str,
        *,
        owner_openid: str = "",
        api_base: str = API_BASE,
        dry_run: bool = False,
        mock_llm: bool = False,
        settings=None,
    ) -> None:
        self.app_id = app_id
        self.client_secret = client_secret
        self.owner_openid = owner_openid.strip()
        self.api_base = api_base.rstrip("/")
        self.dry_run = dry_run
        self.settings = settings or load_settings(force_mock=mock_llm)

        self._token = ""
        self._token_expire = 0.0
        self.session_id: str | None = None
        self.seq: int | None = None
        self.bot_name = ""
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._seqs: dict[str, int] = {}
        self._stopping = False
        # 主动联系多久查一次（她真要不要开口，由 outreach 那边决定）
        self.outreach_interval = 600.0
        # 聚批：群聊里被触发后，等 wake_delay 秒把这段时间里的话一起处理。
        # target → {"items": [...], "deadline": float}。**固定倒计时，不因新消息重置。**
        self._batch: dict[str, dict] = {}
        # 发送节奏：每会话的发送时间戳（算限频用）与最近一条（同文去重用）
        self._send_times: dict[str, list[float]] = {}
        self._sent_recently: dict[str, tuple[str, float, str]] = {}
        # 这一次 send() 是否要带官方的「互动召回」字段（is_wakeup），只对单聊有意义
        self._wakeup_once = False
        # 每个 msg_id 已经回出去几条（平台按 msg_id 计数：群 5 / 单聊 4）
        self._replies_sent: dict[str, int] = {}

    # ══ 退出 ═════════════════════════════════════════════════
    def request_stop(self, *_args: object) -> None:
        """请求退出。收到 SIGTERM/SIGINT（systemd 停止服务）时由信号处理器调用。"""
        if not self._stopping:
            print("\n[退出] 收到停止信号，正在收尾…")
        self._stopping = True

    def install_signal_handlers(self) -> None:
        import signal

        for name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, self.request_stop)
            except (ValueError, OSError):
                pass  # 非主线程等情况下装不上，忽略

    # ══ HTTP ═════════════════════════════════════════════════
    def _request(
        self, method: str, path: str, payload: dict | None = None, *, auth: bool = True
    ) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
        req = urllib.request.Request(self.api_base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("Authorization", f"QQBot {self.access_token()}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise QQError(f"HTTP {exc.code}：{detail}") from exc
        except Exception as exc:
            raise QQError(f"{type(exc).__name__}: {exc}") from exc

        body = self._parse_body(raw)
        # 官方明确说明：业务失败时 HTTP 仍可能是 200，必须看 code
        code = body.get("code")
        if code not in (None, 0):
            raise QQError(f"业务错误 {code}：{body.get('message', '')}")
        return body

    @staticmethod
    def _parse_body(raw: str) -> dict:
        """把响应体解析成 dict，**失败一律 QQError**。

        以前这里是裸的 `json.loads(raw)`：代理页 / CDN 错误页 / 网关页常常是
        **HTTP 200 + text/html**，于是抛 `json.JSONDecodeError` 穿过 `send()` 和
        `handle_event()`（它们只接 `QQError`），一路冒到 `run()` 被当成"断开"——
        一个坏响应就能拆掉整条 WebSocket 会话，而且那一轮的"发送失败要回滚记忆"也不会执行。
        """
        text = (raw or "").strip()
        if not text:
            return {}
        try:
            body = json.loads(text)
        except ValueError as exc:
            raise QQError(f"响应不是合法 JSON：{text[:200]!r}") from exc
        if not isinstance(body, dict):
            raise QQError(f"响应不是 JSON 对象：{text[:200]!r}")
        return body

    def access_token(self, *, force: bool = False) -> str:
        if not force and self._token and time.time() < self._token_expire:
            return self._token
        body = self._request(
            "POST",
            "/app/getAppAccessToken",
            {"appId": self.app_id, "clientSecret": self.client_secret},
            auth=False,
        )
        token = body.get("access_token")
        if not token:
            raise QQError(f"没拿到 access_token：{body}")
        try:
            expires = float(body.get("expires_in", 7200))
        except (TypeError, ValueError):
            expires = 7200.0
        self._token = str(token)
        self._token_expire = time.time() + max(60.0, expires - 120.0)  # 提前 2 分钟换
        print(f"[鉴权] 已获取 access_token（{expires:.0f} 秒后过期，自动续期）")
        return self._token

    def gateway_url(self) -> str:
        try:
            url = self._request("GET", "/gateway").get("url") or ""
        except QQError as exc:
            print(f"[网关] 获取失败（{exc}），改用默认地址", file=sys.stderr)
            return FALLBACK_WS
        if not url:
            return FALLBACK_WS
        return url if url.endswith("/") else url + "/"

    # ══ 发消息 ═══════════════════════════════════════════════
    def _next_seq(self, msg_id: str) -> int:
        seq = self._seqs.get(msg_id, 0) + 1
        self._seqs[msg_id] = seq
        if len(self._seqs) > 500:
            self._seqs.pop(next(iter(self._seqs)))
        return seq

    def send(self, kind: str, openid: str, text: str, msg_id: str | None) -> SendResult:
        """把一段话发出去。返回"哪几段真的送达了"——发送失败不抛异常，但必须如实上报。

        以前这里把 QQError 吞掉只打一行日志，调用方以为发送成功了：
        分段里某一段失败 → 用户看到半截；整条失败 → 她的记忆里却记着说过。

        发送前还有三道闸（借鉴 QQ-agent 的sender）：
          · 每会话限频：分钟/小时上限，超了就等（不硬丢，但要留痕）
          · 随机间隔：连着发好几段时不要顶着平台的风控线
          · 同文去重：8 秒内不重复发同一句话——但**只在发送成功后才记账**，
            提前记账会把合法重试误判成重复、静默丢消息
        """
        path = (
            f"/v2/groups/{openid}/messages"
            if kind == "group"
            else f"/v2/users/{openid}/messages"
        )
        result = SendResult()
        pieces = chunk_text(text)
        if not pieces:
            return result
        key = f"{kind}:{openid}"

        # ★ 平台硬限是**按 msg_id 计数**的：群 5 条 / 单聊 4 条（不是"每条回复"）。
        # 所以这里检查的是"这个 msg_id 已经发出去几条"，把**系统通知**也算进同一本账——
        # 否则"5 段回复 + 1 条终端服务通知"= 6 条，第 6 条必被平台拒掉（审查 P1-21）。
        if msg_id:
            limit = PASSIVE_REPLY_LIMIT_GROUP if kind == "group" else PASSIVE_REPLY_LIMIT_C2C
            already = int(self._replies_sent.get(msg_id, 0))
            if already >= limit:
                reason = f"同一个 msg_id 已经回过 {already} 条（平台上限 {limit}），这条不发了"
                print(f"[发送] {reason}", file=sys.stderr)
                result.failed.append((pieces[0], reason))
                return result
            budget = limit - already
            if len(pieces) > budget:
                keep = pieces[: budget - 1] if budget > 1 else []
                tail = "".join(pieces[budget - 1 :])
                pieces = [*keep, tail]
                print(
                    f"[发送] 这条回复超过平台上限（同一 msg_id 最多 {limit} 条，"
                    f"已回 {already} 条），已把尾巴并成一段（{len(pieces)} 段）",
                    file=sys.stderr,
                )

        # 同文去重（8 秒）：**按 msg_id 分桶**。
        # 以前不看 msg_id：两个不同的 msg_id（平台侧两条独立的被动回复）文本相同且在 8 秒内，
        # 第二条不会发出却进了 result.sent → 调用方以为"用户看见了"，而屏幕上什么都没有。
        # 现在只有"同一个 msg_id"（真·平台重推）才跳过；没有 msg_id 的调用路径
        # （脚本/自测/直连）保持原来的"同会话 8 秒内同文不重发"。
        # 兼容读取：老版本存的是 2 元组 (text, ts)，别让热重载/长跑进程炸掉
        stored = self._sent_recently.get(key)
        last_text, last_ts, last_mid = ("", 0.0, "")
        if isinstance(stored, tuple):
            if len(stored) >= 3:
                last_text, last_ts, last_mid = stored[0], stored[1], stored[2]
            elif len(stored) == 2:
                last_text, last_ts = stored
        same_source = str(last_mid) == str(msg_id or "")
        if same_source and pieces == [last_text] and time.time() - last_ts < 8.0:
            print("[发送] 同一个 msg_id 在 8 秒内发过同样的话，跳过（防重复回复）")
            result.sent.extend(pieces)
            return result

        # 平台硬限已经在上面的 budget 里处理过了（含系统通知），这里不再重复
        for index, piece in enumerate(pieces):
            if index:  # 分段之间隔一下，别顶着风控
                time.sleep(random.uniform(
                    self.settings.runtime.send_min_interval,
                    self.settings.runtime.send_max_interval,
                ))
            blocked = self._rate_limit_wait(key)
            if blocked:
                # 注释一直说"超了就等（不硬丢）"，但以前是**直接丢这一段**：
                # 分段回复会缺一段，用户看到半截。现在按注释等窗口过去（有上限），
                # 等不起才如实上报为失败（调用方会回滚那句记忆）。
                wait = self._send_slot_delay(key)
                if wait <= 0:
                    blocked = ""
                elif wait <= RATE_LIMIT_MAX_WAIT:
                    print(f"[发送] {blocked}，等 {wait:.1f}s 再发这一段", file=sys.stderr)
                    time.sleep(wait)
                    blocked = self._rate_limit_wait(key)
                if blocked:
                    print(f"[发送] {blocked}，这句等不起先不发了（{piece[:20]}…）", file=sys.stderr)
                    result.failed.append((piece, blocked))
                    continue
            body: dict = {"msg_type": MSG_TYPE_TEXT, "content": piece}
            if msg_id:
                body["msg_id"] = msg_id
                body["msg_seq"] = self._next_seq(msg_id)
            if self._wakeup_once and kind == "c2c":
                # 官方「互动召回消息」：只有单聊有意义，额度更紧但更合规
                body["is_wakeup"] = True
            if self.dry_run:
                print(f"[dry-run] {kind} → {openid}\n   {piece}")
                result.sent.append(piece)
                continue
            try:
                self._request("POST", path, body)
                result.sent.append(piece)
                self._note_send(key)          # 成功才记账
                if msg_id:
                    # 平台按 msg_id 计数被动回复条数，成功一条记一条
                    self._replies_sent[str(msg_id)] = int(self._replies_sent.get(str(msg_id), 0)) + 1
                    if len(self._replies_sent) > 500:
                        self._replies_sent.pop(next(iter(self._replies_sent)))
                if len(pieces) == 1:
                    self._sent_recently[key] = (piece, time.time(), str(msg_id or ""))
            except QQError as exc:
                result.failed.append((piece, str(exc)))
                print(f"[发送失败] {kind} → {openid}：{exc}", file=sys.stderr)
        if result.failed:
            print(
                f"[发送失败] 这条回复只送出去 {result.progress} 段"
                f"（共 {len(text)} 字）",
                file=sys.stderr,
            )
        return result

    def _rate_limit_wait(self, key: str) -> str:
        """返回非空 = 被限频拦下了（附原因）。"""
        now = time.time()
        stamps = [t for t in self._send_times.get(key, []) if now - t < 3600]
        self._send_times[key] = stamps
        runtime = self.settings.runtime
        per_min = len([t for t in stamps if now - t < 60])
        if per_min >= runtime.send_max_per_minute:
            return f"每分钟最多 {runtime.send_max_per_minute} 条"
        if len(stamps) >= runtime.send_max_per_hour:
            return f"每小时最多 {runtime.send_max_per_hour} 条"
        return ""

    def _note_send(self, key: str) -> None:
        self._send_times.setdefault(key, []).append(time.time())

    def _send_slot_delay(self, key: str) -> float:
        """还要等多久才有发送额度。0 = 现在就能发。

        与 `_rate_limit_wait()`（返回人话原因）配合使用：先看原因，再算等待时间。
        """
        now = time.time()
        stamps = [t for t in self._send_times.get(key, []) if now - t < 3600]
        runtime = self.settings.runtime
        waits: list[float] = []
        recent = sorted(t for t in stamps if now - t < 60)
        cap_min = max(1, int(runtime.send_max_per_minute))
        if len(recent) >= cap_min:
            # 额度在"第 (len-cap+1) 条"发出后腾出来
            waits.append(recent[len(recent) - cap_min] + 60.0 - now)
        cap_hour = max(1, int(runtime.send_max_per_hour))
        if len(stamps) >= cap_hour:
            waits.append(sorted(stamps)[len(stamps) - cap_hour] + 3600.0 - now)
        return max(0.0, max(waits)) if waits else 0.0

    # ══ 聚批：被触发后等她说完再一起处理 ══════════════════════
    def _may_speak_up(self, group: str) -> bool:
        """她自己插嘴的日上限（每个群单独算）。用掉一次就 +1。

        在**决定要说**的时候就记账，而不是等发送成功——宁可少说一句，
        也不要因为发送失败而变成"额度没扣、下条还说"。
        """
        hub = brain_mod.get_default(self.settings)
        key = f"speak_up:{group}:{hub.clock.now().date().isoformat()}"
        used = int(hub.store.state_get(key, 0) or 0)
        if used >= max(0, int(self.settings.runtime.speak_max_per_day)):
            return False
        hub.store.state_set(key, used + 1)
        return True

    def _enqueue_batch(
        self,
        target: str,
        user_id: str,
        speaker: str,
        content: str,
        msg_id: str,
        at_bot: bool,
        role: str = "",
    ) -> None:
        """把触发消息放进批次，并（第一次时）定下截止时间。

        **截止时间不因为新消息而顺延**：滑动窗口在热闹的群里会让她永远等下去，
        等于永远不开口。
        """
        batch = self._batch.get(target)
        if batch is None:
            batch = {
                "items": [],
                "deadline": time.time() + max(0.2, self.settings.runtime.wake_delay_seconds),
            }
            self._batch[target] = batch
        batch["items"].append({
            "user_id": user_id,
            "speaker": speaker,
            "content": content,
            "msg_id": msg_id,
            "at_bot": at_bot,
            "role": role,
        })

    def _next_batch_deadline(self) -> float | None:
        return min((b["deadline"] for b in self._batch.values()), default=None)

    def _flush_batches(self) -> None:
        """到点的批次，一次性处理。"""
        now = time.time()
        for target, batch in list(self._batch.items()):
            if now < batch["deadline"]:
                continue
            del self._batch[target]
            self._run_batch(target, batch["items"])

    def _run_batch(self, target: str, items: list[dict]) -> None:
        if not items:
            return
        if len(items) > 1:
            print(f"[聚批] 这 {len(items)} 句一起处理（{self.settings.runtime.wake_delay_seconds:.1f}s 窗口）")
        # 前面几句只当"听见了"：落库进她的聊天记录，让她在上下文里看得到，
        # 但只有最后一句触发回复（被动回复窗口也是按它算的）。
        for item in items[:-1]:
            try:
                handle_message(
                    item["content"], item["user_id"], settings=self.settings, scene="group",
                    group_id=target, speaker_name=item["speaker"], is_handler=False,
                    log_only=True, mid=item["msg_id"],
                )
            except Exception as exc:  # noqa: BLE001 —— 旁听落库失败不该影响这一批的回复
                print(f"[旁听失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        last = items[-1]
        self.respond_safely(
            "group", target, last["user_id"], last["speaker"],
            last["content"], last["msg_id"], at_bot=last["at_bot"],
            role=str(last.get("role") or ""),
        )

    def _flush_pending_batches(self) -> None:
        """退出前把还在窗口里的批次处理掉（不等 deadline）。

        systemd 停止/重启（改完 persona.toml 的常规操作）时，命中 2 秒窗口的那条消息
        以前会**永久消失**：不回复、也不落聊天记录。这里在收尾路径上主动冲一次。
        """
        if not self._batch:
            return
        pending = list(self._batch.items())
        self._batch.clear()
        print(f"[退出] 还有 {len(pending)} 个批次没处理，先处理完再走")
        for target, batch in pending:
            try:
                self._run_batch(target, batch["items"])
            except Exception as exc:  # noqa: BLE001
                print(f"[退出] 批次处理失败：{type(exc).__name__}: {exc}", file=sys.stderr)

    # ══ 事件 ═════════════════════════════════════════════════
    # 同一条消息可能被推两次：
    #   · 平台重推（网络抖动 / resume 补发）
    #   · 群里开了"接收所有消息"后，全量事件与 @ 事件可能同时到
    # 所以去重表里存的是"回复过没有"：
    #   已经回过的再收到 = 重复；只旁听过的 @ 版来了，还是要回。
    def is_duplicate(self, msg_id: str, *, reply_worthy: bool = False) -> bool:
        """返回 True = 这条不用再处理了。"""
        now = time.time()
        while self._seen and now - next(iter(self._seen.values()))[0] > DEDUP_WINDOW:
            self._seen.popitem(last=False)
        if not msg_id:
            return False
        prev = self._seen.get(msg_id)
        if prev is not None:
            _ts, prev_replied = prev
            if prev_replied:
                return True  # 已经回过了 → 重复
            if not reply_worthy:
                return True  # 上次也只是旁听 → 重复
            # 上次只旁听、这次是按 @ 推过来的 → 放行，让她回
        self._seen[msg_id] = (now, reply_worthy or bool(prev and prev[1]))
        return False

    def handle_event(self, event_type: str, payload: dict) -> None:
        # 群里开了 / 关了"接收所有消息"
        if event_type in (EVENT_GROUP_MSG_RECEIVE, EVENT_GROUP_MSG_REJECT):
            group = str(payload.get("group_openid") or "")
            on = event_type == EVENT_GROUP_MSG_RECEIVE
            if group:
                brain_mod.get_default(self.settings).store.state_set(
                    f"fullmode:{group}", 1 if on else 0
                )
            print(
                f"[全量] 群 {group[:10]}… {'开启' if on else '关闭'}了「接收所有消息」"
                + ("——她从此听得见群里每一句话" if on else "")
            )
            return
        if event_type not in (EVENT_GROUP_AT, EVENT_GROUP_MESSAGE, EVENT_C2C):
            # 撤回：被撤回的消息**再也不发给模型**（工作记忆、群聊记录、记忆整理都不带它）
            # ⚠ 官方 v2 文档里我没有找到撤回事件的正式名字，所以这里按名字宽松匹配：
            #   平台真推了就用得上，不推也无害（这条路径不会影响其它事件）。
            if "RECALL" in event_type.upper():
                mid = str(
                    payload.get("message_id")
                    or payload.get("msg_id")
                    or payload.get("id")
                    or ""
                )
                # 撤回要限定在同一个会话里：msg_id 理论上可能跨群重复（P3-1），
                # 撤回一个群的消息不该把另一个群里同 id 的消息一起标掉。
                hub_rc = brain_mod.get_default(self.settings)
                gid = str(payload.get("group_openid") or "")
                session = (
                    f"i{hub_rc.instance_no}-g{gid[:12]}-"
                    f"{hub_rc.clock.now().strftime('%Y%m%d')}"
                    if gid
                    else ""
                )
                hit = hub_rc.store.mark_recalled(mid, session_id=session or None)
                print(f"[撤回] {mid[:16]}… 已标记 {hit} 条（不再进入她的记忆）")
            return

        msg_id = str(payload.get("id") or "")
        author = payload.get("author") or {}
        user_id = str(author.get("user_openid") or author.get("id") or "unknown")
        speaker = str(author.get("username") or "").strip()
        kind = "group" if event_type in (EVENT_GROUP_AT, EVENT_GROUP_MESSAGE) else "c2c"
        target = str(payload.get("group_openid") or "") if kind == "group" else user_id
        if not target:
            print(f"[跳过] {event_type} 事件缺少目标 id", file=sys.stderr)
            return

        # 全量模式下，没点到她的消息也要"听得见"（这是她看聊天记录的来源）。
        # 判断有没有 @ 她：@ 事件本身就算；全量事件看 mentions 里有没有机器人。
        mentions = payload.get("mentions") or []
        at_bot = event_type == EVENT_GROUP_AT or any(
            bool(m.get("bot")) for m in mentions if isinstance(m, dict)
        )
        if self.is_duplicate(msg_id, reply_worthy=(kind == "c2c" or at_bot)):
            print(f"[去重] 忽略重复推送的 {msg_id[:16]}…")
            return

        content = extract_content(payload)
        if not content:
            return

        who = "群" if kind == "group" else "私聊"
        print(f"[收到·{who}{'' if at_bot else '·旁听'}] {user_id[:12]}…：{content[:60]}")
        role = str(author.get("member_role") or "")

        # 记住最近活跃的群 —— 她主动发消息到群里时用它当目标
        if kind == "group":
            brain_mod.get_default(self.settings).set_last_group(target)
            # 平台每条群消息都带昵称。先用昵称认人（等比自我介绍再覆盖），
            # 否则她每次都得问"你是哪位"——群里没人会喜欢这个。
            if speaker:
                hub0 = brain_mod.get_default(self.settings)
                if hub0.remember_person(user_id, speaker):
                    print(f"[认得] 群里有人叫「{hub0.known_people().get(user_id, '')}」")
            # 活跃成员表：谁常来、谁好久没露面（她说话的分寸不一样）
            brain_mod.get_default(self.settings).note_activity(user_id)

        # 先看是不是指令（官方 openid 与 QQ 号不互通，所以有 /bind 兜底）
        if self.handle_command(kind, target, content, user_id, msg_id, role):
            return

        # ── 群聊：先决定这条要不要让她开口（档位见 giftia/trigger.py）──
        if kind == "group":
            # 这个群已经有一批在等 → 直接并入：**不再逐条判定、也不重置倒计时**。
            # 否则"@她之后又顺口补一句"会被当成旁听漏掉，或者把倒计时越推越远。
            if target in self._batch:
                self._enqueue_batch(target, user_id, speaker, content, msg_id, at_bot, role)
                print("[聚批] 并入正在等的那一批")
                return
            speak, why = decide_speak(
                self.settings.runtime.response_tier,
                at_bot=at_bot,
                text=content,
                keywords=tuple(self.settings.runtime.keywords or ()),
                roll=random.random(),
            )
            if not speak:
                # 没命中 → 当场标已读（只旁听）：不建会话、不调模型，零 token
                handle_message(
                    content, user_id, settings=self.settings, scene="group",
                    group_id=target, speaker_name=speaker, log_only=True, mid=msg_id,
                )
                print(f"[旁听] {why} → 不出声")
                return
            # 不是被 @ 的（她自己想插一句）→ 受"每个群每天最多几次"限制。
            # 3 档是按**每条消息**掷骰子的，热闹的群里一天能掷出几十次，
            # 不封顶就是刷屏。
            if not at_bot and not self._may_speak_up(target):
                handle_message(
                    content, user_id, settings=self.settings, scene="group",
                    group_id=target, speaker_name=speaker, log_only=True, mid=msg_id,
                )
                print("[闭嘴] 今天自己插嘴的次数用完了，先听着")
                return
            if self.settings.runtime.wake_delay_seconds <= 0:
                # 不聚批：立刻处理（想让她"听到就回"就把 wake_delay_seconds 设成 0）
                self.respond_safely(kind, target, user_id, speaker, content, msg_id, at_bot=at_bot, role=role)
                return
            self._enqueue_batch(target, user_id, speaker, content, msg_id, at_bot, role)
            print(
                f"[待答] {why} → 等 {self.settings.runtime.wake_delay_seconds:.1f}s"
                " 把这段时间里的话一起处理"
            )
            return

        # 私聊：直接处理（群聊走聚批，到点了由 _run_batch 转到这里）
        self.respond_safely(kind, target, user_id, speaker, content, msg_id)

    def _respond(
        self,
        kind: str,
        target: str,
        user_id: str,
        speaker: str,
        content: str,
        msg_id: str,
        at_bot: bool = True,
        role: str = "",
    ) -> None:
        """真正处理一条消息并回复。

        聚批结束后由 `_run_batch` 调用：那时"这一批"的前几句已经作为旁听落库了，
        所以模型的工作记忆里看得到上下文，而回复是按这一批的**最后一句**发的
        （被动回复窗口也是按它算的）。
        """
        hub = brain_mod.get_default(self.settings)
        owner = self.owner_id()
        if not owner and self.settings.runtime.auto_bind:
            # 自动认主：省掉 /bind（官方 openid 无法预先知道，只能这样认人）
            if kind == "c2c":
                # 私聊：未认证时只有管理员/体验号能找她，第一个说话的当然是他
                hub.set_handler_id(user_id)
                owner = user_id
                print(f"[自动认主·私聊] 搭档 = {user_id}（想换人：/unbind）")
            elif role == "owner":
                # 群聊：只有群主 @ 她时才认（陌生人冒充不了群主）
                hub.set_handler_id(user_id)
                owner = user_id
                print(f"[自动认主·群主] 搭档 = {user_id}（想换人：/unbind）")

        # 谁是搭档？
        #   · 已绑定 → 就是绑定的人
        #   · 未绑定 + 私聊 → 说话的就是他（本地 CLI 与"还没认人"时都是这样）
        #   · 未绑定 + 群聊 → 不是他（群里必须靠群主自动认主或 /bind）
        is_handler = (user_id == owner) if owner else (kind == "c2c")

        if kind == "group" and not is_handler:
            # 群里的其他人：先让她认人（这是她"认得群里的人"的唯一来源），再决定出不出声
            if hub.learn_name(content, user_id, owner=False):
                print(f"[认得] 群里有人叫「{hub.known_people().get(user_id, '')}」")
            if not self.settings.runtime.group_stranger_reply:
                # 名字记下了，话也留个记录：她"听得见"，只是不搭话
                handle_message(
                    content, user_id, settings=self.settings, scene="group",
                    group_id=target, speaker_name=speaker, is_handler=False,
                    log_only=True, mid=msg_id,
                )
                print("[跳过] 群里其他人说话，按配置保持安静")
                return
            # 以"客人"身份礼貌回一句：只看这个群里公开发生过的事，
            # 碰不到你和她之间的私聊记忆，也不会推进你们的关系
            guest_reply = handle_message(
                content, user_id, settings=self.settings, scene="group",
                group_id=target, speaker_name=speaker, is_handler=False, mid=msg_id,
            )
            print(f"[{self.settings.persona.name}·客人] {guest_reply}")
            gresult = self.send(kind, target, guest_reply, msg_id)
            hub_g = brain_mod.current()
            if hub_g is not None:
                hub_g.note_reply_delivery(gresult.delivered)
            return

        if not is_handler:
            # 私聊里的别人：戏内挡回去，不进你们的记忆
            self.send(kind, target, DEFLECT, msg_id)
            return

        # ★ 整个项目的接缝：平台消息 → 她
        # scene 多注入一段群聊约束（主要是隐私：群里别人也看得到）；
        # group_id / speaker_name 决定这条记忆"归谁"——群聊必须传，
        # 否则她分不清谁在哪个群说了什么。
        reply = handle_message(
            content,
            user_id,
            settings=self.settings,
            scene="group" if kind == "group" else "c2c",
            group_id=target if kind == "group" else "",
            speaker_name=speaker,
            is_handler=True,
            mid=msg_id,
        )
        print(f"[{self.settings.persona.name}] {reply}")
        result = self.send(kind, target, reply, msg_id)

        # 告诉她"这句话到底送出去没有"——没送出去的话要从记忆里撤回
        hub = brain_mod.current()
        if hub is not None:
            hub.note_reply_delivery(result.delivered)

        # 她的"系统通知"（例如终端服务预告）另发一条，不混进她的台词。
        # 同一个 msg_id 的再次回复会自动用 msg_seq=2，避免被平台按重复消息拒掉。
        if hub is not None:
            for note in hub.notices:
                if note.startswith(("[终端服务]", "[记忆整理]", "[记忆强化]")):
                    self.send(kind, target, note, msg_id)

    def respond_safely(
        self,
        kind: str,
        target: str,
        user_id: str,
        speaker: str,
        content: str,
        msg_id: str | None,
        *,
        at_bot: bool = True,
        role: str = "",
    ) -> None:
        """`_respond` 的包壳：**一条消息处理失败不该掀掉整条连接**。

        以前 `_respond` / `_run_batch` 没有 try/except，任何异常（写库失败、
        编码问题、模型层意外异常）都会冒到 `run()`，被当成"断开"→ 整条 WebSocket
        重连，而用户那句话永远没有回复。这里兜住并如实报错。
        """
        try:
            self._respond(
                kind, target, user_id, speaker, content, msg_id,
                at_bot=at_bot, role=role,
            )
        except Exception as exc:  # noqa: BLE001 —— 兜底就是它的职责
            print(
                f"[处理失败] {kind} → {target}：{type(exc).__name__}: {exc}（连接保持，继续收消息）",
                file=sys.stderr,
            )
            try:
                self.send(kind, target, "……抱歉，我刚才走神了。你再说一遍好不好？", msg_id)
            except Exception:
                pass

    # ══ 认人：官方 OpenID 与真实 QQ 号不互通，只能靠一句 /bind ══
    def owner_id(self) -> str:
        """优先用 --owner / QQ_OWNER_OPENID；没配就读运行时绑定（/bind 写进去的）。"""
        if self.owner_openid:
            return self.owner_openid
        try:
            return brain_mod.get_default(self.settings).handler_id
        except Exception:
            return ""

    def handle_command(
        self,
        kind: str,
        target: str,
        text: str,
        user_id: str,
        msg_id: str | None,
        role: str = "",
    ) -> bool:
        """返回 True 表示这条消息已被指令消费掉，不再交给她。

        **身份闸门是白名单语义**（审查 P0-3a）：没有搭档时只放行 `/bind` `/help` `/unbind`，
        其余指令一律婉拒、不执行也不写库。以前闸门写成 `if owner and user_id != owner`，
        `owner` 为空时条件为 False → 群里任何成员一条 `/profile` 就能把搭档视角的
        全部私聊记忆当众念出来（还能 `/forget` 掉记忆、`/impress` 写东西）。
        """
        parts = text.split()
        if not parts or not parts[0].startswith("/"):
            return False
        cmd = parts[0].lower()

        hub = brain_mod.get_default(self.settings)
        owner = self.owner_id()

        # ① 单聊专属的指令：它们是"你看她的记忆"的视角，不该当众输出
        if cmd in ("/profile", "/impress", "/forget", "/reconcile") and kind != "c2c":
            self.send(kind, target, "……这个我们私下说。", msg_id)
            return True

        # ② 白名单：没有搭档时只允许这三条（否则"未绑定 = 放行一切"）
        if not owner and cmd not in ("/bind", "/help", "/unbind"):
            self.send(
                kind, target,
                "……这个我现在做不了。得先让我知道你是我的搭档——发一句 /bind。",
                msg_id,
            )
            return True

        # ③ 已绑定时：只有搭档本人能下指令（群里的指令一定不是搭档本人，见 ①）
        if owner and user_id != owner and cmd not in ("/help", "/bind"):
            self.send(kind, target, DEFLECT, msg_id)
            return True

        if cmd == "/help":
            self.send(kind, target, COMMANDS, msg_id)
            return True

        if cmd == "/bind":
            if self.owner_openid:
                self.send(kind, target, "……这个我已经记下了，不用再绑一次。", msg_id)
                return True
            if owner and owner != user_id:
                self.send(kind, target, "……抱歉，我已经有搭档了。", msg_id)
                return True
            if owner == user_id:
                self.send(kind, target, "……嗯，我认得你。", msg_id)
                return True
            # 群里的 /bind 只有**群主**可以用——陌生人冒充不了群主，
            # 否则"解绑后群里任何 member 一条 /bind 就能抢注"。
            if kind == "group" and role not in ("owner", "群主"):
                self.send(kind, target, "……这个得群主来说才行。", msg_id)
                return True
            hub.set_handler_id(user_id)
            print(f"[绑定] 搭档 = {user_id}")
            self.send(kind, target, "……嗯。我记住了。\n以后我只认你一个人说话。", msg_id)
            return True

        if cmd == "/unbind":
            if owner and owner != user_id:
                self.send(kind, target, "……这个不能由你来说。", msg_id)
                return True
            hub.set_handler_id("")
            self.send(kind, target, "……好。那我先站远一点。", msg_id)
            return True

        if cmd == "/name":
            if owner and user_id != owner:
                self.send(kind, target, DEFLECT, msg_id)
                return True
            new_name = parts[1].strip() if len(parts) > 1 else ""
            if not new_name:
                self.send(kind, target, f"……你现在叫我「{hub.handler_name}」。", msg_id)
                return True
            hub.set_handler_name(new_name)
            self.send(kind, target, f"……好。{new_name}。我记住了。", msg_id)
            return True

        if cmd in ("/profile", "/impress", "/forget", "/reconcile"):
            if owner and user_id != owner:
                self.send(kind, target, DEFLECT, msg_id)
                return True
            if cmd == "/profile":
                who = parts[1].strip() if len(parts) > 1 else ""
                text = hub.profile_text(who)
            elif cmd == "/impress":
                bits = " ".join(parts[1:]).strip().split(maxsplit=1)
                if len(bits) < 2:
                    text = "……用法是「/impress 名字 你要我记住的话」。"
                else:
                    mid = hub.add_impression(bits[0], bits[1])
                    text = (
                        f"……嗯，我记下了：{bits[0]}——{bits[1]}" if mid
                        else "……名字和内容都得有，我才记得住。"
                    )
            elif cmd == "/forget":
                if not (len(parts) > 1 and parts[1].strip().isdigit()):
                    text = "……用法是「/forget 编号」，编号在 /profile 里能看到。"
                elif hub.forget_memory(int(parts[1].strip())):
                    text = f"……好，#{parts[1].strip()} 那件事，我不再想了。"
                else:
                    text = f"……我这儿没有 #{parts[1].strip()} 这条。"
            else:
                # 整理可能要几十秒：**先把话说出去**，再去干活，别让她"卡住不回"
                self.send(kind, target, "……好，我去把这阵子的记忆理一理。", msg_id)
                res = hub.reconcile_memories(force=True)
                text = (
                    f"理完了。有 {res['merged']} 条是同一件事，我并在一起了。"
                    if res.get("merged") else "理完了——没有重复的，我记性还行。"
                )
            self.send(kind, target, text, msg_id)
            return True

        if cmd == "/status":
            if owner and user_id != owner:
                self.send(kind, target, DEFLECT, msg_id)
                return True
            self.send(kind, target, hub.status_text(), msg_id)
            return True

        return False  # 不认识的斜杠消息当作普通聊天，交给她

    def _outreach_targets(self) -> list[tuple[str, str]]:
        """这次主动消息发去哪：[(kind, openid)]。由 [outreach] target 决定。"""
        mode = str(self.settings.outreach.target or "owner").strip().lower()
        out: list[tuple[str, str]] = []
        owner = self.owner_id()
        group = brain_mod.get_default(self.settings).last_group_id
        if mode in ("owner", "both") and owner:
            out.append(("c2c", owner))
        if mode in ("group", "both") and group:
            out.append(("group", group))
        return out

    def _maybe_outreach(self) -> None:
        """她主动来找你（或群里）。

        只在主线程里跑（Giftia 不是线程安全的），所以由 WebSocket 循环定时调用，
        而不是另开一个后台线程。

        **一批只判一次限流**：以前把 `can_send` 放在 for 循环里，第一轮发完私聊立刻记账，
        第二轮就被 `min_gap_hours` 挡掉 → `target=both` 时群里那条**静默丢失**，
        额度也只扣 1 条（README 承诺"两边都发、每次占 2 条"）。现在：
        循环外判一次（日上限 + 30 天额度 + 最小间隔），一批里的多个目标共享这次判定；
        每个真正发出去的目标各自记账（所以 `both` 真的占 2 条额度）。
        """
        if not self.settings.outreach.enabled:
            return
        hub = brain_mod.get_default(self.settings)
        targets = self._outreach_targets()
        if not targets:
            return  # 还没认人 / 还没进过群，不知道发给谁
        # 一批只判一次：按"这一批要发几条"预留额度，之后同批目标不再被判间隔挡掉
        extra = max(0, len(targets) - 1)
        if not can_send(self.settings, hub.store, hub.clock.now(), batch_extra=extra):
            return
        for index, (kind, target) in enumerate(targets):
            if index and not can_send(
                self.settings, hub.store, hub.clock.now(), ignore_gap=True
            ):
                break  # 同批里后面的目标：额度还在就发，间隔不再重复判
            which = "owner" if kind == "c2c" else "group"
            plan_, text = hub.compose_outreach(record=False, target=which)
            if not plan_ or not text.strip():
                continue
            # 主动消息也走 send()：**三道闸（同文去重 / 限频 / 分段）必须一起生效**。
            # 以前这里直连 _request，绕过了分钟/小时限频——而主动消息恰恰是最容易触发
            # 平台风控的那一类（"防平台风控"的闸不能只保护被动回复）。
            if self.settings.outreach.use_wakeup and kind == "c2c":
                self._wakeup_once = True   # send() 会把它写进请求体
            try:
                result = self.send(kind, target, text, None)
            finally:
                self._wakeup_once = False
            if not result.ok:
                print(
                    f"[主动发送失败·{which}] {result.progress} 段未送达：{result.failed[:1]}",
                    file=sys.stderr,
                )
                continue
            hub.note_outreach_sent(text, keep_in_history=(kind == "c2c"))
            where = "私聊" if kind == "c2c" else "群里"
            print(f"[主动·{where}·{plan_.reason_label}] {text}")

    # ══ WebSocket 会话 ═══════════════════════════════════════
    def identify(self, ws: WebSocket) -> None:
        ws.send_text(
            json.dumps(
                {
                    "op": OP_IDENTIFY,
                    "d": {
                        "token": f"QQBot {self.access_token()}",
                        "intents": INTENT_GROUP_AND_C2C,
                        "shard": [0, 1],
                        "properties": {"$os": "windows", "$browser": "giftia", "$device": "giftia"},
                    },
                }
            )
        )
        print("[鉴权] 已发送 identify（订阅群@与单聊事件）")

    def resume(self, ws: WebSocket) -> None:
        ws.send_text(
            json.dumps(
                {
                    "op": OP_RESUME,
                    "d": {
                        "token": f"QQBot {self.access_token()}",
                        "session_id": self.session_id,
                        "seq": self.seq,
                    },
                }
            )
        )
        print(f"[恢复] 尝试 resume（seq={self.seq}）")

    def session_loop(self) -> None:
        url = self.gateway_url()
        ws = WebSocket(url, timeout=15)
        ws.connect()
        print(f"[连接] {url}")

        hello = json.loads(ws.recv_text(timeout=15))
        if hello.get("op") != OP_HELLO:
            raise QQError(f"期望 op10 hello，收到：{str(hello)[:200]}")
        interval = float(hello.get("d", {}).get("heartbeat_interval", 45000)) / 1000.0
        print(f"[心跳] 周期 {interval:.1f} 秒")

        if self.session_id and self.seq is not None:
            self.resume(ws)
        else:
            self.identify(ws)

        next_beat = time.time() + interval
        next_outreach = time.time() + self.outreach_interval
        try:
            while not self._stopping:
                now = time.time()
                if now >= next_outreach:
                    try:
                        self._maybe_outreach()
                    except Exception as exc:  # 主动联系出问题不该拖垮连接
                        print(f"[主动联系异常] {type(exc).__name__}: {exc}", file=sys.stderr)
                    next_outreach = now + self.outreach_interval
                # 到点的聚批：一次处理这段时间里攒下的话
                self._flush_batches()
                if now >= next_beat:
                    ws.send_text(json.dumps({"op": OP_HEARTBEAT, "d": self.seq}))
                    next_beat = now + interval
                    continue
                try:
                    wait = max(0.5, min(next_beat - now, 5.0))
                    due = self._next_batch_deadline()
                    if due is not None:
                        # 有批次在等：别睡过头，到点就得起来处理
                        wait = max(0.2, min(wait, due - now))
                    raw = ws.recv_text(timeout=wait)
                except WSTimeout:
                    continue

                msg = json.loads(raw)
                op = msg.get("op")
                if msg.get("s") is not None:
                    self.seq = msg["s"]

                if op == OP_DISPATCH:
                    event = msg.get("t")
                    data = msg.get("d") or {}
                    if event == "READY":
                        self.session_id = data.get("session_id")
                        self.bot_name = (data.get("user") or {}).get("username", "")
                        print(f"[就绪] 机器人「{self.bot_name}」已上线，等她说话吧")
                    elif event == "RESUMED":
                        print("[恢复] 会话已恢复，遗漏事件已补发")
                    else:
                        # 一条事件处理失败（写库/编码/模型层意外异常）不该拆掉整条连接：
                        # 以前没有这层兜底，异常会冒到 run() 被当成"断开"→ 重连，
                        # 而那条消息永远没有回复。
                        try:
                            self.handle_event(str(event), data)
                        except QQReconnect:
                            raise
                        except Exception as exc:  # noqa: BLE001
                            print(
                                f"[事件失败] {event}：{type(exc).__name__}: {exc}"
                                "（连接保持，继续收消息）",
                                file=sys.stderr,
                            )
                elif op == OP_HEARTBEAT_ACK:
                    pass
                elif op == OP_RECONNECT:
                    raise QQReconnect("服务端要求重连")
                elif op == OP_INVALID_SESSION:
                    self.session_id, self.seq = None, None
                    raise QQReconnect("会话已失效，需要重新 identify")
        except WSClosed as exc:
            text = str(exc)
            if "4009" not in text:          # 4009 = 连接过期，官方允许 resume
                self.session_id, self.seq = None, None
            raise
        finally:
            # 收尾：先把还在 2 秒窗口里的批次处理掉（systemd 停止/重启时不丢消息），
            # 再关连接、再落库退出。
            self._flush_pending_batches()
            ws.close()

    def run(self) -> None:
        self.install_signal_handlers()
        backoff = 5.0
        try:
            while not self._stopping:
                try:
                    self.session_loop()
                    backoff = 5.0
                except KeyboardInterrupt:
                    self.request_stop()
                except Exception as exc:
                    if self._stopping:
                        break
                    print(f"[断开] {type(exc).__name__}: {exc}；{backoff:.0f} 秒后重连",
                          file=sys.stderr)
                    # 分段等待，这样收到停止信号能立刻退出，不用等满退避时间
                    deadline = time.time() + backoff
                    while time.time() < deadline and not self._stopping:
                        time.sleep(0.5)
                    backoff = min(60.0, backoff * 2)
        finally:
            brain_mod.shutdown_default()
            print("[退出] 已停止。")

    # ══ 干跑：不需要凭据 ═════════════════════════════════════
    def dry_run_demo(self) -> None:
        print("干跑模式：不发任何网络请求，只验证「平台事件 → 她 → 回复」这条链路")
        print(f"（她在这里用的是临时数据目录，不会碰你正式的记忆）\n")
        print(f"她的状态：剩余寿命 {self.settings.lifespan.effective_hours():.0f} 小时（配置值）")
        print(f"模型：{'mock（离线）' if self.settings.model.is_mock else self.settings.model.name}\n")
        samples = [
            (EVENT_C2C, {
                "id": "ROBOT1.0_dryrun_a",
                "author": {"user_openid": "DRYRUN_USER", "username": "搭档"},
                "content": "在吗？今天加班到很晚，回来路上下了点雨",
                "message_type": 0,
                "timestamp": "2026-10-07T22:00:00+08:00",
            }),
            (EVENT_C2C, {
                "id": "ROBOT1.0_dryrun_a",     # 故意重复，验证去重
                "author": {"user_openid": "DRYRUN_USER"},
                "content": "在吗？今天加班到很晚，回来路上下了点雨",
                "message_type": 0,
            }),
            (EVENT_GROUP_AT, {
                "id": "ROBOT1.0_dryrun_b",
                "author": {"user_openid": "DRYRUN_USER"},
                "group_openid": "DRYRUN_GROUP",
                "content": "<@!123456> 你还记得我上次说什么吗",
                "message_type": 0,
            }),
        ]
        for event_type, payload in samples:
            print(f"── 模拟事件 {event_type}")
            self.handle_event(event_type, payload)
            print()
        print("干跑结束。接线无误的话，填好凭据后直接去掉 --dry-run 即可。")


def main() -> int:
    load_env_file()
    parser = argparse.ArgumentParser(
        description="Giftia 的 QQ 官方机器人适配器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true", help="不发网络请求，只验证接线")
    parser.add_argument("--mock-llm", action="store_true", help="她的对话用离线假模型")
    parser.add_argument("--owner", default=os.environ.get("QQ_OWNER_OPENID", ""),
                        metavar="OPENID", help="只服务这个 user_openid（其他人不进记忆）")
    parser.add_argument("--api-base", default=os.environ.get("QQ_API_BASE", API_BASE))
    args = parser.parse_args()

    app_id = os.environ.get("QQ_BOT_APPID", "").strip()
    secret = os.environ.get("QQ_BOT_SECRET", "").strip()

    if args.dry_run:
        # 干跑必须隔离：否则会往正式的 data/giftia.db 里写进测试对话
        import dataclasses
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="giftia-qq-dryrun-"))
        settings = dataclasses.replace(load_settings(force_mock=True), root=tmp)
        bot = QQBot(
            app_id or "dry",
            secret or "dry",
            owner_openid=args.owner,
            api_base=args.api_base,
            dry_run=True,
            settings=settings,
        )
        bot.dry_run_demo()
        return 0

    if not app_id or not secret:
        print(
            "缺少凭据。请先在 .env 里填好（或设成环境变量）：\n"
            "  QQ_BOT_APPID=你的AppID\n"
            "  QQ_BOT_SECRET=你的AppSecret\n"
            "  QQ_OWNER_OPENID=你的 user_openid（可选，建议填：一个她只服务一个搭档）\n\n"
            "还没申请机器人？去 https://q.qq.com 创建，开发设置里把事件订阅方式选 WebSocket。\n"
            "想先验证接线：python adapters/qq_official.py --dry-run",
            file=sys.stderr,
        )
        return 2

    bot = QQBot(app_id, secret, owner_openid=args.owner, api_base=args.api_base,
                mock_llm=args.mock_llm)
    if not bot.owner_openid:
        # owner_openid 是配置里的；也可能早就用 /bind 或自动认主写进库里了
        try:
            bound = brain_mod.get_default(bot.settings).handler_id
        except Exception:
            bound = ""
        if bound:
            print(f"[提示] 搭档已认：{bound[:12]}…（想换人：/unbind）", file=sys.stderr)
        elif bot.settings.runtime.auto_bind:
            print(
                "[提示] 还没认人：在 QQ 里私聊她说第一句话，她就把你记成搭档了。\n"
                "        （群里需要你是群主，或者手动发 /bind。想换人：/unbind）",
                file=sys.stderr,
            )
        else:
            print("[提示] 还没认人：在 QQ 里私聊她发一句 /bind。", file=sys.stderr)
    bot.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
