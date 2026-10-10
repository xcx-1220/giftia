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
    print("C. duration 过期逻辑：过期的**软**心情对外回落成 calm（P1-16 已修）")
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
    print(f"    T0+11h now={sec(later)} current()={m1.key} until={m1.until} source={m1.source} "
          f"（until 已过 11 小时）")
    print("    【期望为何变了】C1/C2/C3 原来记录的是 P1-16 的**缺陷**：")
    print("    current() 完全不看 until，旧心情永久留在 state 里，状态栏/提示词/主动计划")
    print("    读到的都是「5 天前的心情」，于是她每天都能分享同一个旧心情、把稀缺额度烧光。")
    print("    修复后的契约：**软心情过期 → 对外回落 calm（source='expired'）**；")
    print("    只有 source=lifespan 的「不安」不被静默清掉（它压过一切，由 tick() 收尾）。")
    check("C1 软心情过期 11 小时 → current() 报 calm（source=expired），不再返回陈旧 low",
          m1.key == "calm" and m1.source == "expired", f"{m1.key}/{m1.source}")
    check("C1b 正向对照：没过期时 current() 仍然给出真实心情（不是把心情功能关掉）",
          m0.key == "low" and m0.source == "manual", f"{m0.key}/{m0.source}")

    # 过期后重新掷骰：按日期确定性重掷，并把新的 until 写回 state
    m2 = eb.mood.tick(later, eb.status(), 1, last_user_text="在吗")
    print(f"    过期后 tick(中性) → {m2.key}（source={m2.source}）")
    again = eb.current_mood()
    check("C2 过期后 tick() 按当天种子重掷并把新心情写回（不再无限保留旧值）",
          m2.source == "daily" and again.key == m2.key
          and again.source != "expired" and again.until != "",
          f"{m1.key} → {m2.key}（source={m2.source}，写回后 {again.key}/{again.source}）")
    mt3 = MoodTracker(eb.store, e)
    check("C2b 重掷是确定性的：同一天同一实例再掷一次结果相同（种子 = 日期+实例号）",
          mt3._roll(later, 1) == m2.key, f"_roll({later.date()}, 1)={mt3._roll(later, 1)} vs {m2.key}")

    eb.clock.advance(24 * 30)
    m3 = eb.current_mood()
    print(f"    再快进 30 天（期间没人说话）→ current() 仍={m3.key}，"
          f"until={m3.until}，now={sec(eb.clock.now())}")
    check("C3 长期闲置 30 天：current() 不会给出陈旧心情（仍是 calm/expired，不是 30 天前的 low）",
          m3.key == "calm" and m3.source == "expired", f"{m3.key}/{m3.source}")

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
    print("    【期望为何变了】D1/D2 原来记录的是 tone_of 的两个中文坑（P2）：")
    print("    「不开心」被 POSITIVE 的『开心』先命中、与 NEGATIVE 对消成 0（她不会担心你）；")
    print("    「我不累」被 NEGATIVE 的『累』命中（她会担心你）——而且「我不累」的否定根本没被处理。")
    print("    修复后：命中位置紧邻的前一个字是否定字就**反向计分**（并排除「特别/分别」这类假否定）。")
    check("D1 「不开心」判为负面（否认前缀反向计分后 = -1.0，她会担心你）",
          tone_of("不开心") < 0, f"{tone_of('不开心')}")
    check("D2 「我不累」判为非负面（否定词被处理，不再误判成低落）",
          tone_of("我不累") > 0, f"{tone_of('我不累')}")
    check("D2b 正向对照：真正的负面句仍然判成低落（不是把负面词都关掉）",
          tone_of("今天真的好累") < 0 and tone_of("我快撑不住了") < 0,
          f"累={tone_of('今天真的好累')} 撑不住={tone_of('我快撑不住了')}")
    check("D2c 正向对照：假否定不被误伤（「特别难过」里的『别』不是否定）",
          tone_of("特别难过") < 0, f"{tone_of('特别难过')}")
    print(f"    阈值 tone_threshold={s.mood.tone_threshold}："
          f"『不开心』= {tone_of('不开心')} 达到 → 她会担心；『我不累』= {tone_of('我不累')} → 她不会担心")
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
    print("F. 心情分享（mood_share）用的是 current()：过期心情不再被每天分享（P1-16 已修）")
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
    print(f"    期间心情一直是 {fb.current_mood().key}（source={fb.current_mood().source}，"
          f"until={fb.current_mood().until}，now={sec(fb.clock.now())}）")
    print("    【期望为何变了】F1 原来记录的是 P1-16 的缺陷：同一个陈旧心情会被")
    print("    「每天分享一次」，把一份稀缺的 30 天额度全烧在同一个旧心情上。")
    print("    修复后 current() 在过期时回落 calm → mood_share 不再拿陈旧心情当理由。")
    check("F1 陈旧心情不再被『每天分享一次』（5 天里 mood_share 至多 1 次）",
          shared.count("mood_share") <= 1, str(shared))
    check("F1b 心情确实过期了（until 已过，current() 报 calm/expired）",
          fb.current_mood().key == "calm" and fb.current_mood().source == "expired",
          f"{fb.current_mood().key}/{fb.current_mood().source}")

    # 正向对照：额度并没有被关掉——心情新鲜时 mood_share 仍然会被用上
    # （mood_share 还有第二道闸：距上次说话 ≥ mood_share_min_idle_hours(6h)，
    #   所以先静置 7 小时再问，别让"刚聊完"这条闸挡住对照）
    g = base_settings("mood-g", 81920.0)
    gb = Giftia(g, llm=MockLLM(g.model))
    gb.handle("在吗")
    gb.force_mood("tired")
    gb.clock.advance(7)
    fresh = gb.current_mood()
    plan_fresh = gb.outreach_plan(force=True)
    print(f"    正向对照：force_mood('tired') + 静置 7h（until={fresh.until} 未过期，"
          f"now={sec(gb.clock.now())}）→ outreach_plan().reason = "
          f"{plan_fresh.reason if plan_fresh else None}")
    check("F1c 正向对照：心情**新鲜**时 mood_share 仍然会用（不是把这条路关掉）",
          plan_fresh is not None and plan_fresh.reason == "mood_share",
          plan_fresh.reason if plan_fresh else "None")

    return report("beh_mood")


if __name__ == "__main__":
    raise SystemExit(main())
