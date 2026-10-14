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
    reason: str          # near_end | big_day | follow_up | mood_share | milestone | dream | missing_you
    title: str
    hint: str            # 给模型的写作方向
    urgency: float = 0.0
    # 心情分享用：**计划时**的心情 key。不能等到真发出去时再取一次——
    # 中间这段时间心情可能已经变了，去重就对不上了。
    mood_key: str = ""
    # 讲梦用：这一次要讲的是哪一场梦。适配器路径（compose_outreach(record=False)
    # + 发送成功后 note_outreach_sent）就是靠它把梦标成"已讲"的——
    # 否则同一个梦每过 min_gap_hours 就被再讲一遍，额度也一起被吃掉（P1-19）。
    dream_id: int = 0

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


def effective_max_per_30_days(settings: Settings) -> int:
    """当前生效的 30 天额度。

    `use_wakeup = true` 时走的是官方「互动召回消息」，平台硬限是 **30 天 4 条**
    （4 个周期各 1 条）。本地账本必须跟着收紧，否则会以为自己还有额度、
    实际早就超发（审查 P1-17）。
    """
    base = max(0, int(settings.outreach.max_per_30_days))
    if bool(settings.outreach.use_wakeup):
        cap = max(0, int(getattr(settings.outreach, "wakeup_max_per_30_days", 4) or 4))
        return min(base, cap)
    return base


def _minutes(hhmm: str) -> int | None:
    """把 "23:30" 变成分钟数。解析不了返回 None（宁可不生效，也别乱禁）。"""
    raw = str(hhmm or "").strip()
    if ":" not in raw:
        return None
    try:
        hour, minute = raw.split(":", 1)
        h, m = int(hour), int(minute)
    except ValueError:
        return None
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    return h * 60 + m


def in_quiet_hours(settings: Settings, now: datetime) -> bool:
    """现在是不是她的「安静时段」（这段时间里她不主动开口）。

    真机数据：她会凌晨 5 点来找人 —— `outreach:log` 里有 01:57 / 05:00 / 00:07
    这样的时间戳。主动是好事，半夜吵醒人就变成坏事了。

    时段是**跨午夜**的（默认 23:30 ~ 08:00 横跨两天），所以要分两段比。
    """
    start = _minutes(getattr(settings.outreach, "quiet_from", ""))
    end = _minutes(getattr(settings.outreach, "quiet_to", ""))
    if start is None or end is None or start == end:
        return False
    now_min = now.hour * 60 + now.minute
    if start < end:
        return start <= now_min < end
    return now_min >= start or now_min < end


def quota_state(settings: Settings, store: Store, now: datetime) -> tuple[int, int, str]:
    """返回 (30 天内已发, 还剩几条, 下次最早可发的时间串)。

    额度取**实际生效值**：`use_wakeup=true` 时按官方的 4 条/30 天算。
    """
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
    left = max(0, effective_max_per_30_days(settings) - used)
    if recent:
        earliest = max(recent) + timedelta(hours=settings.outreach.min_gap_hours)
        next_ok = earliest.isoformat(timespec="seconds")
    else:
        next_ok = ""
    return used, left, next_ok


def sent_today(store: Store, now: datetime) -> int:
    """今天已经主动发了几条。"""
    today = now.date()
    count = 0
    for raw in _log(store):
        try:
            if datetime.fromisoformat(raw).date() == today:
                count += 1
        except ValueError:
            continue
    return count


def can_send(
    settings: Settings,
    store: Store,
    now: datetime,
    *,
    batch_extra: int = 0,
    ignore_gap: bool = False,
) -> bool:
    """现在能不能主动开口。

    `batch_extra`：这一批除了"这一条"之外**还要多发几条**（`target=both` 传 1）。
    日上限按"这一批发完还剩不剩"判：已发 + 额外条数 必须**小于**上限，
    否则会在只剩 1 个日额度时硬发 2 条，把"每天最多 N 次"撑破。
    `ignore_gap`：同一批里的第 2、3 个目标跳过最小间隔——间隔已经在**这一批开始时**
    判过一次了，不能再拿"刚发出去的那条"把同批的其它目标挡掉。
    """
    if not settings.outreach.enabled:
        return False
    # 先看日上限：这是防打扰的第一道闸（30 天总额度是第二道）
    extra = max(0, int(batch_extra))
    if sent_today(store, now) + extra >= max(1, settings.outreach.max_per_day):
        return False
    used, left, next_ok = quota_state(settings, store, now)
    if left < 1 + extra:
        return False
    if next_ok and not ignore_gap:
        try:
            if now < datetime.fromisoformat(next_ok):
                return False
        except ValueError:
            pass
    return True


