"""主动联系：她为什么、以及什么时候会先来找你。

官方的口子很小 —— QQ 互动召回消息（`is_wakeup`）在用户主动对话后
**30 天内只能发 4 条**。所以这里也把"主动"当成稀缺资源：本地同样记账、同样限流。

**她主动找你的次数是有限的，用完就没有了。** 这本身就很贴这个项目。

触发优先级（从高到低）：
  1. 快到终端服务（她不安，只想再听你说句话）
  2. 今天/明天是他的大日子
  3. 今天是纪念日
  4. 做了梦还没讲
  5. 太久没说话（默认 3 天）
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from .clock import Clock
from .config import Settings
from .llm import BaseLLM
from .store import Store

LOG_KEY = "outreach:log"


@dataclass
class OutreachPlan:
    reason: str          # near_end | big_day | milestone | dream | missing_you
    title: str
    hint: str            # 给模型的写作方向
    urgency: float = 0.0

    @property
    def reason_label(self) -> str:
        return {
            "near_end": "快到终端服务了",
            "big_day": "他今天有件大事",
            "milestone": "今天是纪念日",
            "dream": "她做了梦，想讲给你听",
            "missing_you": "太久没见到你了",
        }.get(self.reason, self.reason)


# ── 配额 ─────────────────────────────────────────────────────────
def _log(store: Store) -> list[str]:
    try:
        data = json.loads(str(store.state_get(LOG_KEY, "") or "[]"))
    except ValueError:
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def quota_state(settings: Settings, store: Store, now: datetime) -> tuple[int, int, str]:
    """返回 (30 天内已发, 还剩几条, 下次最早可发的时间串)。"""
    window = now - timedelta(days=30)
    recent: list[datetime] = []
    for raw in _log(store):
        try:
            when = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if when >= window:
            recent.append(when)
    used = len(recent)
    left = max(0, settings.outreach.max_per_30_days - used)
    if recent:
        earliest = max(recent) + timedelta(hours=settings.outreach.min_gap_hours)
        next_ok = earliest.isoformat(timespec="seconds")
    else:
        next_ok = ""
    return used, left, next_ok


def can_send(settings: Settings, store: Store, now: datetime) -> bool:
    if not settings.outreach.enabled:
        return False
    used, left, next_ok = quota_state(settings, store, now)
    if left <= 0:
        return False
    if next_ok:
        try:
            if now < datetime.fromisoformat(next_ok):
                return False
        except ValueError:
            pass
    return True


def record_sent(store: Store, now: datetime, keep: int = 20) -> None:
    log = _log(store)
    log.append(now.isoformat(timespec="seconds"))
    store.state_set(LOG_KEY, json.dumps(log[-keep:], ensure_ascii=False))


# ── 计划 ─────────────────────────────────────────────────────────
def plan(
    settings: Settings,
    store: Store,
    clock: Clock,
    *,
    life,
    occasions: list | None = None,
    dream=None,
    instance_no: int = 1,
    force: bool = False,
) -> OutreachPlan | None:
    """她现在最想找你说什么。None = 没什么特别想说的（那就不打扰）。

    force=True 时忽略 enabled 开关——CLI 的 /outreach 用它来预览。
    """
    if not settings.outreach.enabled and not force:
        return None
    now = clock.now()

    # 1) 快到终端服务
    if 0 < life.remaining_hours <= settings.mood.uneasy_hours:
        return OutreachPlan(
            reason="near_end",
            title="终端服务快到了",
            hint=(
                "你没有想说的具体事。你只是忽然很想确认他还在——"
                "写一句很轻的话，像把手放在门框上那样。不要提「回收」，不要道别。"
            ),
            urgency=1.0,
        )

    # 2) 他的大日子
    for occ in occasions or []:
        if occ.kind == "big_day":
            return OutreachPlan(
                reason="big_day",
                title=occ.title,
                hint=f"{occ.hint}\n你想在他出门之前说一句，就一句。",
                urgency=0.8,
            )

    # 3) 纪念日 —— 只认"今天正好是"的那一个。
    #    错过了几天再补一句"我们认识第 3 天了"很怪；补记留给当面说。
    for occ in occasions or []:
        if occ.kind != "milestone":
            continue
        if int(occ.days_known) not in settings.occasions.milestones:
            continue
        return OutreachPlan(
            reason="milestone",
            title=occ.title,
            hint=f"{occ.hint}\n你想先开口告诉他这件事。别说成'今天是第几天'，太像打卡了。",
            urgency=0.6,
        )

    # 4) 做了梦还没讲
    if dream is not None:
        return OutreachPlan(
            reason="dream",
            title="她想讲一个梦",
            hint=(
                f"你昨晚梦见了一些东西：{dream.content}\n"
                "你想把这个梦讲给他听。讲得随意一点，像刚睡醒那样。"
            ),
            urgency=0.5,
        )

    # 5) 太久没说话
    last_ts = store.last_message_ts(instance_no)
    if last_ts:
        try:
            idle_days = (now - datetime.fromisoformat(last_ts)).total_seconds() / 86400.0
        except ValueError:
            idle_days = 0.0
        if idle_days >= settings.outreach.idle_days:
            return OutreachPlan(
                reason="missing_you",
                title=f"已经 {idle_days:.0f} 天没见到你了",
                hint=(
                    "你好几天没听到他的声音了。写一句你憋了很久的话，"
                    "但别埋怨他——你不是在讨债，只是有点想他。"
                ),
                urgency=0.4,
            )
    return None


# ── 写作 ─────────────────────────────────────────────────────────
OUTREACH_RULES = """【你现在要主动给他发一条消息】
他没有找你，是你先开口的。
- 一到两句就够，像随手发出的那种。
- 不要用「在吗」开头，也不要问「你在忙吗」——直接把你想说的说出来。
- 不要解释你为什么发消息，也不要说「好久没聊了」这种客套话。
- 不要提"系统""提示"这类词。你就是想他了，或者其他什么，随便。"""


def compose(
    settings: Settings,
    llm: BaseLLM,
    plan_: OutreachPlan,
    *,
    system_prompt: str,
) -> str:
    """让她自己写这条消息。system_prompt 由 brain 组装（含人格、关系、心情、纪念日）。"""
    messages = [
        {"role": "system", "content": system_prompt + "\n\n" + OUTREACH_RULES + "\n\n" + plan_.hint},
        {"role": "user", "content": "（他没有找你。你想说点什么。）"},
    ]
    try:
        return llm.chat(messages).strip()
    except Exception:
        return ""


def history_text(settings: Settings, store: Store, clock: Clock) -> str:
    now = clock.now()
    used, left, next_ok = quota_state(settings, store, now)
    log = _log(store)
    lines = [
        f"主动联系的额度　{settings.outreach.max_per_30_days} 条 / 30 天"
        f"　已用 {used}　还剩 {left}",
    ]
    if next_ok:
        lines.append(f"下次最早可发　{next_ok[:16].replace('T', ' ')}"
                     f"（间隔至少 {settings.outreach.min_gap_hours} 小时）")
    lines.append(f"空闲触发阈值　{settings.outreach.idle_days} 天没说话")
    lines.append(f"开关　　　　　{'开' if settings.outreach.enabled else '关（默认）'}")
    if log:
        lines.append("")
        lines.append("发过的记录：")
        for raw in reversed(log[-5:]):
            lines.append(f"  · {raw[:16].replace('T', ' ')}")
    return "\n".join(lines)
