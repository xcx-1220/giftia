"""虚拟时钟。

Giftia 的寿命是 81920 小时——按真实时间你没法验证"记忆退化"和"回收日"有没有做对。
所以时间在这里是可加的：/time +720 就等于让她的世界过去一个月。
虚拟时间会持久化，重启后继续走。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

# 虚拟时钟的合法幅度（≈100 年）。超出/非有限值一律拒绝或自愈：
# `nan` 一旦写进 meta，之后每次启动都会在 `now()` 上抛异常，连 `/time reset` 都敲不进去。
MAX_OFFSET_HOURS = 100 * 365 * 24.0


def _usable_offset(value: float) -> bool:
    """这个偏移能不能算出一个合法时间（不溢出、不非有限）。"""
    if not math.isfinite(value) or abs(value) > MAX_OFFSET_HOURS:
        return False
    try:
        datetime.now() + timedelta(hours=value)
    except (OverflowError, ValueError):
        return False
    return True


class Clock:
    META_KEY = "virtual_offset_hours"

    def __init__(self, store, offset_hours: float = 0.0) -> None:
        raw = store.meta_get(self.META_KEY)
        self._store = store
        if raw is None:
            self._offset = float(offset_hours) if _usable_offset(float(offset_hours)) else 0.0
            self._persist()
            return
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = float(offset_hours)
        if not _usable_offset(value):
            # 老版本可能已经把 nan/inf 写进库里了：读回来时自愈，别让整个进程起不来
            print(f"[时钟] 库里的虚拟偏移不可用（{raw!r}），已重置为 0")
            self._offset = 0.0
            self._persist()
            return
        self._offset = value

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
        """推进虚拟时钟。

        **先验证再持久化**（审查 P2）：以前是"先写库、再算 now()"，
        于是 `/time nan` 会把 `nan` 写进 meta，之后每次启动都在算 `now()` 时抛异常，
        连 `/time reset` 都敲不进去（要手工改库才能救回来）。
        """
        target = self._offset + float(hours)
        if not _usable_offset(target):
            raise ValueError(f"虚拟时钟不接受这个偏移：{target!r} 小时")
        self._offset = target
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
