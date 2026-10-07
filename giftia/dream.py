"""做梦：她闲置时把记忆碎片揉成一场梦。

技术上是复用 `blur_text()`：把最近的记忆条目**重度模糊**，再交给模型揉成一段梦。
梦作为独立条目存进 `dreams` 表，第二天注入提示词（"你昨晚梦见……"）。

梦不是记忆：不进检索、不占保留率、不参与退化。
但它让"遗忘"这件事显得温柔一点——细节没了，剩下的东西会自己重新组合。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .clock import Clock
from .config import Settings
from .llm import BaseLLM
from .memory import blur_text
from .store import Store


@dataclass
class Dream:
    id: int
    content: str
    created_ts: str
    told: bool = False

    def is_fresh(self, now: datetime, within_hours: float) -> bool:
        try:
            born = datetime.fromisoformat(self.created_ts)
        except (TypeError, ValueError):
            return False
        return (now - born).total_seconds() / 3600.0 <= within_hours


def _last_dream(store: Store, instance_no: int) -> dict | None:
    dreams = store.recent_dreams(instance_no, limit=1)
    return dreams[0] if dreams else None


def should_dream(settings: Settings, store: Store, clock: Clock, instance_no: int) -> bool:
    """现在该不该做梦：有足够的记忆、且离上一场梦够久。"""
    if not settings.dream.enabled:
        return False
    memories = store.active_memories(instance_no)
    if len(memories) < settings.dream.min_memories:
        return False
    last = _last_dream(store, instance_no)
    if not last:
        return True
    try:
        born = datetime.fromisoformat(str(last.get("created_ts", "")))
    except (TypeError, ValueError):
        return True
    hours = (clock.now() - born).total_seconds() / 3600.0
    return hours >= settings.dream.min_hours_between


def make_dream(
    settings: Settings,
    store: Store,
    clock: Clock,
    llm: BaseLLM,
    instance_no: int,
    view: list[tuple[str, str]] | None = None,
) -> Dream | None:
    """把最近的记忆碎片揉成一场梦。失败就返回 None（做梦不该影响正常对话）。

    view 决定"拿谁的事做梦"。默认只拿搭档的（private + 他本人的私聊）——
    梦是她内心的东西，别人的私事不该进她的梦，
    否则她在群里讲梦就等于把别人的私事当众说了出去（真机测试抓到过这条）。
    """
    if view is None:
        view = [("private", ""), ("person", str(store.state_get("handler_id", "") or ""))]
    memories = store.active_memories(instance_no, view=view)
    if len(memories) < settings.dream.min_memories:
        return None

    recent = memories[-settings.dream.fragments :]
    fragments: list[str] = []
    for mem in recent:
        text, _ = blur_text(
            str(mem.get("summary", "")),
            settings.dream.blur,
            seed=int(mem["id"]) * 31 + 7,
        )
        if text.strip():
            fragments.append(text.strip())
    if not fragments:
        return None

    try:
        content = llm.write_dream(fragments, name=settings.persona.name).strip()
    except Exception:
        return None
    if not content:
        return None

    now_iso = clock.iso()
    dream_id = store.add_dream(
        instance_no, now_iso, content, [int(m["id"]) for m in recent]
    )
    return Dream(id=dream_id, content=content, created_ts=now_iso, told=False)


def latest_untold(
    settings: Settings, store: Store, clock: Clock, instance_no: int
) -> Dream | None:
    """还没讲给你听的新梦（太久了就不再翻出来讲）。"""
    if not settings.dream.enabled:
        return None
    for row in reversed(store.recent_dreams(instance_no, limit=5)):
        if int(row.get("told") or 0):
            continue
        dream = Dream(
            id=int(row["id"]),
            content=str(row["content"]),
            created_ts=str(row["created_ts"]),
            told=False,
        )
        if dream.is_fresh(clock.now(), settings.dream.tell_within_hours):
            return dream
        return None
    return None


def dream_text(store: Store, instance_no: int, limit: int = 5) -> str:
    """给 /dream 看的梦簿。"""
    dreams = store.recent_dreams(instance_no, limit=limit)
    if not dreams:
        return "她还没做过梦（记忆攒够几条、隔一段时间就会做一场）。"
    lines = [f"她做过的梦（最近 {len(dreams)} 场）：", ""]
    for row in reversed(dreams):
        stamp = str(row["created_ts"])[:16].replace("T", " ")
        told = "已讲过" if int(row.get("told") or 0) else "还没讲"
        lines.append(f"〔{stamp}　{told}〕")
        lines.append(f"　{row['content']}")
        lines.append("")
    return "\n".join(lines).strip()
