"""Lead 独立验证：群发提示词里的【今天不只是普通的一天】是否泄漏别人的私聊大日子。

代码事实（只读）：
  giftia/brain.py:939-946  compose_outreach 里第二次 find_occasions **没传 view**
  giftia/occasions.py:110  find_occasions 里 store.active_memories(instance_no, view=view)
  giftia/store.py:601-611 _view_clause(None) → ("", []) = **不过滤**
本探针直接对比"传 view"与"不传 view"两个结果，并用真实 compose_outreach 的 system 提示词验证。
"""

from __future__ import annotations

import dataclasses
import shutil
import sys
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
sys.path.insert(0, str(ROOT))
for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from datetime import timedelta  # noqa: E402

from giftia.brain import Giftia  # noqa: E402
from giftia.config import load_settings  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402
from giftia.occasions import days_together_text, find_occasions  # noqa: E402

TMP = Path(r"D:\ai chatbot\.review\tmp\lead-verify-p0c")
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

base = load_settings(lifespan_override=81920.0, force_mock=True)
settings = dataclasses.replace(
    base, root=TMP,
    outreach=dataclasses.replace(base.outreach, enabled=True, target="group"),
)

captured: list[str] = []


class RecordingLLM(MockLLM):
    def chat(self, messages, **kw):  # type: ignore[override]
        captured.append(str(messages[0]["content"]))
        return super().chat(messages, **kw)


brain = Giftia(settings, llm=RecordingLLM(settings.model))
tomorrow = (brain.clock.now().date() + timedelta(days=1)).isoformat()

# 四条大日子：搭档的私事 / 别人的私聊 / 本群公开 / 另一个群
brain.store.add_memory(
    instance_no=1, created_ts=brain.clock.iso(), happened_ts=brain.clock.iso(),
    summary="搭档明天要去面试。", importance=0.8, layer="important",
    scope="private", subject_id="", event_date=tomorrow, about="搭档",
)
brain.store.add_memory(
    instance_no=1, created_ts=brain.clock.iso(), happened_ts=brain.clock.iso(),
    summary="SECRET-BRAVO 小雨明天要去医院做手术。", importance=0.9, layer="important",
    scope="person", subject_id="USER-XIAOYU", event_date=tomorrow, about="小雨",
)
brain.store.add_memory(
    instance_no=1, created_ts=brain.clock.iso(), happened_ts=brain.clock.iso(),
    summary="阿伟明天要考驾照。", importance=0.7, layer="important",
    scope="group", subject_id="GROUP-1", event_date=tomorrow, about="阿伟",
)
brain.store.add_memory(
    instance_no=1, created_ts=brain.clock.iso(), happened_ts=brain.clock.iso(),
    summary="小刚后天要搬家。", importance=0.6, layer="normal",
    scope="group", subject_id="GROUP-OTHER", event_date=tomorrow, about="小刚",
)
brain.set_last_group("GROUP-1")

now = brain.clock.now()
bond = brain.current_bond(now)
print("库里的四条大日子：private(搭档) / person(小雨) / group=GROUP-1(阿伟) / group=GROUP-OTHER(小刚)\n")

print("A. find_occasions 传 view vs 不传 view")
group_only = find_occasions(
    settings, brain.store, now=now, instance_no=1, days_known=bond.days_known,
    mark=False, view=[("group", "GROUP-1")],
)
no_view = find_occasions(
    settings, brain.store, now=now, instance_no=1, days_known=bond.days_known,
    mark=False, view=None,
)
print(f"   view=[('group','GROUP-1')] → {[o.title for o in group_only if o.kind == 'big_day']}")
print(f"   view=None（不过滤）        → {[o.title for o in no_view if o.kind == 'big_day']}")

print("\nB. compose_outreach(target='group') 实际发给模型的 system 提示词")
captured.clear()
plan_, text = brain.compose_outreach(record=False, target="group")
blob = captured[-1] if captured else ""
leaks = [kw for kw in ("搭档明天要去面试", "小雨明天要去医院做手术", "小刚后天要搬家") if kw in blob]
print(f"   计划层 reason = {plan_.reason if plan_ else None}")
print(f"   提示词里出现的不该出现的私事：{leaks}")
for ln in blob.splitlines():
    if any(k in ln for k in ("搭档明天要去面试", "小雨明天要去医院", "小刚后天要搬家")):
        print(f"     泄漏行 → {ln.strip()[:70]}")
print(f"   [{'FAIL' if leaks else 'PASS'}] 群发提示词只应包含本群公开的大日子（阿伟要考驾照）")

print("\nC. 对照：正常群聊路径（handle scene='group'）同样查询")
captured.clear()
brain.handle("在吗", "USER-AWEI", scene="group", group_id="GROUP-1", speaker_name="阿伟")
blob2 = captured[-1] if captured else ""
leaks2 = [kw for kw in ("搭档明天要去面试", "小雨明天要去医院做手术") if kw in blob2]
print(f"   正常群聊提示词里的泄漏：{leaks2} → [{'FAIL' if leaks2 else 'PASS'}]（闸门本身是好的）")

print("\nD. 同类漏点：/days 的「她记得的大日子」")
days = days_together_text(brain.store, 1, bond.days_known, settings.occasions.milestones)
leaks3 = [kw for kw in ("小雨明天要去医院", "小刚后天要搬家") if kw in days]
for ln in days.splitlines():
    if "2026-" in ln or "明天" in ln:
        print(f"   {ln.strip()[:80]}")
print(f"   [{'FAIL' if leaks3 else 'PASS'}] /days 只应列搭档自己的大日子 → 泄漏 {leaks3}")

brain.close()
shutil.rmtree(TMP, ignore_errors=True)
