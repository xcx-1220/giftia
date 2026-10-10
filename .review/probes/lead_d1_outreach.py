"""Lead 探针 D1：self test 第 24 节 3 项失败到底是不是"跟日期走的 flaky"。

复现 tools/selftest.py:788-802 的构造，打印 mood / plan.reason，
并把时钟停在不同日期上再跑一遍，看结果是否随日期变化。
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
from datetime import datetime, timedelta
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

TMP = Path(r"D:\ai chatbot\.review\tmp\lead-outreach")
TMP.mkdir(parents=True, exist_ok=True)


def make_settings(root: Path, hours: float):
    base = load_settings(lifespan_override=hours, force_mock=True)
    return dataclasses.replace(base, root=root)


def probe(day_offset: int) -> None:
    root = TMP / f"d{day_offset}"
    root.mkdir(parents=True, exist_ok=True)
    base = make_settings(root, 100.0)
    out = Giftia(base, llm=MockLLM(base.model))
    out.handle("在吗")
    plan_default = out.outreach_plan()
    mood = out.current_mood()
    print(
        f"[day+{day_offset}] 日期={out.clock.now().date()} 剩余={out.status().remaining_hours:.1f}h "
        f"mood.key={mood.key!r} label={mood.label!r} "
        f"默认关闭→plan={plan_default!r}"
    )

    on = dataclasses.replace(
        base, outreach=dataclasses.replace(base.outreach, enabled=True, idle_days=3)
    )
    out2 = Giftia(on, llm=MockLLM(base.model))
    out2.handle("在吗")
    p_just = out2.outreach_plan(force=True)
    print(f"          刚说过话 force=True → {(p_just.reason if p_just else None)!r}")
    out2.clock.advance(24 * 4)
    p_idle = out2.outreach_plan(force=True)
    print(f"          闲 4 天  force=True → {(p_idle.reason if p_idle else None)!r}")


for offset in (0, 1, 2, 3):
    probe(offset)

print("\n── 结论用数据说话：若 reason 随日期变化 → 测试是 flaky；若恒为 mood_share → 行为变更 ──")
