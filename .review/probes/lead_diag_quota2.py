"""诊断 2：把 can_send 内部的中间值打出来。"""

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

from giftia import outreach as outreach_mod  # noqa: E402
from giftia.brain import Giftia  # noqa: E402
from giftia.config import load_settings  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402

TMP = ROOT / ".review" / "tmp" / "lead-quota-diag2"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

base = dataclasses.replace(load_settings(lifespan_override=81920.0, force_mock=True), root=TMP)
pro = Giftia(base, llm=MockLLM(base.model))
pro.set_handler_id("OWNER")
pro.handle("在吗", user_id="OWNER")
now = pro.clock.now()

fresh = dataclasses.replace(
    pro.settings,
    outreach=dataclasses.replace(
        pro.settings.outreach, max_per_day=2, max_per_30_days=99, min_gap_hours=0.0
    ),
)
for _ in range(2):
    outreach_mod.record_sent(pro.store, now)

sent = outreach_mod.sent_today(pro.store, now)
cap = max(1, fresh.outreach.max_per_day)
print(f"sent_today={sent}  max_per_day={fresh.outreach.max_per_day} -> cap={cap}")
print(f"判断 sent + 0 > cap ：{sent + 0 > cap}")
print(f"quota_state={outreach_mod.quota_state(fresh, pro.store, now)}")
print(f"can_send(fresh) = {outreach_mod.can_send(fresh, pro.store, now)}")
print(f"can_send(pro.settings) = {outreach_mod.can_send(pro.settings, pro.store, now)}")
print(f"pro.settings.outreach.max_per_day = {pro.settings.outreach.max_per_day}")
print("\n源码里的日上限那一行：")
import inspect  # noqa: E402

src = inspect.getsource(outreach_mod.can_send).splitlines()
for i, line in enumerate(src):
    if "max_per_day" in line or "sent_today" in line:
        print(f"  {i}: {line}")

pro.close()
shutil.rmtree(TMP, ignore_errors=True)
