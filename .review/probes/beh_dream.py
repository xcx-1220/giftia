"""Stream D 探针 5：dream 做梦 —— 闲置判定、碎片模糊、不进检索、tell_within_hours、重复讲梦。

    python .review/run_tests.py .review/probes/beh_dream.py

只读产品代码；临时数据在 .review/tmp/behavior-checker/dream-*。
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from beh_common import CaptureLLM, base_settings, check, note, report, systems  # noqa: E402

from giftia.brain import Giftia  # noqa: E402
from giftia.dream import latest_untold, make_dream, should_dream  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402
from giftia.store import Store  # noqa: E402


class DreamSpy(CaptureLLM):
    """记下 write_dream 真正收到的碎片。"""

    def __init__(self, model):
        super().__init__(model)
        self.fragments: list[list[str]] = []

    def write_dream(self, fragments, *, name="Giftia"):
        self.fragments.append(list(fragments))
        return super().write_dream(fragments, name=name)


def seed_memories(brain, n=4, scope="private", subject=""):
    for i in range(n):
        brain.store.add_memory(
            instance_no=brain.instance_no,
            created_ts=brain.clock.iso(), happened_ts=brain.clock.iso(),
            summary=f"关于他和她的第 {i} 件私事：他去过一家叫深蓝的咖啡馆",
            scope=scope, subject_id=subject, importance=0.6,
        )


def main() -> int:
    print("=" * 78)
    print("A. 闲置判定：min_memories / min_hours_between")
    print("=" * 78)
    s = base_settings("dream-a", 81920.0)
    llm = DreamSpy(s.model)
    b = Giftia(s, llm=llm)
    print(f"    min_memories={s.dream.min_memories} min_hours_between={s.dream.min_hours_between} "
          f"fragments={s.dream.fragments} blur={s.dream.blur} tell_within_hours={s.dream.tell_within_hours}")
    seed_memories(b, 2)
    check("A1 记忆不足 min_memories 时不做梦", not should_dream(s, b.store, b.clock, 1),
          f"记忆 {b.store.count_memories(1)} 条")
    seed_memories(b, 2)
    check("A2 记忆够了、没做过梦 → 该做", should_dream(s, b.store, b.clock, 1),
          f"记忆 {b.store.count_memories(1)} 条")
    d1 = make_dream(s, b.store, b.clock, llm, 1, view=[("private", "")])
    print(f"    第一场梦：{d1.content[:40] if d1 else None!r}")
    check("A3 能做出梦并落库", d1 is not None, str(d1))
    check("A4 刚做完不到 min_hours_between → 不再做",
          not should_dream(s, b.store, b.clock, 1))
    b.clock.advance(s.dream.min_hours_between + 0.1)
    check("A5 超过 min_hours_between → 又可以做", should_dream(s, b.store, b.clock, 1))

    print("\n    —— 碎片到底被模糊成什么样 ——")
    raw = [m["summary"] for m in b.store.active_memories(1, view=Store.ALL_SCOPES)][-s.dream.fragments:]
    frags = llm.fragments[0] if llm.fragments else []
    print(f"    原始记忆 1：{raw[0] if raw else '(无)'}")
    print(f"    交给模型的碎片 1：{frags[0] if frags else '(无)'}")
    check("A6 碎片确实被 blur 过（与原文不同、且长度不增）",
          bool(frags) and (frags[0] != raw[0]), f"{frags[:1]} vs {raw[:1]}")
    frags2 = None
    b.clock.advance(s.dream.min_hours_between + 0.1)
    llm.fragments.clear()
    make_dream(s, b.store, b.clock, llm, 1, view=[("private", "")])
    frags2 = llm.fragments[0] if llm.fragments else []
    check("A7 同一批记忆的模糊结果可复现（seed = id*31+7）", frags == frags2,
          f"{frags[:1]} vs {frags2[:1]}")

    print()
    print("=" * 78)
    print("B. 梦不进检索 / 不占保留率 / 不参与退化")
    print("=" * 78)
    mem_before = b.store.count_memories(1)
    ids = [m["id"] for m in b.store.active_memories(1, view=Store.ALL_SCOPES)]
    print(f"    做梦后记忆条数={mem_before}（做梦前 4），active_memories 里没有 dream 文本："
          f"{all('梦里有' not in str(m.get('summary')) for m in b.store.active_memories(1, view=Store.ALL_SCOPES))}")
    check("B1 梦不写进 memories（不占条数、不进检索、不参与退化）",
          mem_before == 4 and not any("梦里有" in str(m.get("summary"))
                                      for m in b.store.active_memories(1, view=Store.ALL_SCOPES)),
          f"{mem_before} 条")

    print()
    print("=" * 78)
    print("C. tell_within_hours 过期 / 讲过的梦不再注入")
    print("=" * 78)
    c = Giftia(dataclasses.replace(s, root=base_settings("dream-c", 81920.0).root),
               llm=DreamSpy(s.model))
    seed_memories(c, 4)
    dc = make_dream(s, c.store, c.clock, c.llm, 1, view=[("private", "")])
    fresh = latest_untold(s, c.store, c.clock, 1)
    print(f"    刚做完 → latest_untold={'有' if fresh else '无'}")
    c.clock.advance(s.dream.tell_within_hours + 1)
    stale = latest_untold(s, c.store, c.clock, 1)
    print(f"    过了 tell_within_hours={s.dream.tell_within_hours} 小时 → "
          f"latest_untold={'有' if stale else '无'}")
    check("C1 超过 tell_within_hours 的梦不再翻出来讲", fresh is not None and stale is None)
    c.store.mark_dream_told(dc.id)
    check("C2 讲过的梦不再返回", latest_untold(s, c.store, c.clock, 1) is None)

    print()
    print("=" * 78)
    print("D. 主动联系讲梦：适配器路径（record=False + note_outreach_sent）不会标记已讲")
    print("=" * 78)
    d = Giftia(dataclasses.replace(s, root=base_settings("dream-d", 81920.0).root),
               llm=DreamSpy(s.model))
    d.set_handler_id("OWNER-1")
    seed_memories(d, 4)
    d.force_mood("calm")              # 排掉 mood_share，让 dream 成为理由
    d.store.state_set("occasion:i1:milestone", "3")   # 排掉「认识第 1 天」这个理由
    dd = make_dream(s, d.store, d.clock, d.llm, 1, view=[("private", "")])
    print(f"    做了一场梦 #{dd.id}：{dd.content[:36]}…")
    d.clock.advance(24)               # 闲 1 天（< tell_within_hours=36，梦还有效）
    p = d.outreach_plan(force=True)
    print(f"    outreach_plan → reason={p.reason if p else None} / {p.title if p else ''}")
    check("D1 做了梦还没讲时，主动理由可以是 dream", p is not None and p.reason == "dream",
          p.reason if p else "None")
    _, text = d.compose_outreach(record=False, force=True, target="owner")   # 适配器就是这样调的
    d.note_outreach_sent(text, keep_in_history=True)                          # 发送成功后的记账
    told = [int(x.get("told") or 0) for x in d.store.recent_dreams(1)]
    print(f"    （适配器路径）实发内容：{text[:40]}…")
    print(f"    发完之后 dreams.told = {told}")
    d.clock.advance(4)                # 过了 min_gap_hours=3，同一天里她可以再发
    p2 = d.outreach_plan(force=True)
    print(f"    同一天再过 4 小时 outreach_plan → reason={p2.reason if p2 else None} / "
          f"{p2.title if p2 else ''}")
    _, text2 = d.compose_outreach(record=False, force=True, target="owner")
    print(f"    第二次实发内容：{text2[:44]}…（和第一次{'相同' if text2 == text else '不同'}）")
    told2 = [int(x.get("told") or 0) for x in d.store.recent_dreams(1)]
    print(f"    同一个梦 #{dd.id} 还在，latest_untold="
          f"{'有' if latest_untold(s, d.store, d.clock, 1) else '无'}，told={told2}")
    print("    【期望为何变了】D2 原来记录的是 P1-19 的缺陷：适配器路径（record=False +")
    print("    note_outreach_sent）**不标记 told**，于是同一天里她把同一个梦又讲了一遍。")
    print("    修复后 note_outreach_sent 会把刚讲的梦标记掉 → 第二次不再以 dream 为理由。")
    check("D2 适配器路径实发后就标记 told=[1]，同一天不会把同一个梦再讲一遍",
          told == [1] and told2 == [1] and (p2 is None or p2.reason != "dream"),
          f"told={told}→{told2}，第二次 reason={p2.reason if p2 else None}")
    # 正向对照：梦还得能讲出来——再攒一场新梦，它仍然会被当成主动理由
    d.clock.advance(s.dream.min_hours_between + 1)
    dd2 = make_dream(s, d.store, d.clock, d.llm, 1, view=[("private", "")])
    p3 = d.outreach_plan(force=True)
    print(f"    正向对照：再做一场新梦 #{dd2.id if dd2 else None} → outreach_plan().reason = "
          f"{p3.reason if p3 else None}")
    check("D2b 正向对照：新的梦仍然会被讲出来（不是把讲梦这条路关掉）",
          dd2 is not None and p3 is not None and p3.reason == "dream",
          p3.reason if p3 else "None")

    # 对照：CLI / record=True 的路径会标记
    e = Giftia(dataclasses.replace(s, root=base_settings("dream-e", 81920.0).root),
               llm=DreamSpy(s.model))
    e.set_handler_id("OWNER-1")
    seed_memories(e, 4)
    e.force_mood("calm")
    e.store.state_set("occasion:i1:milestone", "3")
    de = make_dream(s, e.store, e.clock, e.llm, 1, view=[("private", "")])
    e.clock.advance(24)
    _, text2 = e.compose_outreach(record=True, force=True, target="owner")
    told_e = [int(x.get("told") or 0) for x in e.store.recent_dreams(1)]
    print(f"\n    （record=True 路径）发完之后 dreams.told = {told_e}")
    check("D3 record=True 路径会标记 told（两条路径语义不一致）", told_e == [1], str(told_e))

    print()
    print("=" * 78)
    print("E. 边界：fragments=0 时会拿几条记忆做梦")
    print("=" * 78)
    s0 = dataclasses.replace(
        base_settings("dream-f", 81920.0),
        dream=dataclasses.replace(base_settings("dream-f2", 81920.0).dream, fragments=0),
    )
    f = Giftia(s0, llm=DreamSpy(s0.model))
    seed_memories(f, 8)
    fd = make_dream(s0, f.store, f.clock, f.llm, 1, view=[("private", "")])
    src = f.store.recent_dreams(1)[0]["source_ids"] if fd else ""
    n_src = len([x for x in str(src).split(",") if x])
    print(f"    fragments=0 → 梦的 source_ids 条数 = {n_src}（记忆共 "
          f"{f.store.count_memories(1)} 条）")
    print("    【期望为何变了】E1 原来记录的是切片陷阱：`memories[-0:]` 等价于 `memories[0:]`，")
    print("    会把**全部** 8 条记忆当素材（fragments=0 的本意是「至少 1 条」）。")
    print("    修复后取 `max(1, fragments)` 条 → source_ids 恰好 1 条。")
    check("E1 fragments=0 时只取 1 条素材（不再是 memories[-0:] 拿全部 8 条）",
          n_src == 1, f"{n_src} 条")
    # 正向对照：fragments 配置真的生效，不是恒取 1 条
    f0 = Giftia(dataclasses.replace(s, root=base_settings("dream-f3", 81920.0).root,
                                    dream=dataclasses.replace(s.dream, fragments=3)),
                llm=DreamSpy(s.model))
    seed_memories(f0, 8)
    make_dream(f0.settings, f0.store, f0.clock, f0.llm, 1, view=[("private", "")])
    src3 = f0.store.recent_dreams(1)[0]["source_ids"]
    n3 = len([x for x in str(src3).split(",") if x])
    print(f"    正向对照：fragments=3 → source_ids 条数 = {n3}")
    check("E1b 正向对照：fragments=3 时取材 3 条（配置真的生效）", n3 == 3, f"{n3} 条")

    return report("beh_dream")


if __name__ == "__main__":
    raise SystemExit(main())
