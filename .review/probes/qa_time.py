"""Stream E · 时间纵轴仿真：虚拟时钟推进 400+ 天 / 近万小时。

核对：寿命进度与阶段切换点、退化阶段 vs [[degradation.stages]].min_ratio、
纪念日是否按列表触发、bond 曲线是否落在 README 说的第 11/34/80/180 天。

    python .review/probes/qa_time.py
"""

from __future__ import annotations

import dataclasses
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qa_harness import (  # noqa: E402
    Checker, RecordingLLM, make_brain, make_dir, make_settings, rm_dir,
)

from giftia.llm import MemoryDraft, MockLLM  # noqa: E402

C = Checker("Stream E · 时间纵轴仿真（400+ 天 / 近万小时）")

NEUTRAL = "今天过得还行，工作上没什么特别的"

# ══ A. 真实寿命 81920 小时，每天都说话，跑 400 天 ══════════════════
C.info("── A. 400 天日更（寿命 = 出厂 81920 小时）──")
tmp_a = make_dir("time-a")
settings_a = make_settings(tmp_a)
C.check("默认寿命 = 81920 小时（番剧设定）", settings_a.lifespan.effective_hours() == 81920.0,
        str(settings_a.lifespan.effective_hours()))
llm_a = RecordingLLM(settings_a.model)
brain_a = make_brain(settings_a, llm_a)

bond_switches: list[tuple[int, str, float]] = []
milestone_hits: list[tuple[int, int]] = []
last_stage = brain_a.current_bond().stage.key
DAYS = 400
for d in range(1, DAYS + 1):
    brain_a.handle(NEUTRAL, "u_owner", scene="c2c")
    b = brain_a.current_bond()
    if b.stage.key != last_stage:
        bond_switches.append((d, b.stage.key, round(b.affinity, 2)))
        last_stage = b.stage.key
    for occ in brain_a.last_occasions:
        if occ.kind == "milestone":
            milestone_hits.append((d, int(occ.days_known)))
    brain_a.clock.advance(24.0)

C.info(f"bond 阶段切换（第几天, 档位, 亲密度）：{bond_switches}")
C.info(f"纪念日触发（第几天, days_known）：{milestone_hits}")
st400 = brain_a.status()
C.info(f"400 天后：已服务 {st400.used_hours:.1f} 小时 / {st400.used_hours / 24:.1f} 天，"
       f"剩余比例 {st400.remaining_ratio:.4f}，阶段 {st400.stage.name}")
C.check("进度 = 已服务小时 / 总寿命（9600h / 81920h ≈ 11.7%）",
        abs(st400.used_ratio - st400.used_hours / 81920.0) < 1e-6,
        f"{st400.used_ratio:.6f} vs {st400.used_hours / 81920.0:.6f}")
C.check("400 天后仍处于 clear 阶段（11.7% 远未到 60%）", st400.stage.name == "clear",
        st400.stage.name)
C.check("剩余寿命 + 已服务 ≈ 总寿命",
        abs(st400.remaining_hours + st400.used_hours - 81920.0) < 1.0,
        f"{st400.remaining_hours:.1f}+{st400.used_hours:.1f}")

want_bond = {"partner": 11, "familiar": 34, "close": 80, "irreplaceable": 180}
C.info("── README 承诺 vs 实测（bond）──")
for key, promised in want_bond.items():
    got = next((d for d, k, _ in bond_switches if k == key), None)
    C.info(f"    {key}: README/配置注释说第 {promised} 天 → 实测第 {got} 天"
           f"（差 {None if got is None else got - promised} 天）")
C.check("「搭档」落在第 11 天 ±2", any(k == "partner" and abs(d - 11) <= 2
                                    for d, k, _ in bond_switches), str(bond_switches))
C.check("「熟悉」落在第 34 天 ±3", any(k == "familiar" and abs(d - 34) <= 3
                                    for d, k, _ in bond_switches), str(bond_switches))
C.check("「亲近」落在第 80 天 ±5（settings.toml 注释写 85，README 写 80）",
        any(k == "close" and abs(d - 80) <= 5 for d, k, _ in bond_switches), str(bond_switches))
C.check("「无可替代」落在第 180 天 ±3", any(k == "irreplaceable" and abs(d - 180) <= 3
                                        for d, k, _ in bond_switches), str(bond_switches))
