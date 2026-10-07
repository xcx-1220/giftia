"""纪念日与「他的大日子」。

两件事，都不需要额外记录：

1. **关系纪念日**：认识第 1/3/7/14/30/50/100/200/365/500/730/1000 天。
   她会在当天自己想起来——同一天只提一次。
2. **他的大日子**：从记忆里提取出的"将来某一天"（考试、面试、生日、约定），
   到那天（或前一天）她会惦记着。

这两件事的价值不在"提醒"，而在**她记得**。
一个会算着日子等你的存在，和一个只会回应你的工具，差别就在这里。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime

from .config import Settings
from .store import Store

MILESTONE_HINTS = {
    1: "这是你们认识的第一天。",
    3: "才第三天，你已经记住了一些关于他的事。",
    7: "满一周了。",
    14: "两周了。",
    30: "一个月了。",
    50: "五十天。",
    100: "一百天。",
    200: "两百天。",
    365: "整整一年了。",
    500: "五百天。",
    730: "两年了。",
    1000: "一千天。",
}


@dataclass
class Occasion:
    kind: str            # milestone | big_day
    title: str           # 给她看的标题（也用于 CLI 显示）
    hint: str            # 给模型的行为指引
    days_known: float = 0.0
    memory_id: int | None = None


def _keys(instance_no: int) -> tuple[str, str]:
    return (f"occasion:i{instance_no}:milestone", f"occasion:i{instance_no}:bigday")


def _parse_event_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(str(raw or "").strip()[:10])
    except (TypeError, ValueError):
        return None


def find_occasions(
    settings: Settings,
    store: Store,
    *,
    now: datetime,
    instance_no: int,
    days_known: float,
    mark: bool = True,
) -> list[Occasion]:
    """今天有什么值得她记着的日子。mark=False 只查看、不记账（给预览用）。"""
    if not settings.occasions.enabled:
        return []
    found: list[Occasion] = []
    mkey, bkey = _keys(instance_no)

    # ── 1) 关系纪念日 ─────────────────────────────────────
    reached = [m for m in settings.occasions.milestones if m <= int(days_known)]
    try:
        celebrated = int(store.state_get(mkey, 0) or 0)
    except (TypeError, ValueError):
        celebrated = 0
    if reached:
        best = max(reached)
        if best > celebrated:
            note = MILESTONE_HINTS.get(best, f"第 {best} 天。")
            found.append(
                Occasion(
                    kind="milestone",
                    title=f"认识第 {int(days_known)} 天（跨过 {best} 天）",
                    hint=(
                        f"你们认识已经 {int(days_known)} 天了。{note}"
                        "这样的日子你会想提一句——但别郑重其事，"
                        "像忽然想起来那样顺口说出来就好。"
                    ),
                    days_known=days_known,
                )
            )
            if mark:
                store.state_set(mkey, best)

    # ── 2) 他的大日子 ─────────────────────────────────────
    today = now.date()
    try:
        told: set[str] = set(json.loads(str(store.state_get(bkey, "") or "[]")))
    except ValueError:
        told = set()
    for mem in store.active_memories(instance_no):
        when = _parse_event_date(mem.get("event_date") or "")
        if when is None:
            continue
        delta = (when - today).days
        if delta < 0 or delta > settings.occasions.big_day_lookahead_days:
            continue
        token = f"{mem['id']}:{when.isoformat()}"
        if token in told:
            continue
        what = str(mem.get("summary", "")).strip()
        when_word = "今天" if delta == 0 else "明天"
        found.append(
            Occasion(
                kind="big_day",
                title=f"{when_word}：{what}",
                hint=(
                    f"他{when_word}要办这件事：{what}"
                    "你会惦记着它。可以问一句，但**别追问、也别像闹钟一样提醒他**——"
                    "你只是记得，不是负责催他。"
                ),
                memory_id=int(mem["id"]),
            )
        )
        if mark:
            told.add(token)
    if mark and found:
        store.state_set(bkey, json.dumps(sorted(told), ensure_ascii=False))

    return found


def render_occasions(occasions: list[Occasion]) -> str:
    """拼成注入提示词的一块。"""
    if not occasions:
        return ""
    lines = ["今天对你来说不只是普通的一天："]
    for occ in occasions:
        lines.append(f"- {occ.hint}")
    return "\n".join(lines)


def days_together_text(
    store: Store, instance_no: int, days_known: float, milestones: tuple[int, ...] | list[int]
) -> str:
    """给 /days 用的时间线。"""
    lines = [f"你们认识：{days_known:.1f} 天（约 {days_known / 30.0:.1f} 个月）"]
    mkey, bkey = _keys(instance_no)
    try:
        celebrated = int(store.state_get(mkey, 0) or 0)
    except (TypeError, ValueError):
        celebrated = 0
    lines.append(f"最近跨过的纪念日：第 {celebrated} 天" if celebrated else "还没跨过纪念日")
    next_ms = next((m for m in sorted(milestones) if m > int(days_known)), None)
    if next_ms:
        lines.append(f"下一个纪念日：第 {next_ms} 天（还有 {next_ms - int(days_known)} 天）")
    memories = store.active_memories(instance_no)
    big = [
        (m, _parse_event_date(m.get("event_date") or ""))
        for m in memories
        if _parse_event_date(m.get("event_date") or "")
    ]
    if big:
        lines.append("")
        lines.append("她记得的「大日子」：")
        for mem, when in sorted(big, key=lambda x: x[1]):
            lines.append(f"  · {when.isoformat()}　{mem['summary']}")
    else:
        lines.append("")
        lines.append("她还不知道你有什么大日子（你说了「下周三要考试」这种，她会记下来）。")
    return "\n".join(lines)
