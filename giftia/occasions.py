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
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

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


def _parse_event_date(raw: str, today: date | None = None) -> date | None:
    """从记忆的 `event_date` 文本里认出那一天。

    容忍常见写法（模型/整理链路写出来的不一定是严格 ISO）：

    - `2026-10-11`、`2026-10-11T09:00:00`、`20261011`、` 2026-10-11 `
    - `2026/10/11`、`2026.10.11`、`2026年10月11日`
    - `10月20号` / `10月20日`（缺年份 → 按 `today` 补，已过则算明年）
    - `下周三` / `这周三` / `周三` / `星期六`（相对 `today` 解析）

    认不出来就返回 `None`，**不报错**——那天她不会想起来，仅此而已。
    `today` 只用于相对/缺年份的写法；不传时按系统今天算。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    base = today or date.today()

    # 1) ISO（含 `2026-10-11T09:00`）与紧凑写法 `20261011`
    for candidate in (text[:10], text):
        try:
            return date.fromisoformat(candidate)
        except (TypeError, ValueError):
            pass
    if re.fullmatch(r"\d{8}", text):
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        except ValueError:
            return None

    # 2) 数字分隔符：2026/10/11、2026.10.11、2026年10月11日、10月20号、10/20
    m = re.search(
        r"(?:(\d{4})\s*[-/.年]\s*)?(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*[日号]?", text
    )
    if m:
        year_s, month_s, day_s = m.group(1), m.group(2), m.group(3)
        try:
            month, day = int(month_s), int(day_s)
            if year_s:
                return date(int(year_s), month, day)
            guess = date(base.year, month, day)
            # 只写了"10月20号"：过了就算明年的那一天
            return guess if guess >= base else date(base.year + 1, month, day)
        except ValueError:
            return None

    # 3) 相对星期：下周三 / 这周三 / 周三 / 星期天 / 下星期日
    m = re.search(r"(下下|下|这|本)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天1-7])", text)
    if m:
        week = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
        target = int(m.group(2)) if m.group(2) in "1234567" else week.get(m.group(2))
        if target is None:
            return None
        prefix = m.group(1) or ""
        delta = (target - base.isoweekday()) % 7
        if prefix == "下":
            delta += 7
        elif prefix == "下下":
            delta += 14
        return base + timedelta(days=delta)

    return None


def _handler_view(store: Store) -> list[tuple[str, str]]:
    """搭档视角：他自己的私事 + 他本人的私聊（**不含群**）。

    与 `brain.memory_view`、`recycle._handler_view`、`dream` 的默认口径一致。
    """
    handler_id = str(store.state_get("handler_id", "") or "")
    view: list[tuple[str, str]] = [("private", "")]
    if handler_id:
        view.append(("person", handler_id))
    return view


def find_occasions(
    settings: Settings,
    store: Store,
    *,
    now: datetime,
    instance_no: int,
    days_known: float,
    mark: bool = True,
    view: list[tuple[str, str]] | None = None,
) -> list[Occasion]:
    """今天有什么值得她记着的日子。mark=False 只查看、不记账（给预览用）。

    view 决定"能看到谁的大日子"——**和记忆检索用同一道闸门**。
    否则你私聊里说的"下周三考试"会变成她在群里惦记的事，等于当众说出来。
    不传 → 搭档视角（`_handler_view`），与 `brain.memory_view` / `/days` 同一口径。
    """
    if not settings.occasions.enabled:
        return []
    if view is None:
        view = _handler_view(store)
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
    for mem in store.active_memories(instance_no, view=view):
        when = _parse_event_date(mem.get("event_date") or "", today)
        if when is None:
            continue
        delta = (when - today).days
        if delta < 0 or delta > settings.occasions.big_day_lookahead_days:
            continue
        # 去重键带上"今天"：前一天提过「明天考试」之后，正日子当天还要再提一次
        # （“明天”和“今天”是两句话）；否则只提一次就永远不说了。
        token = f"{mem['id']}:{when.isoformat()}:{today.isoformat()}"
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

    # ── 3) 事后回访：他昨天/前天办的那件事，办得怎么样 ──────
    # 「今天要考驾照」只是提醒，「昨天考得怎么样」才像真的在惦记。
    # 这是"她主动一点"里最动人的一块，而且数据早就有（记忆的 event_date）。
    for mem in store.active_memories(instance_no, view=view):
        when = _parse_event_date(mem.get("event_date") or "", today)
        if when is None:
            continue
        back = (today - when).days
        if back <= 0 or back > settings.occasions.follow_up_days:
            continue
        # 同样带上"今天"：事后第 1 天问过之后，第 2 天还要能再回访一次。
        token = f"back:{mem['id']}:{when.isoformat()}:{today.isoformat()}"
        if token in told:
            continue
        what = str(mem.get("summary", "")).strip()
        when_word = "昨天" if back == 1 else f"{back} 天前"
        found.append(
            Occasion(
                kind="follow_up",
                title=f"{when_word}那件事：{what}",
                hint=(
                    f"他{when_word}办了这件事：{what}\n"
                    "你可以问一句结果怎么样——但要像真的关心，不是来验收的。"
                    "他要是没接话，就不要再追。"
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
    store: Store,
    instance_no: int,
    days_known: float,
    milestones: tuple[int, ...] | list[int],
    *,
    view: list[tuple[str, str]] | None = None,
) -> str:
    """给 /days 用的时间线。

    `view` = 这份清单读到谁的"大日子"：不传 → **搭档视角**
    `[("private",""), ("person", handler_id)]`（不含群，也不含别人的私聊）。
    `/days` 是搭档在自己终端上看的东西，别人的私聊约定与其它群的事不该列在这里（P1-11）。

    `view` 是**可选关键字参数**，调用点（`brain.days_text()`）不用改。
    """
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
    effective_view = view if view is not None else _handler_view(store)
    today = date.today()
    big = [
        (m, when)
        for m in store.active_memories(instance_no, view=effective_view)
        if (when := _parse_event_date(m.get("event_date") or "", today)) is not None
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