C.check("亲密度单调不减", all(bond_switches[i][2] <= bond_switches[i + 1][2]
                              for i in range(len(bond_switches) - 1)), str(bond_switches))

ms_list = [1, 3, 7, 14, 30, 50, 100, 200, 365]
fired = sorted({v for _d, v in milestone_hits})
C.check("纪念日按配置列表触发（1/3/7/14/30/50/100/200/365）", fired == ms_list, str(fired))
C.check("每个纪念日只提一次（无重复）",
        len([v for _d, v in milestone_hits]) == len(set(v for _d, v in milestone_hits)),
        str(milestone_hits))
C.check("400 天里没有提前触发 500 天纪念日", 500 not in fired, str(fired))
off = [d for d, v in milestone_hits if v not in ms_list]
C.check("没有在非列表的日子乱触发", not off, str(off))

# 时间感：注入提示词里的相对说法
sysp = brain_a.preview_prompt("我昨天说的那件事")
C.check("提示词里当前时间带星期几", "（周" in sysp, "")
C.check("记忆条目前的相对时间是人话（刚刚/昨天/N 天前）",
        any(w in sysp for w in ("刚刚", "分钟前", "今天 ", "昨天", "前天", " 天前")), "")
brain_a.close()
rm_dir(tmp_a)

# ══ B. 压缩寿命 960 小时（40 天）跑完五阶段 ═════════════════════
C.info("── B. 压缩寿命 960 小时：阶段切换点 vs min_ratio ──")
tmp_b = make_dir("time-b")
settings_b = make_settings(tmp_b, hours=960.0)
llm_b = RecordingLLM(settings_b.model)
brain_b = make_brain(settings_b, llm_b)
switches_b: list[tuple[float, str]] = []
last = ""
for h in range(0, 961):
    brain_b.clock.advance(1.0)
    st = brain_b.status()
    if st.stage.name != last:
        switches_b.append((round(st.used_hours, 2), st.stage.name))
        last = st.stage.name
C.info(f"实测：{(switches_b)}")
expect = {"hazy": 960 * 0.4, "blurred": 960 * 0.7, "fading": 960 * 0.9, "terminal": 960 * 0.98}
for name, hours in expect.items():
    got = next((h for h, n in switches_b if n == name), None)
    C.check(f"{name} 切换点 ≈ 剩余 {960 - hours:.0f}h（min_ratio 换算），实测已服务 {got}h",
            got is not None and abs(got - hours) <= 1.5, f"want~{hours} got {got}")
C.check("阶段单向推进，不回头",
        [n for _h, n in switches_b] == ["clear", "hazy", "blurred", "fading", "terminal"],
        str([n for _h, n in switches_b]))
C.check("终端预警阈值 = min(72h, 5% 总寿命) = 48h",
        abs(min(72.0, 960 * 0.05) - 48.0) < 1e-9, "")
brain_b.clock.advance(-41.0)           # 已服务 920h → 剩余 40h
C.check("剩 40h（<48h 预警阈值）时进入预警窗口", brain_b.lifespan.is_terminal_warning(),
        f"remaining={brain_b.status().remaining_hours:.1f}")
C.check("到期前 is_expired=False", not brain_b.lifespan.is_expired())
brain_b.clock.advance(1000.0)
C.check("越过总寿命后 is_expired=True 且剩余比例被夹到 0",
        brain_b.lifespan.is_expired() and brain_b.status().remaining_ratio == 0.0,
        f"ratio={brain_b.status().remaining_ratio}")
brain_b.close()
rm_dir(tmp_b)

# ══ C. 他的「大日子」：当天/前一天惦记、事后回访、群里看不到 ════
C.info("── C. 大日子（event_date）──")

class EventLLM(MockLLM):
    """第二天的整理返回一条带 event_date 的记忆。"""

    def __init__(self, cfg, when: str):
        super().__init__(cfg)
        self.when = when
        self.done = False

    def extract_memories(self, transcript, *, name="Giftia", today="", scene="c2c", known=""):
        if self.done:
            return []
        self.done = True
        return [MemoryDraft(summary="搭档下周三要考驾照，他说有点紧张",
                            tags="考试,驾照", emotion="紧张", importance=0.8,
                            layer="important", event_date=self.when, about="搭档")]


