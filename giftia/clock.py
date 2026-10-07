"""虚拟时钟。

Giftia 的寿命是 81920 小时——按真实时间你没法验证"记忆退化"和"回收日"有没有做对。
所以时间在这里是可加的：/time +720 就等于让她的世界过去一个月。
虚拟时间会持久化，重启后继续走。
"""

from __future__ import annotations

from datetime import datetime, timedelta


class Clock:
    META_KEY = "virtual_offset_hours"

    def __init__(self, store, offset_hours: float = 0.0) -> None:
        raw = store.meta_get(self.META_KEY)
        if raw is None:
            self._offset = float(offset_hours)
            self._store = store
            self._persist()
        else:
            self._store = store
            try:
                self._offset = float(raw)
            except (TypeError, ValueError):
                self._offset = float(offset_hours)

    # ── 查询 ──────────────────────────────────────────────
    @property
    def offset_hours(self) -> float:
        return self._offset

    def now(self) -> datetime:
        """当前虚拟时间（本地朴素时间）。"""
        return datetime.now() + timedelta(hours=self._offset)

    def iso(self, dt: datetime | None = None) -> str:
        return (dt or self.now()).isoformat(timespec="seconds")

    def parse(self, value: str) -> datetime:
        return datetime.fromisoformat(value)

    # ── 操作 ──────────────────────────────────────────────
    def advance(self, hours: float) -> datetime:
        self._offset += float(hours)
        self._persist()
        return self.now()

    def reset(self) -> datetime:
        self._offset = 0.0
        self._persist()
        return self.now()

    def describe_offset(self) -> str:
        if abs(self._offset) < 1e-9:
            return "未快进（与真实时间同步）"
        sign = "已快进" if self._offset > 0 else "已回拨"
        total = abs(self._offset)
        days, rest = divmod(total, 24)
        if days >= 1:
            return f"{sign} {days:.0f} 天 {rest:.0f} 小时"
        return f"{sign} {rest:.1f} 小时"

    def _persist(self) -> None:
        self._store.meta_set(self.META_KEY, f"{self._offset:.6f}")
