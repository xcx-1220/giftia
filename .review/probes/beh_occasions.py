"""Stream D 探针 4：occasions 纪念日 / 大日子 / 回访 —— 边界、去重、群聊可见性。

    python .review/run_tests.py .review/probes/beh_occasions.py

只读产品代码；临时数据在 .review/tmp/behavior-checker/occ-*。
"""

from __future__ import annotations

import dataclasses
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from beh_common import CaptureLLM, base_settings, check, note, report, systems  # noqa: E402

from giftia.brain import Giftia  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402
from giftia.occasions import _parse_event_date, find_occasions  # noqa: E402


def add_mem(brain, summary, event_date, scope="private", subject=""):
    return brain.store.add_memory(
        instance_no=brain.instance_no,
        created_ts=brain.clock.iso(), happened_ts=brain.clock.iso(),
        summary=summary, event_date=event_date, scope=scope, subject_id=subject,
        importance=0.7,
    )


def main() -> int:
    print("=" * 78)
    print("A. 纪念日：第 N 天是 0 基还是 1 基 / 边界 / 只提一次 / 按实例")
    print("=" * 78)
    s = base_settings("occ-a", 81920.0)
    b = Giftia(s, llm=MockLLM(s.model))
    now = b.clock.now()
    for dk in (0.0, 0.5, 0.99, 1.0, 1.99, 2.0, 2.99, 3.0):
        occ = find_occasions(s, b.store, now=now, instance_no=1, days_known=dk, mark=False)
        ms = [o.title for o in occ if o.kind == "milestone"]
        print(f"    days_known={dk:<5} → {ms[0] if ms else '（无）'}")
    check("A1 days_known<1 不算第 1 天（认识当天不庆祝）",
          not [o for o in find_occasions(s, b.store, now=now, instance_no=1,
                                         days_known=0.99, mark=False) if o.kind == "milestone"])
    # 只提一次
    first = [o for o in find_occasions(s, b.store, now=now, instance_no=1,
                                       days_known=1.0, mark=True) if o.kind == "milestone"]
    second = [o for o in find_occasions(s, b.store, now=now, instance_no=1,
                                        days_known=1.0, mark=True) if o.kind == "milestone"]
    print(f"    第一次 mark=True → {[o.title for o in first]}；第二次 → {[o.title for o in second]}")
    check("A2 同一天只提一次（celebrated 记账）", len(first) == 1 and len(second) == 0)
    # 跨过多个里程碑：只报最高那个
    jumped = [o for o in find_occasions(s, b.store, now=now, instance_no=1,
                                        days_known=8.2, mark=True) if o.kind == "milestone"]
    print(f"    直接从第 0 天跳到第 8 天 → {jumped[0].title if jumped else '（无）'}")
    check("A3 跨过多个里程碑时报「跨过」最高的那个（不补报 3/7 天）", len(jumped) == 1)
    # 按实例分键
    other_inst = [o for o in find_occasions(s, b.store, now=now, instance_no=2,
                                            days_known=8.2, mark=False) if o.kind == "milestone"]
    print(f"    同一个库、实例 #2 在第 8 天 → {other_inst[0].title if other_inst else '（无）'}")
    check("A4 纪念日按实例分键（换代后重新数）", len(other_inst) == 1)

    print()
    print("=" * 78)
    print("B. 他的大日子：lookahead 边界 / 措辞 / 前一天提过之后当天还提不提")
    print("=" * 78)
    b2 = Giftia(dataclasses.replace(s, root=base_settings("occ-b", 81920.0).root),
                llm=MockLLM(s.model))
    today = b2.clock.now().date()
    for off in (-1, 0, 1, 2, 3):
        d = (today + timedelta(days=off)).isoformat()
        mid = add_mem(b2, f"偏移{off:+d} 的事", d)
        occ = find_occasions(s, b2.store, now=b2.clock.now(), instance_no=1,
                             days_known=5.0, mark=False)
        mine = [o for o in occ if o.memory_id == mid]
        print(f"    event_date 偏移 {off:+d} → {[o.title for o in mine] or '（不提醒）'}")
    got = find_occasions(s, b2.store, now=b2.clock.now(), instance_no=1, days_known=5.0, mark=False)
    titles = [o.title for o in got]
    big = [o.title for o in got if o.kind == "big_day"]
    print(f"    big_day 汇总：{big}")
    check("B1 lookahead=1：big_day 只提醒今天和明天（+2/+3 不提醒；-1 走『事后回访』）",
          any(t.startswith("今天：") for t in big) and any(t.startswith("明天：") for t in big)
          and not any("偏移+2" in t for t in big) and not any("偏移+3" in t for t in big)
          and not any("偏移-1" in t for t in big),
          str(big))
    check("B1b 同一条昨天的记忆走 follow_up（回访），不是 big_day",
          any(o.kind == "follow_up" and o.title.startswith("昨天那件事") for o in got),
          str([(o.kind, o.title) for o in got]))

    # 前一天提过 → 当天还提不提？
    b3 = Giftia(dataclasses.replace(s, root=base_settings("occ-b2", 81920.0).root),
                llm=MockLLM(s.model))
    tomorrow = (b3.clock.now().date() + timedelta(days=1)).isoformat()
    mid = add_mem(b3, "明天要考驾照", tomorrow)
    day1 = [o.title for o in find_occasions(s, b3.store, now=b3.clock.now(), instance_no=1,
                                            days_known=5.0, mark=True) if o.memory_id == mid]
    b3.clock.advance(24)
    day2 = [o.title for o in find_occasions(s, b3.store, now=b3.clock.now(), instance_no=1,
                                            days_known=6.0, mark=True) if o.memory_id == mid]
    print(f"    前一天 mark=True → {day1}；第二天（正日子）→ {day2 or '（不提醒）'}")
    print("    【期望为何变了】B2 原来断言『前一天提过之后，正日子当天不再提』")
    print("    （PASS = 去重键只有 id+event_date）。qa-verifier / behavior-checker 都报了这条：")
    print("    『明天要考驾照』提醒一次之后，正日子当天反而一句话都没有。")
    print("    修复后去重键 = id+event_date+今天，所以前一天与当天各提一次（措辞不同）。")
    check("B2 前一天提过之后，正日子当天**仍会**提一次（去重键 = id+日期+今天，见 F 段）",
          len(day1) == 1 and len(day2) == 1
          and any(t.startswith("明天：") for t in day1) and any(t.startswith("今天：") for t in day2),
          f"{day1} / {day2}")

    print()
    print("=" * 78)
    print("C. 事后回访：follow_up_days 边界与措辞")
    print("=" * 78)
    c = Giftia(dataclasses.replace(s, root=base_settings("occ-c", 81920.0).root),
               llm=MockLLM(s.model))
    t0 = c.clock.now().date()
    for back in (1, 2, 3):
        add_mem(c, f"{back} 天前的事", (t0 - timedelta(days=back)).isoformat())
    occ = find_occasions(s, c.store, now=c.clock.now(), instance_no=1, days_known=5.0, mark=False)
    fus = [o.title for o in occ if o.kind == "follow_up"]
    print(f"    昨天/2 天前/3 天前 → {fus}")
    check("C1 follow_up_days=2：只回访昨天和前天，3 天前不问了",
          any(t.startswith("昨天那件事") for t in fus) and any("2 天前" in t for t in fus)
          and not any("3 天前" in t for t in fus), str(fus))

    print()
    print("=" * 78)
    print("D. 群聊可见性：私聊说的大日子会不会被带进群")
    print("=" * 78)
    d = Giftia(dataclasses.replace(s, root=base_settings("occ-d", 81920.0).root),
               llm=CaptureLLM(s.model))
    d.set_handler_id("OWNER-1")
    d.set_last_group("GROUP-1")
    tom = (d.clock.now().date() + timedelta(days=1)).isoformat()
    add_mem(d, "搭档明天要去面试", tom, scope="private")
    add_mem(d, "小雨明天要去医院做手术", tom, scope="person", subject="OTHER-9")
    add_mem(d, "阿伟明天要考驾照", tom, scope="group", subject="GROUP-1")
    add_mem(d, "陌生人小刚后天要搬家", tom, scope="person", subject="OTHER-8")

    view_group = find_occasions(s, d.store, now=d.clock.now(), instance_no=1, days_known=5.0,
                                mark=False, view=[("group", "GROUP-1")])
    print(f"    find_occasions(view=[('group','GROUP-1')]) → {[o.title for o in view_group]}")
    check("D1 显式传 view 时只看到本群公开的大日子（occasions 自己的闸门是对的）",
          all("小雨" not in o.title and "搭档" not in o.title for o in view_group)
          and any("阿伟" in o.title for o in view_group),
          str([o.title for o in view_group]))

    plan_grp = d.outreach_plan(force=True, for_group=True)
    print(f"    outreach_plan(for_group=True) → {plan_grp.reason if plan_grp else None} / "
          f"{plan_grp.title if plan_grp else ''}")
    check("D2 计划层的群理由只用本群的大日子",
          plan_grp is not None and "小雨" not in plan_grp.title and "搭档" not in plan_grp.title,
          plan_grp.title if plan_grp else "None")

    llm = d.llm
    llm.seen.clear()
    plan_, text = d.compose_outreach(force=True, target="group")
    prompt = systems(llm)
    leaked = [w for w in ("小雨", "搭档明天要去面试", "小刚") if w in prompt]
    print(f"    compose_outreach(target='group') 的 system 提示词里出现：{leaked}")
    print("    —— 提示词片段 ——")
    idx = prompt.find("今天不只是普通的一天")
    print("    " + (prompt[idx:idx + 260].replace("\n", "\n    ") if idx >= 0 else "（没有这一天块）"))
    print("    【期望为何变了】D3 原来是「记录 bug」：群发提示词里出现别人的私聊/搭档私事就算 PASS。")
    print("    team-brain 修完 P0-4 后这条路径不再泄漏（实测命中 []），所以期望翻转为")
    print("    「群发提示词里**不得**出现任何非本群归属的大日子」；D1 的群视角正向对照仍然保留。")
    check("D3 【隐私】群发提示词里不得出现别人的私聊/搭档私事的大日子（小雨/小刚/搭档）",
          not leaked, f"命中 {leaked}")

    # 对照：正常群聊（handle → _prepare）走的是 memory_view，没漏
    d.llm.seen.clear()
    d.handle("大家早", "GUEST-1", scene="group", group_id="GROUP-1", speaker_name="阿伟")
    chat_prompt = systems(d.llm)
    leaked_chat = [w for w in ("小雨", "搭档明天要去面试", "小刚") if w in chat_prompt]
    print(f"    正常群聊 _prepare 的 system 提示词里出现：{leaked_chat}")
    check("D4 对照：正常群聊路径也没有漏（两条群路径现在都守住归属）",
          not leaked_chat, str(leaked_chat))

    print()
    print("=" * 78)
    print("E. 日期解析边界")
    print("=" * 78)
    print("    【期望为何变了】E1 原来写着『10月20号』『下周三』『2026/10/11』")
    print("    应当静默作废（PASS = 解析不出来）。修复批次把这几条列为 P2：")
    print("    模型/整理链路写进 event_date 的不一定是严格 ISO，认不出来就等于")
    print("    “那天她不会想起来”。所以 E1 改成：常见写法要**能解析出正确的日期**，")
    print("    而真正非法的（2026-13-01 / 空白 / None）仍然返回 None 且不报错。")
    today = date(2026, 10, 10)      # 周六，用来验证相对星期
    for raw in ("2026-10-11", "2026-10-11T09:00:00", "2026-2-30", "10月20号", "20261011",
                "", " 2026-10-11 ", None, "2026-13-01", "下周三", "2026/10/11",
                "2026年10月11日", "10/20", "周六", "明天"):
        print(f"    _parse_event_date({raw!r}, today=2026-10-10) = {_parse_event_date(raw, today)}")
    check("E1 常见写法能解析成正确的日期（『2026/10/11』『10月20号』『下周三』）",
          _parse_event_date("2026/10/11", today) == date(2026, 10, 11)
          and _parse_event_date("10月20号", today) == date(2026, 10, 20)
          and _parse_event_date("下周三", today) == date(2026, 10, 21),
          f"{_parse_event_date('2026/10/11', today)}/"
          f"{_parse_event_date('10月20号', today)}/{_parse_event_date('下周三', today)}")
    check("E1b 非法/缺失的日期仍然静默作废（返回 None，不抛错）",
          _parse_event_date("2026-13-01", today) is None
          and _parse_event_date("2026-2-30", today) is None
          and _parse_event_date("", today) is None
          and _parse_event_date(None, today) is None
          and _parse_event_date("明天", today) is None,
          f"{_parse_event_date('2026-13-01', today)}/{_parse_event_date('2026-2-30', today)}")
    check("E1c ISO 与紧凑写法没被新解析器弄坏",
          _parse_event_date("2026-10-11", today) == date(2026, 10, 11)
          and _parse_event_date("20261011", today) == date(2026, 10, 11)
          and _parse_event_date("2026-10-11T09:00:00", today) == date(2026, 10, 11))

    print()
    print("=" * 78)
    print("F. 去重键带上“提醒发生在哪一天”（前一天提过 → 正日子当天还要提）")
    print("=" * 78)
    f = Giftia(dataclasses.replace(s, root=base_settings("occ-f", 81920.0).root),
               llm=MockLLM(s.model))
    tomorrow = (f.clock.now().date() + timedelta(days=1)).isoformat()
    mid = add_mem(f, "明天要考驾照", tomorrow)
    d1 = [o.title for o in find_occasions(s, f.store, now=f.clock.now(), instance_no=1,
                                          days_known=5.0, mark=True) if o.memory_id == mid]
    f.clock.advance(24)
    d2 = [o.title for o in find_occasions(s, f.store, now=f.clock.now(), instance_no=1,
                                          days_known=6.0, mark=True) if o.memory_id == mid]
    print(f"    前一天（“明天考试”）→ {d1}；正日子当天（“今天考试”）→ {d2 or '（不提醒）'}")
    check("F1 前一天提过之后，正日子当天还会再提一次（去重键 = id+日子+今天）",
          len(d1) == 1 and len(d2) == 1, f"{d1} / {d2}")

    g = Giftia(dataclasses.replace(s, root=base_settings("occ-g", 81920.0).root),
               llm=MockLLM(s.model))
    past = (g.clock.now().date() - timedelta(days=1)).isoformat()
    mid2 = add_mem(g, "昨天考驾照", past)
    g1 = [o.title for o in find_occasions(s, g.store, now=g.clock.now(), instance_no=1,
                                          days_known=5.0, mark=True) if o.memory_id == mid2]
    g.clock.advance(24)
    g2 = [o.title for o in find_occasions(s, g.store, now=g.clock.now(), instance_no=1,
                                          days_known=6.0, mark=True) if o.memory_id == mid2]
    print(f"    事后第 1 天（“昨天那件事”）→ {g1}；第 2 天（“2 天前那件事”）→ {g2 or '（不追问）'}")
    check("F2 事后第 1 天问过之后，第 2 天还能再回访一次（follow_up 去重键同样带上今天）",
          len(g1) == 1 and len(g2) == 1, f"{g1} / {g2}")

    return report("beh_occasions")


if __name__ == "__main__":
    raise SystemExit(main())
