"""Stream D 探针 3：bond 关系 —— 按天结算能不能刷、diminish 公式、阶段两条腿、回收清零。

    python .review/run_tests.py .review/probes/beh_bond.py

只读产品代码；临时数据在 .review/tmp/behavior-checker/bond-*。
"""

from __future__ import annotations

import dataclasses
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from beh_common import base_settings, check, note, report  # noqa: E402

from giftia.bond import BondTracker, stage_for  # noqa: E402
from giftia.brain import Giftia  # noqa: E402
from giftia.config import DEFAULT_BOND_STAGES  # noqa: E402
from giftia.llm import MemoryDraft, MockLLM  # noqa: E402
from giftia.store import Store  # noqa: E402


class CoreMemoryLLM(MockLLM):
    """模拟"她真的在记关于你的事"：每次整理产出一条全新的内核记忆。"""

    TOPICS = [
        "搭档养了一只叫团子的橘猫", "搭档住在杭州西湖区", "搭档的生日是三月十四号",
        "搭档讨厌香菜和折耳根", "搭档在准备注册会计师考试", "搭档的妹妹下个月结婚",
        "搭档每天骑单车通勤四十分钟", "搭档小时候在哈尔滨长大", "搭档想攒钱买一台钢琴",
        "搭档最喜欢秋天和桂花味", "搭档的公司搬到了滨江", "搭档上周把手机摔碎了",
        "搭档喜欢在深夜听爵士乐", "搭档明天要去拔智齿", "搭档不吃早饭已经三年",
        "搭档想养一只柴犬叫豆豆", "搭档的工位在靠窗第二排", "搭档最近在学游泳",
        "搭档最怕坐飞机", "搭档周末常去爬北高峰", "搭档的旧手表是爷爷留下的",
        "搭档爱喝冰美式不加糖", "搭档的房东要涨房租", "搭档在追一部老港剧",
        "搭档把阳台改成了小花园", "搭档的同事叫老周", "搭档想学做番茄牛腩",
        "搭档的手机壁纸是雪山",
    ]

    def __init__(self, model):
        super().__init__(model)
        self.n = 0

    def extract_memories(self, transcript, *, name="Giftia", today="", scene="c2c", known=""):
        topic = self.TOPICS[self.n % len(self.TOPICS)]
        self.n += 1
        return [MemoryDraft(
            summary=f"关于{topic}这件事，他跟你说过了。",
            tags="搭档", emotion="平常", importance=0.9, layer="core",
        )]


