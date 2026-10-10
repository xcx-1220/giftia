"""诊断：为什么"你明显低落 → 她在担心你"变成 low/daily。"""

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

from giftia.brain import Giftia  # noqa: E402
from giftia.config import load_settings  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402
from giftia.mood import tone_of  # noqa: E402

TMP = ROOT / ".review" / "tmp" / "lead-mood-diag"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

base = dataclasses.replace(load_settings(lifespan_override=100.0, force_mock=True), root=TMP)
md = Giftia(base, llm=MockLLM(base.model))

text = "我今天特别难过，什么都做不好，压力很大"
print(f"tone_of({text!r}) = {tone_of(text):+.3f}  (tone_threshold={base.mood.tone_threshold})")
print(f"uneasy_hours={base.mood.uneasy_hours}  剩余寿命={md.status().remaining_hours:.2f}h")

md.handle("你好")
m0 = md.current_mood()
print(f"\n第 1 轮后：key={m0.key!r} source={m0.source!r} until={m0.until!r}")
print(f"  is_expired = {md.mood.is_expired(md.clock.now(), md.instance_no)}")

md.handle(text)
m1 = md.current_mood()
print(f"第 2 轮后：key={m1.key!r} source={m1.source!r} until={m1.until!r}")
print(f"  剩余寿命={md.status().remaining_hours:.2f}h  now={md.clock.now()}")
print(f"  state 里的原始值：key={md.store.state_get(f'mood:i{md.instance_no}:key')!r} "
      f"source={md.store.state_get(f'mood:i{md.instance_no}:source')!r} "
      f"until={md.store.state_get(f'mood:i{md.instance_no}:until')!r}")

md.close()
shutil.rmtree(TMP, ignore_errors=True)