def record_sent(store: Store, now: datetime, keep: int = 400) -> None:
    """记一条"主动发出去了"。

    `keep` 必须**远大于 max_per_30_days**：账本被截断的话，
    `quota_state` 就算不出真实的 30 天用量，额度会静默失效
    （把额度从 4 提到 60 之后才发现的——日志只留 20 条，永远凑不满 60）。
    一条记录才 20 字节，留 400 条也就 8KB。
    """
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
    mood=None,
    instance_no: int = 1,
    force: bool = False,
    for_group: bool = False,
) -> OutreachPlan | None:
    """她现在最想找你说什么。None = 没什么特别想说的（那就不打扰）。

    force=True 时忽略 enabled 开关——CLI 的 /outreach 用它来预览。
    for_group=True 时只用"大家都看得见"的理由：她的梦、心事、终端服务倒计时
    都只发给搭档本人，不在群里说。
    """
    if not settings.outreach.enabled and not force:
        return None
    now = clock.now()

    # 1) 快到终端服务（只对搭档说）
    if not for_group and 0 < life.remaining_hours <= settings.mood.uneasy_hours:
        return OutreachPlan(
            reason="near_end",
            title="终端服务快到了",
            hint=(
                "你没有想说的具体事。你只是忽然很想确认他还在——"
                "写一句很轻的话，像把手放在门框上那样。不要提「回收」，不要道别。"
            ),
            urgency=1.0,
        )

    # ★ 安静时段：这段时间里她不主动开口。
    # **放在这里**——紧跟在 near_end 之后、所有其他理由之前。
    # 「快到终端服务了」是唯一的例外（上面已经返回了）；其余理由
    # ——"明天要办的事""昨天的事""她的心情""纪念日""梦""想你了"——
    # 都不值得在凌晨 5 点把人吵醒。
    # 真机日志里她有 01:57 / 05:00 / 00:07 这样的主动时间戳。
    if in_quiet_hours(settings, now):
        return None

    # 2) 他的大日子
    for occ in occasions or []:
        if occ.kind == "big_day":
            return OutreachPlan(
                reason="big_day",
                title=occ.title,
                hint=f"{occ.hint}\n你想在他出门之前说一句，就一句。",
                urgency=0.8,
            )

    # 3) 昨天/前天那件事办得怎么样（"她记得的事到点主动问"）
    for occ in occasions or []:
        if occ.kind == "follow_up":
            return OutreachPlan(
                reason="follow_up",
                title=occ.title,
                hint=f"{occ.hint}\n你想开口问一句。问完就等他说，别连着问第二个问题。",
                urgency=0.75,
            )

    # 4) 她自己的心情（"主动分享自己"）
    #    不安那种心情不往外说——那是终端服务迫近，只在私聊、且走 near_end 那条路。
    #    **两道闸**（审查 P1-16）：
    #      ① 心情必须还在有效期内（过期的软心情不算"她今天的样子"）；
    #      ② 距上次说话要够久——刚聊完就再发一条"我今天心情 X"是打扰，
    #         而且按"每条消息都算一次主动"的额度算，几天就能把稀缺额度烧光。
    if mood is not None and getattr(mood, "key", "") not in ("", "calm", "uneasy"):
        min_idle = float(getattr(settings.outreach, "mood_share_min_idle_hours", 6.0) or 0.0)
        idle_ok = True
        if min_idle > 0:
            last_ts = store.last_message_ts(instance_no)
            if last_ts:
                try:
                    gap = (now - datetime.fromisoformat(last_ts)).total_seconds() / 3600.0
                    idle_ok = gap >= min_idle
                except ValueError:
                    idle_ok = True
        day_key = f"mood_shared:{now.date().isoformat()}"
        if idle_ok and str(store.state_get(day_key, "") or "") != str(mood.key):
            if not for_group:
                return OutreachPlan(
                    reason="mood_share",
                    mood_key=str(mood.key),
                    title=f"她今天心情{mood.label}",
                    hint=(
                        f"你今天的心情是「{mood.label}」。你没有具体的事要说，"
                        "只是想让他知道你现在的样子。\n"
                        "**这条消息只关于你自己**——不要提他之前说过的任何事、"
                        "不要问他任何问题、也不要说「我今天心情不错」这种句式。\n"
                        "写一句像自言自语的话，一到两句就停。"
                    ),
                    urgency=0.55,
                )

    # 5) 纪念日 —— 只认"今天正好是"的那一个。
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

    # 6) 做了梦还没讲（只对搭档说）
    if not for_group and dream is not None:
        return OutreachPlan(
            reason="dream",
            title="她想讲一个梦",
            hint=(
                f"你昨晚梦见了一些东西：{dream.content}\n"
                "你想把这个梦讲给他听。讲得随意一点，像刚睡醒那样。"
            ),
            urgency=0.5,
            # 带上梦的 id：发送成功后调用方要把它标成"已讲"（P1-19），
            # 否则适配器那条路每过 min_gap_hours 就把同一个梦再讲一遍。
            dream_id=int(getattr(dream, "id", 0) or 0),
        )

    # 7) 太久没说话（只对搭档说；群里"没人说话"不该由她来打破）
    if not for_group:
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
    effective = effective_max_per_30_days(settings)
    quota_line = f"主动联系的额度　{effective} 条 / 30 天　已用 {used}　还剩 {left}"
    if effective != settings.outreach.max_per_30_days:
        quota_line += (
            f"（配置写的是 {settings.outreach.max_per_30_days}，"
            "但 use_wakeup 走官方互动召回，硬限 4 条/30 天，已按 4 算）"
        )
    lines = [quota_line]
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