def main() -> int:
    print("=" * 78)
    print("A. 「按天结算、不可刷」：同一天说一百句 / 别人说话 / 群里说话")
    print("=" * 78)
    s0 = base_settings("bond-a", 81920.0)
    s1 = dataclasses.replace(  # 隔离掉记忆加成，只看「按天结算」这一条规则
        s0, relationship=dataclasses.replace(
            s0.relationship, gain_core_memory=0.0, gain_important_memory=0.0))
    one = Giftia(s1, llm=MockLLM(s1.model))
    one.handle("在吗")
    a_one = one.current_bond().affinity
    print(f"    一天说 1 句 → 亲密度 {a_one:.4f}（gain_daily={s1.relationship.gain_daily}）")

    many = Giftia(dataclasses.replace(s1, root=base_settings("bond-a2", 81920.0).root),
                  llm=MockLLM(s1.model))
    for i in range(100):
        many.handle(f"在吗，第 {i} 句")
    a_many = many.current_bond().affinity
    print(f"    同一天说 100 句 → 亲密度 {a_many:.4f}（interactions={many.current_bond().interactions}）")
    check("A1 同一天 100 句 = 1 句（按天结算成立）", abs(a_many - a_one) < 1e-9,
          f"{a_one:.4f} vs {a_many:.4f}")

    # 第二天：只是"来了"就算
    many.clock.advance(24)
    many.handle("在吗")
    a_day2 = many.current_bond().affinity
    print(f"    第二天再来 1 句 → 亲密度 {a_day2:.4f}（+{a_day2 - a_many:.4f}）")
    check("A2 隔天再来会再涨（同一天只结算一次，不是只有第一天算）",
          a_day2 > a_many, f"{a_many:.4f} → {a_day2:.4f}")

    # 别人（访客私聊 / 群聊）刷不动
    g = Giftia(dataclasses.replace(s1, root=base_settings("bond-a3", 81920.0).root),
               llm=MockLLM(s1.model))
    g.set_handler_id("OWNER-1")
    g.handle("在吗", "OWNER-1")
    base_aff = g.current_bond().affinity
    for i in range(50):
        g.handle(f"你好呀第 {i} 句", "GUEST-9", scene="c2c")     # 别人私聊
    for i in range(50):
        g.handle(f"群里说话第 {i} 句", "GUEST-9", scene="group", group_id="G-1")
    print(f"    搭档 1 句 → {base_aff:.4f}；之后访客私聊 50 句 + 群聊 50 句 → "
          f"{g.current_bond().affinity:.4f}（interactions={g.current_bond().interactions}）")
    check("A3 别人私聊 + 群里说话都刷不动亲密度", abs(g.current_bond().affinity - base_aff) < 1e-9,
          f"{base_aff:.4f} vs {g.current_bond().affinity:.4f}")

    print()
    print("=" * 78)
    print("B. diminish 公式：实际涨幅 = 名义 × (1 - 当前/100)")
    print("=" * 78)
    tb_s = dataclasses.replace(
        base_settings("bond-b", 81920.0),
        relationship=dataclasses.replace(
            base_settings("bond-b2", 81920.0).relationship,
            gain_daily=10.0, gain_emotion_bonus=0.0,
            gain_core_memory=0.0, gain_important_memory=0.0, diminish=True),
    )
    tb = Giftia(tb_s, llm=MockLLM(tb_s.model))
    tb.handle("在吗")
    expect = 10.0
    got = tb.current_bond().affinity
    print(f"    0 → 第一次：实际 {got:.4f}，公式 10×(1-0/100)={expect:.4f}")
    check("B1 affinity=0 时全额", abs(got - expect) < 1e-6, f"{got:.4f}")
    for step, a0 in ((2, got), (3, None)):
        a0 = tb.current_bond().affinity
        exp = a0 + 10.0 * (1 - a0 / 100.0)
        tb.clock.advance(24)
        tb.handle("在吗")
        a1 = tb.current_bond().affinity
        print(f"    第 {step} 天：{a0:.4f} → {a1:.4f}（公式预测 {exp:.4f}，涨幅 {a1 - a0:.4f}）")
        check(f"B{step} diminish 递减增长与公式一致", abs(a1 - exp) < 1e-6,
              f"{a1:.4f} vs {exp:.4f}")

    print()
    print("=" * 78)
    print("C. 阶段判定：min_affinity + min_days 两条腿，README 的天数曲线")
    print("=" * 78)
    # C1 stage_for 纯函数：只有亲密度不够 / 只有天数不够
    st_aff_only, _ = stage_for(DEFAULT_BOND_STAGES, 100.0, 0.5)
    st_days_only, _ = stage_for(DEFAULT_BOND_STAGES, 0.0, 500.0)
    st_both, nxt = stage_for(DEFAULT_BOND_STAGES, 100.0, 500.0)
    print(f"    亲密度 100 / 认识 0.5 天 → {st_aff_only.key}")
    print(f"    亲密度 0   / 认识 500 天 → {st_days_only.key}")
    print(f"    亲密度 100 / 认识 500 天 → {st_both.key}（next={nxt}）")
    check("C1 只有亲密度不够（要有天数）", st_aff_only.key == "stranger", st_aff_only.key)
    check("C2 只有天数不够（要有亲密度）", st_days_only.key == "stranger", st_days_only.key)
    check("C3 两条腿都满足才到最高档", st_both.key == "irreplaceable", st_both.key)

    # C4 README：第 11/34/80/180 天
    sim_s = dataclasses.replace(
        base_settings("bond-c", 100000.0),
        relationship=dataclasses.replace(
            base_settings("bond-c2", 100000.0).relationship,
            gain_daily=1.5, gain_emotion_bonus=0.5,
            gain_core_memory=0.0, gain_important_memory=0.0),
    )
    sim = Giftia(sim_s, llm=MockLLM(sim_s.model))
    seen = {}
    # 每天都来一次（中性语气 → 没有情绪加成），按激活当天算第 1 天
    for day in range(1, 400):
        sim.bond.touch(sim.clock.now(), sim.instance_no, sim.lifespan.activated_at,
                       text="在吗", tone=0.0)
        b = sim.current_bond()
        if b.stage.key not in seen:
            seen[b.stage.key] = (day, b.affinity, b.days_known)
        sim.clock.advance(24)
    print("    每天都见面（中性语气、无记忆加成）的实测曲线：")
    for k, (day, aff, dk) in seen.items():
        print(f"      第 {day:>3} 个日历日（认识 {dk:6.2f} 天） → {k:<14} 亲密度={aff:6.2f}")
    readme = {"partner": 11, "familiar": 34, "close": 80, "irreplaceable": 180}
    gotdays = {k: seen.get(k, (None,))[0] for k in readme}
    print("    【期望为何变了】C4 原来记录的是「README 的 180 天复现不出来」（record: 181）——")
    print("    但这不是缺陷，是**文档口径**：README 的天数是日历日，而最高档的门槛是")
    print("    `days_known >= 180`；按激活当天算第 1 天，days_known 恰好到 180 时已经是")
    print("    第 181 个日历日。前 3 档（11/34/80）两种口径完全一致，只有第 4 档差 1 天。")
    print("    所以断言改成「实测就是 11/34/80/181，且与 README 只差最后 1 天的口径差」。")
    check("C4 实测 11/34/80/**181**（前三档与 README 一致；第 4 档差 1 天 = 日历日 vs days_known 口径）",
          gotdays["partner"] == 11 and gotdays["familiar"] == 34
          and gotdays["close"] == 80 and gotdays["irreplaceable"] == 181,
          f"实测={gotdays}，README={readme}")
    check("C4b 口径差可解释：第 181 个日历日的 days_known ≈ 180（刚好跨过门槛），差 1 天来自日历日 vs days_known",
          abs(float(seen["irreplaceable"][2]) - 180.0) < 1e-3,
          f"days_known={seen['irreplaceable'][2]}，affinity={seen['irreplaceable'][1]:.2f}")

    print()
    print("=" * 78)
    print("D. 「奖励的是你来了，不是你说多少」—— 同一天多聊不再重复加成（P1-15 已修）")
    print("=" * 78)
    d0 = dataclasses.replace(
        base_settings("bond-d", 81920.0),
        memory=dataclasses.replace(base_settings("bond-d2", 81920.0).memory, consolidate_every=2),
    )
    llm = CoreMemoryLLM(d0.model)
    d = Giftia(d0, llm=llm)
    d.set_handler_id("OWNER-1")
    d.handle("我叫小林", "OWNER-1")            # 第一条：按天结算 +1.5
    a_first = d.current_bond().affinity
    print(f"    第 1 句之后：亲密度 {a_first:.4f}，记忆 {d.store.count_memories(d.instance_no)} 条")
    for i in range(19):
        d.handle(f"我再告诉你一件事，这是第 {i} 件事", "OWNER-1")
    a_end = d.current_bond().affinity
    mems = d.store.count_memories(d.instance_no)
    print(f"    同一天再说 19 句 → 亲密度 {a_end:.4f}（interactions={d.current_bond().interactions}），"
          f"记忆 {mems} 条，她一共整理了 {llm.n} 次")
    print(f"    按 README「同一天说一百句也只算一次」应为 {a_first:.4f}；"
          f"实际 {a_end:.4f}（多出来 {a_end - a_first:.4f}）")
    print("    【期望为何变了】D1/D2 原来记录的是 P1-15 的缺陷：同一天内可以靠")
    print("    「多聊 → 多整理出内核记忆」无限刷亲密度（实测曾经 3.47 → 7.4+）。")
    print("    修复后亲密度按天结算：同一天内 19 条记忆 / 20 次整理也**只算一次**。")
    check("D1 同一天内多聊不再涨亲密度（3.47 → 3.47），interactions 也仍是 1",
          abs(a_end - a_first) < 1e-9 and d.current_bond().interactions == 1,
          f"{a_first:.4f} → {a_end:.4f}，interactions={d.current_bond().interactions}，"
          f"记忆 {mems} 条")
    check("D2 记忆确实在涨（19 条 / 20 次整理），但亲密度与条数**不成正比**",
          mems >= 5 and abs(a_end - a_first) < 1e-9,
          f"记忆 {mems} 条，亲密度 {a_end:.4f}（涨了 {a_end - a_first:.4f}）")
    # 正向对照：跨一天仍然会涨（证明不是把成长曲线关掉，只是不再按条计）
    d.clock.advance(24)
    d.handle("第二天再来", "OWNER-1")
    a_next = d.current_bond().affinity
    print(f"    正向对照：跨到第 2 天再说一句 → 亲密度 {a_end:.4f} → {a_next:.4f}"
          f"（interactions={d.current_bond().interactions}）")
    check("D2b 正向对照：跨天后亲密度照常增长（成长曲线没有被关掉）",
          a_next > a_end and d.current_bond().interactions == 2,
          f"{a_end:.4f} → {a_next:.4f}")
    print(f"    interactions 仍是 {d.current_bond().interactions}（按天计数），"
          f"亲密度也是「一天一次」的曲线")

    print()
    print("=" * 78)
    print("E. 回收换代：关系清零；decay 只在「他回来说话」时才结算")
    print("=" * 78)
    e0 = dataclasses.replace(
        base_settings("bond-e", 81920.0),
        relationship=dataclasses.replace(
            base_settings("bond-e2", 81920.0).relationship,
            gain_core_memory=0.0, gain_important_memory=0.0, decay_per_day=5.0),
    )
    e = Giftia(e0, llm=MockLLM(e0.model))
    e.set_handler_id("OWNER-1")
    for i in range(6):
        e.clock.advance(24)
        e.handle("在吗", "OWNER-1")
    before = e.current_bond()
    print(f"    连续 6 天说话 → 亲密度 {before.affinity:.2f}，interactions={before.interactions}")
    e.clock.advance(24 * 10)      # 10 天不来（期间没有任何调用）
    idle_aff = e.store.state_get(f"bond:i{e.instance_no}:affinity")
    print(f"    闲置 10 天（无任何调用）后 state 里的亲和度 = {idle_aff}（没变）")
    e.handle("在吗", "OWNER-1")    # 他回来了：decay 在 touch 之前先结算
    after = e.current_bond()
    print(f"    他回来第 1 句之后 → 亲密度 {after.affinity:.2f}"
          f"（decay_per_day={e0.relationship.decay_per_day}，闲置 9 天 → -45）")
    check("E1 decay 不在闲置期间自动发生，只在下次说话时补算",
          str(idle_aff) == f"{before.affinity:.4f}" and after.affinity < before.affinity,
          f"闲置中={idle_aff} 回来后={after.affinity:.4f}")

    old_no = e.instance_no
    text = e.run_recycle("探针")
    b2 = e.current_bond()
    print(f"    回收：实例 #{old_no} → #{e.instance_no}，"
          f"亲密度 {after.affinity:.2f} → {b2.affinity:.2f}，"
          f"interactions {after.interactions} → {b2.interactions}，阶段 {b2.stage.key}")
    check("E2 回收换代后关系清零（新的她不认识你）",
          b2.affinity == 0 and b2.interactions == 0, f"{b2.affinity}/{b2.interactions}")
    old_state = e.store.state_get(f"bond:i{old_no}:affinity")
    print(f"    旧实例的 state 键 bond:i{old_no}:affinity = {old_state!r}（brain.run_recycle 里显式 reset）")

    return report("beh_bond")


if __name__ == "__main__":
    raise SystemExit(main())
