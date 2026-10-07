"""情绪状态机：她的心情。

三股力量按优先级决定她今天什么心情：

1. **寿命压力**（最高优先级）：快到终端服务的时间了 → 不安。
   这是她的底色，谁也压不过它。
2. **你的语气**：你说的话明显低落 → 她在担心你；明显开心 → 她跟着开心。
   用的是零成本的关键词打分（确定性、无额外 API 调用、可测试）。
3. **日常漂移**：每天按日期确定性地"掷一次骰子"。
   所以她自己也会有说不清的心情——**"她今天心情不好，但不是因为你"**，
   这正是你要的那种真实感。

掷骰子以「日期 + 实例编号」为种子：同一天同一任的她，心情是确定的
（可复现、可测试，也不会每刷新一次就换一张脸）。
心情有持续时长，过期就回到平静。
"""

from __future__ import annotations

import random
import zlib
from dataclasses import dataclass

from .config import MoodDef, Settings
from .store import Store

POSITIVE = (
    "开心", "高兴", "太好了", "顺利", "成功", "喜欢", "不错", "哈哈", "轻松",
    "搞定", "升职", "通过", "好事", "惊喜", "谢谢", "温暖", "舒服", "赚", "赢了",
)
NEGATIVE = (
    "难过", "不开心", "累", "烦", "崩", "失败", "压力", "焦虑", "紧张", "哭",
    "失眠", "疼", "病", "吵", "辞职", "分手", "失业", "孤独", "委屈", "难受",
    "糟糕", "完了", "没意思", "撑不住",
)


def tone_of(text: str) -> float:
    """粗略语气分：-1（很低落）.. 1（很开心）。

    关键词计数 + 标点加成。刻意做得简单：零成本、确定性、可测试。
    真要更准可以换成一次模型调用，但那是每轮都付的延迟，不值。
    """
    text = text or ""
    pos = sum(1 for w in POSITIVE if w in text)
    neg = sum(1 for w in NEGATIVE if w in text)
    if pos == 0 and neg == 0:
        return 0.0
    score = (pos - neg) / float(pos + neg)
    if text.count("！") + text.count("!") >= 2:
        score += 0.1 if score >= 0 else -0.1
    if "哈哈" in text or "233" in text:
        score += 0.15
    return max(-1.0, min(1.0, score))


@dataclass
class Mood:
    key: str
    label: str
    note: str
    intensity: float
    since: str
    until: str
    source: str          # lifespan | his_tone | daily | manual

    @property
    def is_calm(self) -> bool:
        return self.key == "calm"


class MoodTracker:
    """把心情存进 store 的 state 表（按实例编号分键）。"""

    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    # ── 键 ────────────────────────────────────────────────
    def _key(self, name: str, instance_no: int) -> str:
        return f"mood:i{instance_no}:{name}"

    def _def(self, key: str) -> MoodDef:
        return self.settings.moods.get(key) or self.settings.moods["calm"]

    # ── 读 ────────────────────────────────────────────────
    def current(self, now, instance_no: int) -> Mood:
        key = str(self.store.state_get(self._key("key", instance_no)) or "calm")
        if key not in self.settings.moods:
            key = "calm"
        d = self._def(key)
        return Mood(
            key=key,
            label=d.label,
            note=d.note,
            intensity=float(self.store.state_get(self._key("intensity", instance_no)) or 0.0),
            since=str(self.store.state_get(self._key("since", instance_no)) or ""),
            until=str(self.store.state_get(self._key("until", instance_no)) or ""),
            source=str(self.store.state_get(self._key("source", instance_no)) or ""),
        )

    def _expired(self, now, instance_no: int) -> bool:
        until = str(self.store.state_get(self._key("until", instance_no)) or "")
        if not until:
            return True
        try:
            from datetime import datetime

            return now >= datetime.fromisoformat(until)
        except ValueError:
            return True

    # ── 写 ────────────────────────────────────────────────
    def _set(self, key: str, now, instance_no: int, *, source: str, hours: float, intensity: float = 0.6) -> Mood:
        from datetime import timedelta

        d = self._def(key)
        until = now + timedelta(hours=hours)
        self.store.state_set(self._key("key", instance_no), key)
        self.store.state_set(self._key("intensity", instance_no), f"{intensity:.3f}")
        self.store.state_set(self._key("since", instance_no), now.isoformat(timespec="seconds"))
        self.store.state_set(self._key("until", instance_no), until.isoformat(timespec="seconds"))
        self.store.state_set(self._key("source", instance_no), source)
        return Mood(
            key=key,
            label=d.label,
            note=d.note,
            intensity=intensity,
            since=now.isoformat(timespec="seconds"),
            until=until.isoformat(timespec="seconds"),
            source=source,
        )

    def tick(self, now, life, instance_no: int, *, last_user_text: str = "") -> Mood:
        """一次真实对话：按三股力量的优先级更新她的心情。"""
        if not self.settings.mood.enabled:
            return self.current(now, instance_no)

        # 1) 寿命压力：压过一切
        if 0 < life.remaining_hours <= self.settings.mood.uneasy_hours:
            cur = self.current(now, instance_no)
            if cur.key != "uneasy" or cur.source != "lifespan":
                return self._set(
                    "uneasy", now, instance_no,
                    source="lifespan",
                    hours=self.settings.mood.uneasy_duration_hours,
                )
            return cur

        # 2) 你的语气
        tone = tone_of(last_user_text)
        if tone <= -self.settings.mood.tone_threshold:
            cur = self.current(now, instance_no)
            if cur.key != "worried" or cur.source != "his_tone":
                return self._set("worried", now, instance_no, source="his_tone",
                                 hours=self.settings.mood.duration_hours,
                                 intensity=min(1.0, abs(tone)))
            return cur
        if tone >= self.settings.mood.tone_threshold:
            cur = self.current(now, instance_no)
            if cur.key != "happy" or cur.source != "his_tone":
                return self._set("happy", now, instance_no, source="his_tone",
                                 hours=self.settings.mood.duration_hours,
                                 intensity=min(1.0, tone))
            return cur

        # 3) 没过期就保持
        if not self._expired(now, instance_no):
            return self.current(now, instance_no)

        # 4) 过期了 → 按日期确定性地重新掷一次
        return self._set(
            self._roll(now, instance_no), now, instance_no,
            source="daily", hours=self.settings.mood.duration_hours,
            intensity=0.5,
        )

    def _roll(self, now, instance_no: int) -> str:
        weights = self.settings.mood.weights or {"calm": 1.0}
        seed = zlib.crc32(f"{now.date().isoformat()}|{instance_no}".encode("utf-8"))
        rng = random.Random(seed)
        keys = list(weights.keys())
        total = sum(max(0.0, float(weights[k])) for k in keys) or 1.0
        pick = rng.random() * total
        acc = 0.0
        for k in keys:
            acc += max(0.0, float(weights[k]))
            if pick <= acc:
                return k if k in self.settings.moods else "calm"
        return "calm"

    def force(self, key: str, now, instance_no: int) -> Mood:
        """手动指定心情（调试/演示用：想看看她低落时是什么样子）。"""
        if key not in self.settings.moods:
            raise KeyError(key)
        return self._set(key, now, instance_no, source="manual",
                         hours=self.settings.mood.duration_hours)

    def reset(self, instance_no: int) -> None:
        for name in ("key", "intensity", "since", "until", "source"):
            self.store.state_set(self._key(name, instance_no), "")
