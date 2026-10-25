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
import re
import zlib
from dataclasses import dataclass

from .config import MoodDef, Settings
from .store import Store

POSITIVE = (
    "开心", "高兴", "太好了", "顺利", "成功", "喜欢", "不错", "哈哈", "轻松",
    "搞定", "升职", "通过", "好事", "惊喜", "谢谢", "温暖", "舒服", "赚", "赢了",
)
NEGATIVE = (
    "难过", "不开心", "烦", "崩溃", "失败", "压力", "焦虑", "紧张", "哭",
    "失眠", "疼", "病", "吵", "辞职", "分手", "失业", "孤独", "委屈", "难受",
    "糟糕", "完了", "没意思", "撑不住",
)
# `累` 太容易被当子串命中（"积累""累积"），所以单独用词边界匹配；
# 而 "我不累" / "别担心，我不累" 这类**否定**不能算负面——她不该为这个担心你。
_TIRED_RE = re.compile(r"(?<!积)(?<!劳)累")
# 否定判定：**看紧邻的那一个字**是不是否定字（不/没/别/无/勿/甭），
# 再加两个双字否定词。不能只看"前面 2~3 个字里出现了否定字"——
# 实测「特别难过」会被 `特别` 里的"别"误判成否定，语气分从 -1.0 掉到 -0.1，
# 她就再也不会因为你难过而担心你了。
_NEGATION_CHARS = "不没别无勿甭"
_NEGATION_PAIRS = ("没有", "毫无")
# 有些"像否定"的字其实是别的词的一部分，识别时要把它们排除：
#   · 特别难过 / 特别开心 —— `特别` 是程度副词，不是否定
#   · 分别、个别、区别 —— 同理
#   · 一点也不开心 —— `一点` 是程度状语，"不"仍然是否定
# 做法：命中位置**正前方**那个字若在下面这个集合里，就认为否定字属于前一个词，不算否定。
_NEGATION_LOOKBEHIND = "特分个区差别"
_NEGATION_WORDS = ("不", "没", "没有", "别", "不要", "不太", "不再", "从未", "毫无", "无", "勿", "甭")


def _negated(text: str, index: int) -> bool:
    """命中位置**紧邻的前一个字**是否定字吗（兼容「没有」「毫无」这种双字否定）。"""
    if index <= 0:
        return False
    prev = text[index - 1]
    if prev not in _NEGATION_CHARS:
        return text[max(0, index - 2) : index] in _NEGATION_PAIRS
    # 「特别」「分别」里的"别"不是否定；「一点也不」里的"不"仍是否定
    before = text[index - 2] if index >= 2 else ""
    if before and before in _NEGATION_LOOKBEHIND:
        return False
    return True


def tone_of(text: str) -> float:
    """粗略语气分：-1（很低落）.. 1（很开心）。

    关键词计数 + 标点加成。刻意做得简单：零成本、确定性、可测试。
    要处理两个中文里的经典坑（审查报告 P2）：
      · 「不开心」：`开心` 命中的同时 `不开心` 也命中，直接对消成 0 分 → 她不会担心你；
      · 「我不累」：`累` 命中 → 判成负面 → 她会担心你（其实你在说没事）。
    所以：命中的词前面有否定词就**反向**计一分，`累` 用词边界避免"积累"误伤。
    """
    text = text or ""
    # 完成动作中的“完了”不是灾难；保留独立的“完了”。
    text = re.sub(r"(?:吃|做|看|读|写|说|用|忙|弄|学|考|买|卖|办|洗|打|处理)完了", "完成", text)
    negative_events = bool(re.search(r"停服|倒闭|去世|被裁|被拒|当空气|被忽视", text))
    pos = neg = 0
    for word in POSITIVE:
        start = 0
        while (idx := text.find(word, start)) != -1:
            if _negated(text, idx) or re.match(re.escape(word) + r"(?:不起来|不了|不成)", text[idx:]):
                neg += 1
            else:
                pos += 1
            start = idx + len(word)
    for word in NEGATIVE:
        start = 0
        while (idx := text.find(word, start)) != -1:
            # 「不开心」本身就在 NEGATIVE 里，且前缀是否定 —— 不能再被反向一次
            if word.startswith(_NEGATION_WORDS) or not _negated(text, idx):
                neg += 1
            else:
                pos += 1
            start = idx + len(word)
    # 单独判 `累`：它在 NEGATIVE 里已经去掉，改由词边界匹配（见 _TIRED_RE 的注释）
    for m in _TIRED_RE.finditer(text):
        if _negated(text, m.start()):
            pos += 1
        else:
            neg += 1
    if negative_events:
        neg += max(1, pos + 1)
    if pos == 0 and neg == 0:
        return 0.0
    score = (pos - neg) / float(pos + neg)
    # 平手时偏向"负面"：漏掉一次难过比多问一句"你还好吗"代价大得多。
    # 典型例子：「我升职失败了」——`升职` 是正面词、`失败` 是负面词，机械对消成 0 分，
    # 而她本该担心你。只有真的正负都不沾才返回 0（上面那条）。
    if pos and neg and score == 0.0:
        score = -0.5
    if negative_events:
        score = min(score, -0.6)
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
        """她此刻的心情。

        **过期的软心情回落成"平静"**（审查报告 P1-16）：以前 `current()` 完全不看 `until`，
        于是没人说话时那个旧心情永远留在 state 里 —— 状态栏、提示词、主动计划读到的
        都是"5 天前的心情"，于是她每天都能"分享"同一个旧心情，把稀缺额度烧光。
        寿命压力那种"不安"由 `tick()` 负责收尾（它压过一切，不该被静默清掉）。
        """
        key = str(self.store.state_get(self._key("key", instance_no)) or "calm")
        if key not in self.settings.moods:
            key = "calm"
        source = str(self.store.state_get(self._key("source", instance_no)) or "")
        if key != "calm" and source != "lifespan" and self._expired(now, instance_no):
            # 软心情过期：对外一律报 calm（state 里那份旧值留着不写，
            # 下一次 tick 会按当天种子正常重掷并覆盖它）
            return Mood(
                key="calm",
                label=self._def("calm").label,
                note=self._def("calm").note,
                intensity=0.0,
                since="",
                until="",
                source="expired",
            )
        d = self._def(key)
        return Mood(
            key=key,
            label=d.label,
            note=d.note,
            intensity=float(self.store.state_get(self._key("intensity", instance_no)) or 0.0),
            since=str(self.store.state_get(self._key("since", instance_no)) or ""),
            until=str(self.store.state_get(self._key("until", instance_no)) or ""),
            source=source,
        )

    def is_expired(self, now, instance_no: int) -> bool:
        """当前心情是不是已经过期（供主动联系判断"该不该拿它当理由"）。"""
        return self._expired(now, instance_no)

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

        # 1b) 寿命压力已经过去（被 /time 回拨、或换了新一任）：那份"不安"要收尾，
        #     否则它会以 source=lifespan 的身份永久留着（`current()` 有意不为它兜底）。
        cur = self.current(now, instance_no)
        if cur.key == "uneasy" and cur.source == "lifespan":
            return self._set(
                self._roll(now, instance_no), now, instance_no,
                source="daily", hours=self.settings.mood.duration_hours, intensity=0.5,
            )

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
