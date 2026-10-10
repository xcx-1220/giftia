"""寿命系统：倒计时、退化阶段、终端服务预警。

关键设计：寿命在"激活那一刻"就被写死进数据库，之后改配置文件不会影响
这一任的她——和番剧里一样，出厂时就定好了。
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

from .clock import Clock
from .config import LifespanConfig, Settings, Stage
from .store import Store


@dataclass
class LifeStatus:
    instance_no: int
    activated_at: datetime
    expires_at: datetime
    total_hours: float
    used_hours: float
    remaining_hours: float
    remaining_ratio: float
    stage: Stage
    expired: bool

    @property
    def used_ratio(self) -> float:
        return max(0.0, min(1.0, 1.0 - self.remaining_ratio))

    @property
    def service_hours(self) -> float:
        return self.used_hours

    def human_remaining(self) -> str:
        return humanize_hours(self.remaining_hours)

    def human_service(self) -> str:
        return humanize_hours(self.used_hours)

    def human_total(self) -> str:
        return humanize_hours(self.total_hours)


def humanize_hours(hours: float) -> str:
    """把小时数说成人话，颗粒度随量级自动切换。"""
    if hours <= 0:
        return "0 小时"
    days = hours / 24.0
    if days >= 365:
        years = days // 365
        months = (days - years * 365) // 30
        return f"{years:.0f} 年 {months:.0f} 个月"
    if days >= 30:
        months = days // 30
        rest_days = days - months * 30
        return f"{months:.0f} 个月 {rest_days:.0f} 天"
    if days >= 1:
        return f"{days:.1f} 天"
    if hours >= 1:
        return f"{hours:.1f} 小时"
    if hours * 60 >= 1:
        return f"{hours * 60:.0f} 分钟"
    return "刚刚"


class Lifespan:
    META_ACTIVATED = "activated_at"
    META_INSTANCE = "instance_no"
    META_TOTAL = "instance_total_hours"

    def __init__(self, settings: Settings, store: Store, clock: Clock) -> None:
        self.settings = settings
        self.store = store
        self.clock = clock

    # ── 激活 / 换代 ───────────────────────────────────────
    def ensure_activated(self, total_hours_override: float | None = None) -> int:
        """首次运行时写入激活时间；返回当前实例编号。"""
        if self.store.meta_get(self.META_ACTIVATED) is None:
            hours = self.settings.lifespan.effective_hours(total_hours_override)
            now = self.clock.now()
            self.store.meta_set(self.META_ACTIVATED, self.clock.iso(now))
            self.store.meta_set(self.META_INSTANCE, 1)
            self.store.meta_set(self.META_TOTAL, f"{hours:.6f}")
        return self.instance_no

    def reactivate(self, total_hours_override: float | None = None) -> int:
        """回收之后，新的她一小时后被激活（番剧里的"下一位"）。"""
        hours = self.settings.lifespan.effective_hours(total_hours_override)
        instance = self.instance_no + 1
        now = self.clock.now()
        self.store.meta_set(self.META_ACTIVATED, self.clock.iso(now))
        self.store.meta_set(self.META_INSTANCE, instance)
        self.store.meta_set(self.META_TOTAL, f"{hours:.6f}")
        return instance

    @property
    def instance_no(self) -> int:
        return self.store.meta_get_int(self.META_INSTANCE, 1)

    @property
    def activated_at(self) -> datetime:
        raw = self.store.meta_get(self.META_ACTIVATED)
        if raw is None:
            return self.clock.now()
        return self.clock.parse(raw)

    @property
    def total_hours(self) -> float:
        """这一任的总寿命：激活时写死的值优先，其次才看当前配置。

        库里的值可能是坏的（0 / 负数 / nan / inf——手改库、旧版本写进去的）。
        坏值一律不用，退回配置值，别让"她活多久"取决于一个坏数字。
        """
        stored = self.store.meta_get_float(self.META_TOTAL, 0.0)
        if math.isfinite(stored) and stored > 0:
            return stored
        return self.settings.lifespan.effective_hours()

    # ── 状态 ──────────────────────────────────────────────
    def status(self) -> LifeStatus:
        now = self.clock.now()
        activated = self.activated_at
        total = self.total_hours
        if not math.isfinite(total) or total <= 0:
            # 绝不把坏值表现成"出生即到期"：那会让第一条消息就触发回收换代、
            # 把她的档案封存掉（P1-9）。退回能用的值，并且**大声说出来**。
            fallback = float(self.settings.lifespan.total_hours)
            if not math.isfinite(fallback) or fallback <= 0:
                fallback = LifespanConfig().total_hours
            print(
                f"[lifespan] 总寿命值非法（{total!r}），按 {fallback:g} 小时处理；"
                "请检查 settings.toml [lifespan] 与 meta.instance_total_hours",
                file=sys.stderr,
            )
            total = fallback
        try:
            expires = activated + timedelta(hours=total)
        except (OverflowError, OSError, ValueError):
            # 极大但有限的值（例如 1e300）会让 timedelta 直接溢出。
            # status() 无处不在（/status、/prompt、进度条、brief），在这儿崩
            # 等于这个库再也启动不了对话。只影响展示的"回收预定"日期，
            # ratio / stage / expired 都不受影响。
            expires = activated + timedelta(days=36500)  # ≈100 年
        used = (now - activated).total_seconds() / 3600.0
        remaining = total - used
        ratio = remaining / total if total > 0 else 0.0
        ratio = max(0.0, min(1.0, ratio))
        stage = self.settings.stage_for(ratio)
        return LifeStatus(
            instance_no=self.instance_no,
            activated_at=activated,
            expires_at=expires,
            total_hours=total,
            used_hours=max(0.0, used),
            remaining_hours=max(0.0, remaining),
            remaining_ratio=ratio,
            stage=stage,
            expired=remaining <= 0,
        )

    def is_expired(self) -> bool:
        return self.status().expired

    def is_terminal_warning(self) -> bool:
        """回收预告：默认最后 72 小时，但不允许超过总寿命的 5%。

        否则当你把寿命压成 100 小时做演示时，一开机就收到"回收预告"。
        """
        st = self.status()
        threshold = min(self.settings.lifespan.terminal_warning_hours, st.total_hours * 0.05)
        return 0 < st.remaining_hours <= max(threshold, 1e-9)

    # ── 展示 ──────────────────────────────────────────────
    def progress_bar(self, width: int = 30) -> str:
        st = self.status()
        filled = int(round(st.used_ratio * width))
        filled = max(0, min(width, filled))
        return "█" * filled + "·" * (width - filled)

    def status_block(self) -> str:
        st = self.status()
        lines = [
            f"实例编号      #{st.instance_no}",
            f"激活时间      {st.activated_at.strftime('%Y-%m-%d %H:%M')}",
            f"设计寿命      {st.human_total()}（{st.total_hours:.0f} 小时）",
            f"已服务        {st.human_service()}",
            f"剩余寿命      {st.human_remaining()}",
            f"回收预定      {st.expires_at.strftime('%Y-%m-%d %H:%M')}",
            f"记忆状态      {st.stage.label}（stage={st.stage.name}, 清晰度 {1 - st.stage.blur:.0%}）",
            f"进度          [{self.progress_bar()}] {st.used_ratio:.2%}",
        ]
        if st.expired:
            lines.append("!! 已到期：终端服务应当立即执行回收")
        elif self.is_terminal_warning():
            lines.append("!! 剩余不足预警阈值，终端服务已收到回收预告")
        return "\n".join(lines)

    def brief(self) -> str:
        """给模型看的一行状态。"""
        st = self.status()
        return (
            f"剩余寿命 {st.human_remaining()}（{st.remaining_hours:.0f}/{st.total_hours:.0f} 小时，"
            f"已服务 {st.used_ratio:.1%}）"
        )
