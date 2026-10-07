"""关系：亲密度与阶段演进。

三个刻意的设计：

1. **不可刷**。亲密度按"天"增长——同一天说一百句也只算一次。
   奖励的是"你来了"，不是"你说了多少"。
2. **升级要两条腿**。同时满足亲密度和**认识天数**才升级：
   你再能聊，也换不来"认识三个月"。所以「亲近」这一档必须真的相处够久。
3. **默认不衰减**。她不会因为你忙就少喜欢你一点（想改有这个开关）。
   惩罚缺席的陪伴型产品，本质上是在拿愧疚感留人。

**回收换代时关系清零**——新的她不认识你。这是这套设定最疼的地方，
也是它区别于普通"好感度系统"的地方：你积累的东西，会随她一起被回收。
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

from .config import BondStage, Settings
from .store import Store


@dataclass
class Bond:
    affinity: float
    days_known: float
    interactions: int
    stage: BondStage
    next_stage: BondStage | None = None
    progressed: bool = False

    @property
    def progress(self) -> float:
        """距离下一档的进度（0..1）。没有下一档时恒为 1。"""
        nxt = self.next_stage
        if nxt is None:
            return 1.0
        cur = self.stage
        span_a = max(1e-6, nxt.min_affinity - cur.min_affinity)
        span_d = max(1e-6, nxt.min_days - cur.min_days)
        got_a = (self.affinity - cur.min_affinity) / span_a
        got_d = (self.days_known - cur.min_days) / span_d
        return max(0.0, min(1.0, min(got_a, got_d)))

    def missing(self) -> str:
        """还差什么才能进下一档 —— 只说缺的那一条，好让人知道该做什么。"""
        nxt = self.next_stage
        if nxt is None:
            return "已经到最后一档了。"
        need: list[str] = []
        if self.affinity < nxt.min_affinity:
            need.append(f"亲密度 {self.affinity:.0f}/{nxt.min_affinity:.0f}")
        if self.days_known < nxt.min_days:
            need.append(f"认识 {self.days_known:.1f}/{nxt.min_days:.0f} 天")
        return "还差：" + "、".join(need) if need else "随时可以升级。"


def stage_for(
    stages: tuple[BondStage, ...], affinity: float, days_known: float
) -> tuple[BondStage, BondStage | None]:
    """按"两条腿都满足"取最高一档，并返回下一档。"""
    ordered = sorted(stages, key=lambda s: (s.min_days, s.min_affinity))
    current = ordered[0]
    nxt: BondStage | None = None
    for stage in ordered:
        if affinity >= stage.min_affinity and days_known >= stage.min_days:
            current = stage
        elif nxt is None:
            nxt = stage
    # 如果有比 current 更高的档没满足，第一个没满足的就是下一档
    if nxt is None or (nxt.min_days, nxt.min_affinity) <= (current.min_days, current.min_affinity):
        nxt = next(
            (s for s in ordered if (s.min_days, s.min_affinity) > (current.min_days, current.min_affinity)),
            None,
        )
    return current, nxt


class BondTracker:
    """把关系状态存进 store 的 state 表（按实例编号分键）。"""

    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    # ── 键 ────────────────────────────────────────────────
    def _key(self, name: str, instance_no: int) -> str:
        return f"bond:i{instance_no}:{name}"

    def _get_float(self, name: str, instance_no: int, default: float = 0.0) -> float:
        raw = self.store.state_get(self._key(name, instance_no))
        try:
            return float(raw) if raw is not None else default
        except (TypeError, ValueError):
            return default

    # ── 读 ────────────────────────────────────────────────
    def load(self, now, instance_no: int, activated_at) -> Bond:
        affinity = max(0.0, min(100.0, self._get_float("affinity", instance_no)))
        interactions = int(self._get_float("interactions", instance_no))
        days_known = max(0.0, (now - activated_at).total_seconds() / 86400.0)
        stage, nxt = stage_for(self.settings.bond_stages, affinity, days_known)
        return Bond(
            affinity=affinity,
            days_known=days_known,
            interactions=interactions,
            stage=stage,
            next_stage=nxt,
        )

    # ── 写 ────────────────────────────────────────────────
    def _scale(self, gain: float, affinity: float) -> float:
        """递减增长：越亲近，同样的付出涨得越少。"""
        if not self.settings.relationship.diminish:
            return gain
        return gain * max(0.0, 1.0 - affinity / 100.0)

    def set_affinity(self, instance_no: int, value: float) -> None:
        """直接设定亲密度（调试、演示、以及测试用）。"""
        self.store.state_set(
            self._key("affinity", instance_no), f"{max(0.0, min(100.0, value)):.4f}"
        )

    def touch(
        self,
        now,
        instance_no: int,
        activated_at,
        *,
        text: str = "",
        tone: float = 0.0,
    ) -> Bond:
        """一次真实对话带来的关系变化。同一天只结算一次（这就是"不可刷"）。"""
        if not self.settings.relationship.enabled:
            return self.load(now, instance_no, activated_at)

        before = self.load(now, instance_no, activated_at)
        today = now.date().isoformat()
        last_day = self.store.state_get(self._key("last_day", instance_no))

        if last_day != today:
            gain = self.settings.relationship.gain_daily
            if abs(tone) >= self.settings.mood.tone_threshold:
                # 说了一件有情绪的事，比随口寒暄更靠近
                gain += self.settings.relationship.gain_emotion_bonus
            gained = self._scale(gain, before.affinity)
            self.store.state_set(
                self._key("affinity", instance_no),
                f"{min(100.0, before.affinity + gained):.4f}",
            )
            self.store.state_set(
                self._key("interactions", instance_no), str(before.interactions + 1)
            )
            self.store.state_set(self._key("last_day", instance_no), today)

        after = self.load(now, instance_no, activated_at)
        after.progressed = after.stage.key != before.stage.key
        return after

    def on_memory(self, instance_no: int, layer: str) -> None:
        """整理出一条记忆时给一点关系加成：记住关于他本人的事，最重。"""
        if not self.settings.relationship.enabled:
            return
        gain = {
            "core": self.settings.relationship.gain_core_memory,
            "important": self.settings.relationship.gain_important_memory,
        }.get(layer, 0.0)
        if gain <= 0:
            return
        current = self._get_float("affinity", instance_no)
        self.store.state_set(
            self._key("affinity", instance_no),
            f"{min(100.0, current + self._scale(gain, current)):.4f}",
        )

    def decay(self, now, instance_no: int) -> None:
        """长期不来的缓慢衰减。默认关闭（decay_per_day = 0）。"""
        rate = self.settings.relationship.decay_per_day
        if rate <= 0:
            return
        last_day = self.store.state_get(self._key("last_day", instance_no))
        if not last_day:
            return
        from datetime import date

        try:
            then = date.fromisoformat(str(last_day))
        except ValueError:
            return
        idle_days = max(0, (now.date() - then).days - 1)
        if idle_days <= 0:
            return
        current = self._get_float("affinity", instance_no)
        kept = max(0.0, current - rate * idle_days)
        self.store.state_set(self._key("affinity", instance_no), f"{kept:.4f}")

    def reset(self, instance_no: int) -> None:
        """回收换代：新的她不认识你。"""
        for name in ("affinity", "interactions", "last_day"):
            self.store.state_set(self._key(name, instance_no), "")

    def seed(self, instance_no: int) -> int:
        """给这个实例一个稳定的随机种子（情绪掷骰子用）。"""
        return zlib.crc32(f"{instance_no}:{self.settings.persona.name}".encode("utf-8"))
