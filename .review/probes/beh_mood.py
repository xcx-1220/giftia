"""Stream D 探针 2：mood 情绪状态机 —— 三股力量、确定性掷骰、duration 过期。

    python .review/run_tests.py .review/probes/beh_mood.py

只读产品代码；临时数据在 .review/tmp/behavior-checker/mood-*。
"""

from __future__ import annotations

import dataclasses
import random
import sys
import zlib
from datetime import datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from beh_common import base_settings, check, note, report  # noqa: E402

from giftia.brain import Giftia  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402
from giftia.mood import NEGATIVE, POSITIVE, MoodTracker, tone_of  # noqa: E402


def sec(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def main() -> int:
    print("=" * 78)
    print("A. 确定性掷骰：同一天同一任是否稳定 / 跨进程 / 换实例")
    print("=" * 78)
    s = base_settings("mood-a", 81920.0, )
    b = Giftia(s, llm=MockLLM(s.model))
    mt = b.mood
    d = datetime(2026, 10, 10, 12, 0)
    rolls = [mt._roll(d, 1) for _ in range(5)]
    check("A1 同一天同一实例多次掷骰结果一致", len(set(rolls)) == 1, str(rolls))
    print(f"    _roll(2026-10-10, #1) = {rolls[0]!r}")
    print(f"    种子 = zlib.crc32('2026-10-10|1') = {zlib.crc32(b'2026-10-10|1')}（不是 hash()，跨进程稳定）")
    # 独立 Random 实例（模拟跨进程/重启后重新构造）
    r2 = random.Random(zlib.crc32(b"2026-10-10|1"))
    print(f"    独立构造的 Random 第一个随机数 = {r2.random():.6f}（与上面同源 → 重启后一致）")
    keys = {no: mt._roll(d, no) for no in range(1, 6)}
    print(f"    同一天不同实例的掷骰：{keys}")
    check("A2 实例号改变会改变结果（换代后是新的一张脸，不继承旧心情）",
          len(set(keys.values())) > 1, str(keys))
    b.mood.reset(1)
    check("A3 reset(实例) 之后回到 calm（换代清零）", b.current_mood().key == "calm",
          b.current_mood().key)

    print("\n    —— 权重分布（与 settings 的 [mood.weights] 对齐）——")
    counts: dict[str, int] = {}
    start = datetime(2026, 1, 1, 12, 0)
    for i in range(2000):
        k = mt._roll(start + timedelta(days=i), 1)
        counts[k] = counts.get(k, 0) + 1
    w = s.mood.weights
    print(f"    2000 天实测频率：" + "  ".join(
        f"{k}={counts.get(k, 0) / 2000:.3f}(配置 {w.get(k)})" for k in w))
    check("A4 实测频率与配置权重吻合（±0.05）",
          all(abs(counts.get(k, 0) / 2000 - float(w[k])) < 0.05 for k in w),
          str({k: counts.get(k, 0) for k in w}))

    print()
    print("=" * 78)
    print("B. 三股力量优先级：寿命压力 > 你的语气 > 每日漂移")
    print("=" * 78)
    # B1 寿命压力压过「明显开心」
    n = base_settings("mood-b", 100.0)
    nb = Giftia(n, llm=MockLLM(n.model))
    nb.clock.advance(100 - 10)          # 剩余 10h < uneasy_hours 48
    m = nb.mood.tick(nb.clock.now(), nb.status(), 1, last_user_text="太好了！我升职了！哈哈哈！")
    check("B1 剩余 10h + 明显开心 → 仍是 uneasy（寿命压力压过语气）",
          m.key == "uneasy" and m.source == "lifespan", f"{m.key}/{m.source}")
    print(f"    remaining={nb.status().remaining_hours:.1f}h uneasy_hours={n.mood.uneasy_hours} → {m.key}")

    # B2 语气
    t = base_settings("mood-b2", 81920.0)
    tb = Giftia(t, llm=MockLLM(t.model))
    now = tb.clock.now()
    m_neg = tb.mood.tick(now, tb.status(), 1, last_user_text="今天真的好累，压力好大，我快撑不住了")
    print(f"    负面文本 → {m_neg.key}/{m_neg.source} intensity={m_neg.intensity}")
    check("B2 明显低落 → worried（source=his_tone）",
          m_neg.key == "worried" and m_neg.source == "his_tone", f"{m_neg.key}")
    m_pos = tb.mood.tick(now + timedelta(seconds=1), tb.status(), 1,
                         last_user_text="太好了！我成功了！！高兴")
    check("B3 明显开心 → happy", m_pos.key == "happy" and m_pos.source == "his_tone", f"{m_pos.key}")
    m_keep = tb.mood.tick(now + timedelta(hours=1), tb.status(), 1, last_user_text="在吗")
    check("B4 中性语气 + 未过期 → 保持原心情（不会每句一换）",
          m_keep.key == m_pos.key, f"{m_keep.key}")

    # B5 每日漂移：中性语气 + 已过期
    mt2 = MoodTracker(tb.store, t)
    tb.store.state_set("mood:i1:until", sec(now - timedelta(hours=1)))
    rolled = tb.mood.tick(now, tb.status(), 1, last_user_text="在吗")
    expect = mt2._roll(now, 1)
    check("B5 中性语气 + 已过期 → 按日期确定性掷骰",
          rolled.key == expect and rolled.source == "daily", f"{rolled.key} vs {expect}")

    print()
    print("=" * 78)
    print("C. duration 过期逻辑：过期的心情不会消失，也不会回到平静")
    print("=" * 78)
    e = base_settings("mood-c", 81920.0)
    eb = Giftia(e, llm=MockLLM(e.model))
    eb.handle("在吗")
    eb.force_mood("low")
    t0 = eb.clock.now()
    m0 = eb.current_mood()
    print(f"    T0  now={sec(t0)} 心情={m0.key} until={m0.until}")
    eb.clock.advance(11)                      # > duration_hours=10
    later = eb.clock.now()
    m1 = eb.current_mood()
    print(f"    T0+11h now={sec(later)} current()={m1.key} until={m1.until} "
          f"（until 已过 {11} 小时）")
    check("C1 current() 无视 until：过期 11 小时仍返回 low（状态不会自己回落到 calm）",
          m1.key == "low" and m1.until < sec(later), f"{m1.key} until={m1.until}")

    # 过期后重新掷骰：同一天内会掷出同一个 key
    m2 = eb.mood.tick(later, eb.status(), 1, last_user_text="在吗")
    print(f"    过期后 tick(中性) → {m2.key}（source={m2.source}）")
    check("C2 「过期就回到平静」不成立：过期后是重新掷骰，且同一天会掷回同一个 key",
          m2.key == m1.key and m2.source == "daily",
          f"{m1.key} → {m2.key}（duration_hours={e.mood.duration_hours}，"
          f"种子只含日期 {later.date()} 和实例号）")

    eb.clock.advance(24 * 30)
    m3 = eb.current_mood()
    print(f"    再快进 30 天（期间没人说话）→ current() 仍={m3.key}，"
          f"until={m3.until}，now={sec(eb.clock.now())}")
    check("C3 长期闲置时 current() 永远给出陈旧心情（主动联系/状态栏/提示词都用它）",
          m3.key == "low", f"{m3.key}")

    print()
    print("=" * 78)
    print("D. tone_of 关键词打分：否定句与常见口语的反例")
    print("=" * 78)
    cases = [
        ("我不累", "他在说自己不累"),
        ("不开心", "明显的负面情绪"),
        ("别担心，我不累", "明显的安慰句"),
        ("我升职失败了", "喜忧各一半"),
        ("今天不忙，挺轻松的", "轻松"),
        ("哈哈哈太开心了", "明显开心"),
    ]
    for text, why in cases:
        print(f"    tone_of({text!r}) = {tone_of(text):+.2f}   （{why}）")
    check("D1 「不开心」被判为中性（POSITIVE 的『开心』先命中，抵消 NEGATIVE 的『不开心』）",
          tone_of("不开心") == 0.0, f"{tone_of('不开心')}")
    check("D2 「我不累」被判为低落（NEGATIVE『累』命中，否定词无人处理）",
          tone_of("我不累") < 0, f"{tone_of('我不累')}")
    print(f"    阈值 tone_threshold={s.mood.tone_threshold}："
          f"『不开心』= {tone_of('不开心')} 达不到 → 她不会担心；『我不累』= {tone_of('我不累')} → 她会担心")
    check("D3 关键词表里『开心』与『不开心』同时存在（子串互相抵消）",
          "开心" in POSITIVE and "不开心" in NEGATIVE,
          f"POSITIVE 含『开心』={('开心' in POSITIVE)}，NEGATIVE 含『不开心』={('不开心' in NEGATIVE)}")

    print()
    print("=" * 78)
    print("E. 边界：权重全 0 / 负权重 / calmer 兜底")
    print("=" * 78)
    z = dataclasses.replace(
        base_settings("mood-e", 81920.0),
        mood=dataclasses.replace(base_settings("mood-e2", 81920.0).mood,
                                 weights={"calm": 0.0, "happy": 0.0}),
    )
    zb = Giftia(z, llm=MockLLM(z.model))
    day0 = datetime(2026, 1, 1, 12, 0)
    got = [zb.mood._roll(day0 + timedelta(days=i), 1) for i in range(10)]
    print(f"    权重全 0 时 10 次掷骰 = {got}")
    check("E1 权重全 0 → 兜底为 calm，不会异常", all(g == "calm" for g in got), str(set(got)))
    w2 = dataclasses.replace(
        base_settings("mood-e3", 81920.0),
        mood=dataclasses.replace(base_settings("mood-e4", 81920.0).mood,
                                 weights={"calm": -5.0, "low": 1.0}),
    )
    wb = Giftia(w2, llm=MockLLM(w2.model))
    got2 = {wb.mood._roll(day0 + timedelta(days=i), 1) for i in range(30)}
    check("E2 负权重被 max(0,·) 吃掉，剩下的按权重抽", got2 <= {"calm", "low"}, str(got2))
    print(f"    权重 {{calm:-5, low:1}} → 30 天结果集合 = {got2}（calm 被负数权重排除后只剩 low）")

    print()
    print("=" * 78)
    print("F. 心情分享（mood_share）用的是 current()：陈旧心情 + 每日一条")
    print("=" * 78)
    f = base_settings("mood-f", 81920.0)
    fb = Giftia(f, llm=MockLLM(f.model))
    fb.handle("在吗")
    fb.force_mood("tired")
    shared: list[str] = []
    for i in range(5):
        plan_ = fb.outreach_plan(force=True)
        shared.append(plan_.reason if plan_ else "None")
        fb.clock.advance(24)
    print(f"    连续 5 天，outreach_plan().reason = {shared}")
    print(f"    期间心情一直是 {fb.current_mood().key}（until={fb.current_mood().until}，"
          f"now={sec(fb.clock.now())}），从没被清过")
    check("F1 同一个陈旧心情会被『每天分享一次』（同一天同一种心情只说一次 = 一天一条）",
          shared.count("mood_share") >= 4, str(shared))

    return report("beh_mood")


if __name__ == "__main__":
    raise SystemExit(main())