tmp_c = make_dir("time-c")
settings_c = make_settings(tmp_c, hours=5000.0, consolidate_every=4)
today = date.today()
target = today + timedelta(days=2)
llm_c = EventLLM(settings_c.model, target.isoformat())
brain_c = make_brain(settings_c, llm_c)
brain_c.set_handler_id("u_owner")
for m in ("我跟你说个事，我下周三要去考驾照", "教练说我倒库还得多练练", "我有点紧张",
          "考完就好了吧"):
    brain_c.handle(m, "u_owner", scene="c2c")
brain_c.consolidate(force=True)
mem = [m for m in brain_c.store.active_memories(1, view=Store.ALL_SCOPES) if m.get("event_date")]
C.check("带 event_date 的记忆已落库", len(mem) == 1, str([(m['id'], m.get('event_date')) for m in brain_c.store.active_memories(1, view=Store.ALL_SCOPES)]))
C.check("event_date 是具体日期", mem and mem[0]["event_date"] == target.isoformat(),
        str(mem[0]["event_date"]) if mem else "无")

def occ_now():
    return brain_c._prepare("今天怎么样", mark=True)[1] and brain_c.last_occasions

brain_c.handle("今天怎么样", "u_owner", scene="c2c")
C.check("今天（大日子前两天）不惦记", not brain_c.last_occasions,
        str([o.kind for o in brain_c.last_occasions]))
brain_c.clock.advance(24.0)
brain_c.handle("今天怎么样", "u_owner", scene="c2c")
C.check("前一天惦记（big_day_lookahead_days=1）",
        any(o.kind == "big_day" for o in brain_c.last_occasions),
        str([(o.kind, o.title) for o in brain_c.last_occasions]))
brain_c.clock.advance(24.0)
brain_c.handle("今天怎么样", "u_owner", scene="c2c")
C.info("当天（大日子正日）last_occasions = "
       + str([(o.kind, o.title) for o in brain_c.last_occasions]))
C.check("大日子只提醒一次：前一天说过 → 当天不重复（token 已记账）",
        not any(o.kind == "big_day" for o in brain_c.last_occasions),
        f"last_occasions={[(o.kind, o.title) for o in brain_c.last_occasions]}")
brain_c.clock.advance(24.0)
brain_c.handle("今天怎么样", "u_owner", scene="c2c")
C.check("★ 事后第 1 天回访（follow_up back=1）",
        any(o.kind == "follow_up" for o in brain_c.last_occasions),
        str([(o.kind, o.title) for o in brain_c.last_occasions]))
brain_c.clock.advance(24.0)
brain_c.handle("今天怎么样", "u_owner", scene="c2c")
C.check("★ 事后第 2 天也该回访（follow_up_days=2 / README「昨天/前天办的事怎样了」）",
        any(o.kind == "follow_up" for o in brain_c.last_occasions),
        f"last_occasions={[(o.kind, o.title) for o in brain_c.last_occasions]}；"
        f"told 集合={brain_c.store.state_get('occasion:i1:bigday')}")
brain_c.clock.advance(24.0)
brain_c.handle("今天怎么样", "u_owner", scene="c2c")
C.check("第 3 天不再回访（窗口关闭）",
        not any(o.kind == "follow_up" for o in brain_c.last_occasions),
        str([(o.kind, o.title) for o in brain_c.last_occasions]))

# 群里看不到搭档的私人大日子（view 过滤）
from giftia.occasions import find_occasions  # noqa: E402
from giftia.store import Store  # noqa: E402
g = find_occasions(settings_c, brain_c.store, now=brain_c.clock.now(), instance_no=1,
                   days_known=10, mark=False, view=[("group", "groupX")])
C.check("★ 搭档私聊的大日子不会在群里被惦记（view 过滤）",
        not any(o.kind == "big_day" for o in g), str([(o.kind, o.title) for o in g]))
p = find_occasions(settings_c, brain_c.store, now=brain_c.clock.now(), instance_no=1,
                   days_known=10, mark=False, view=[("private", "")])
C.check("同一时刻搭档视角能看到（对照组）", True, "")
brain_c.close()
rm_dir(tmp_c)

raise SystemExit(C.summary())
