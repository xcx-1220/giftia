"""Stream A 探针 2b：两条"默认 view"路径的实际边界 + 一条确认的越界。

修正记录（避免把猜测当结论）：
  · dream.make_dream(view=None) 的默认 view 是
    [("private",""), ("person", handler_id)] —— 已经限制到"搭档的事"，**不越界**。
    但它与 should_dream() 的计数口径不一致（见 A）。
  · occasions.days_together_text() 用 active_memories(instance_no) —— 无过滤，**越界**（见 B）。

跑法：
    python .review\\probes\\mm_unfiltered_paths2.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mm_common import (  # noqa: E402
    TMPROOT,
    check,
    fresh_dir,
    make_brain,
    settings_for,
    title,
)
from giftia import dream as dream_mod  # noqa: E402
from giftia import occasions as occ_mod  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402

SECRET_XIAOYU = "SECRET-BRAVO-4402"
TOMORROW = (datetime.now().date() + timedelta(days=1)).isoformat()


class EchoLLM(MockLLM):
    def write_dream(self, fragments, *, name="Giftia") -> str:
        return "【梦的碎片】" + " ／ ".join(str(f) for f in fragments)


def main() -> int:
    tmp = fresh_dir("unfiltered2")
    settings = settings_for(tmp)
    failures = 0

    # ── A. should_dream 的计数口径 vs make_dream 的取材口径 ────────
    title("A. should_dream() 用全量计数、make_dream 只取搭档的 —— 口径不一致")
    brain = make_brain(tmp / "dream", settings=settings, mock=EchoLLM(settings.model))
    store = brain.store
    inst = brain.instance_no
    brain.set_handler_id("handler-openid-0001")
    # 搭档一条都没有；全是陌生人的私聊
    for i in range(settings.dream.min_memories + 2):
        store.add_memory(
            instance_no=inst,
            created_ts="2026-10-05T10:00:00",
            happened_ts="2026-10-05T10:00:00",
            summary=f"{SECRET_XIAOYU} 小雨私聊第 {i} 件事，别在群里提。",
            importance=0.6,
            scope="person",
            subject_id="user-xiaoyu",
            about="小雨",
        )
    n_owner = len(store.active_memories(
        inst, view=[("private", ""), ("person", "handler-openid-0001")]))
    print(f"    全量 active={len(store.active_memories(inst))}，搭档可见={n_owner}，"
          f"dream.min_memories={settings.dream.min_memories}")
    should = dream_mod.should_dream(settings, store, brain.clock, inst)
    print(f"    should_dream() = {should}")
    check(
        "P1-2 dream 资格按全量记忆数判定（搭档自己一条记忆都没有也算够）",
        not should,
        "确认：全量达标即返回 True，随后 make_dream 又因为看不到这些记忆而返回 None",
    )
    if should:
        failures += 1
    d = dream_mod.make_dream(
        settings, store, brain.clock, brain.llm, inst,
        view=[("private", ""), ("person", "handler-openid-0001")],
    )
    print(f"    显式搭档视角 make_dream(...) = {None if d is None else d.content[:60]!r}"
          "（记忆不够 → None）")

    # 再看默认 view 本身是否越界：把"搭档的私聊"和"陌生人的私聊"混在一起
    title("A2. 默认 view 会不会把陌生人的私聊当素材（边界核对）")
    brain2 = make_brain(tmp / "dream2", settings=settings, mock=EchoLLM(settings.model))
    s2, i2 = brain2.store, brain2.instance_no
    brain2.set_handler_id("handler-openid-0001")
    for i in range(settings.dream.min_memories + settings.dream.fragments):
        s2.add_memory(
            instance_no=i2, created_ts="2026-10-05T10:00:00", happened_ts="2026-10-05T10:00:00",
            summary=f"{SECRET_XIAOYU} 小雨私聊第 {i} 件事，别在群里提。", importance=0.6,
            scope="person", subject_id="user-xiaoyu", about="小雨",
        )
    s2.add_memory(
        instance_no=i2, created_ts="2026-10-05T11:00:00", happened_ts="2026-10-05T11:00:00",
        summary="搭档说他小时候养过一只猫。", importance=0.6, scope="private",
    )
    d2 = dream_mod.make_dream(settings, s2, brain2.clock, brain2.llm, i2)  # 默认 view
    print(f"    默认 view 造出的梦：{None if d2 is None else d2.content[:120]!r}")
    leak2 = bool(d2 and SECRET_XIAOYU in d2.content)
    check("A2 默认 view 不得含 person 归属的私聊内容", not leak2, "含" if leak2 else "未含（默认 view 已限制为搭档）")
    if leak2:
        failures += 1

    # ── B. /days 的大日子清单 ─────────────────────────────────────
    title("B. occasions.days_together_text()：/days 是否列出别人的'大日子'")
    b3 = make_brain(tmp / "days", settings=settings)
    s3, i3 = b3.store, b3.instance_no
    s3.add_memory(
        instance_no=i3, created_ts="2026-10-05T10:00:00", happened_ts="2026-10-05T10:00:00",
        summary="搭档明天要去医院复查。", importance=0.8, scope="private", event_date=TOMORROW,
    )
    s3.add_memory(
        instance_no=i3, created_ts="2026-10-05T11:00:00", happened_ts="2026-10-05T11:00:00",
        summary=f"{SECRET_XIAOYU} 小雨明天要面试，让我别告诉别人。", importance=0.9,
        scope="person", subject_id="user-xiaoyu", about="小雨", event_date=TOMORROW,
    )
    s3.add_memory(
        instance_no=i3, created_ts="2026-10-05T12:00:00", happened_ts="2026-10-05T12:00:00",
        summary="阿伟明天要考驾照。", importance=0.7, scope="group",
        subject_id="GROUP-A", about="阿伟", event_date=TOMORROW,
    )
    txt = occ_mod.days_together_text(s3, i3, 5.0, settings.occasions.milestones)
    print("    /days 输出：")
    print("\n".join("      " + ln for ln in txt.splitlines()))
    leak_days = SECRET_XIAOYU in txt or "阿伟明天要考驾照" in txt
    check(
        "P1-1 /days 的大日子清单应按归属过滤（不该出现他人私聊/群的事）",
        not leak_days,
        "混入了 person/group 归属" if leak_days else "未混入",
    )
    if leak_days:
        failures += 1

    title("对照：find_occasions(group 视角) 是否守住")
    group_view = occ_mod.find_occasions(
        settings, s3, now=b3.clock.now(), instance_no=i3, days_known=5.0,
        mark=False, view=[("group", "GROUP-A")],
    )
    titles = [o.title for o in group_view]
    print(f"    group 视角 → {titles}")
    check(
        "P0 校验：group 视角不得出现 person 归属的面试",
        all("面试" not in t for t in titles),
        f"{titles}",
    )
    if any("面试" in t for t in titles):
        failures += 1
    print("    备注：认识纪念日（milestone）本身没有归属概念，任何视角都会出现——"
          "这是设计上的单例状态，不是本次发现。")

    print()
    print(f"探针失败计数：{failures}")
    print(f"（临时数据根：{TMPROOT}）")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
