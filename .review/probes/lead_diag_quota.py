"""诊断：为什么"发满每天上限"这条断言仍然 FAIL（今天已发 2 条、max_per_day=2）。"""

from __future__ import annotations

import dataclasses
import shutil
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
sys.path.insert(0, str(ROOT))
for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from giftia import outreach as outreach_mod  # noqa: E402
from giftia.brain import Giftia  # noqa: E402
from giftia.config import load_settings  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402

TMP = ROOT / ".review" / "tmp" / "lead-quota-diag"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

base = dataclasses.replace(load_settings(lifespan_override=81920.0, force_mock=True), root=TMP)
pro = Giftia(base, llm=MockLLM(base.model))
pro.set_handler_id("OWNER")
pro.handle("在吗", user_id="OWNER")
clock_now = pro.clock.now()

fresh = dataclasses.replace(
    pro.settings,
    outreach=dataclasses.replace(
        pro.settings.outreach, max_per_day=2, max_per_30_days=99, min_gap_hours=0.0
    ),
)
print(f"max_per_day={fresh.outreach.max_per_day} max_per_30_days={fresh.outreach.max_per_30_days}")
print(f"空账本 can_send = {outreach_mod.can_send(fresh, pro.store, clock_now)}")
for i in range(2):
    outreach_mod.record_sent(pro.store, clock_now)
    print(f"  记第 {i + 1} 条后 sent_today={outreach_mod.sent_today(pro.store, clock_now)}")
print(f"quota_state = {outreach_mod.quota_state(fresh, pro.store, clock_now)}")
print(f"发满后 can_send = {outreach_mod.can_send(fresh, pro.store, clock_now)}  ← 期望 False")

print("\n账本原始内容：")
print(" ", pro.store.state_get("outreach:log", ""))
print(f"\n第二天的 now = {clock_now + timedelta(days=1)}")
print(
    "第二天 can_send =",
    outreach_mod.can_send(fresh, pro.store, clock_now + timedelta(days=1)),
)

pro.close()
shutil.rmtree(TMP, ignore_errors=True)
