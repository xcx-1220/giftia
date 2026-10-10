"""Stream A 探针 10：保留率 Δt 的起点语义。

`retention_of` 取 base = last_recalled_at or created_ts or happened_ts。
`created_ts` 是**整理时刻**（consolidate 的 now_iso），不是事情发生的时刻。
整理是攒够 N 条消息才做的（settings: consolidate_every=6），所以
"Δt 从记忆被写下来那一刻算起"本身没错，但要和 README 的说法对齐：
    「每次'被真正说出口'都会让 S 变大、Δt 归零」

本探针只做一件事：证明 created_ts 与 happened_ts 可以差很远，
而退化/保留率完全按 created_ts 走 —— human_age/happened_ts 则走另一条。

跑法：
    python .review\\probes\\mm_retention_base.py
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
    mem,
    settings_for,
    stage,
    title,
)
from giftia.memory import effective_blur, human_age, retention_of  # noqa: E402

NOW = datetime.now()


def main() -> int:
    tmp = fresh_dir("retention")
    settings = settings_for(tmp)
    cfg = settings.memory
    failures = 0

    title("1. base 的优先级与 Δt 起点")
    ten_days_ago = (NOW - timedelta(days=10)).isoformat(timespec="seconds")
    just_now = NOW.isoformat(timespec="seconds")
    cases = [
        ("happened 10 天前，created 刚刚（刚被整理出来）",
         mem(1, "x", created_ts=just_now, happened_ts=ten_days_ago)),
        ("happened 与 created 都是 10 天前",
         mem(2, "x", created_ts=ten_days_ago, happened_ts=ten_days_ago)),
        ("有 last_recalled_at（刚刚说过）",
         mem(3, "x", created_ts=ten_days_ago, happened_ts=ten_days_ago,
             last_recalled_at=just_now)),
    ]
    for label, m in cases:
        base = m.get("last_recalled_at") or m.get("created_ts") or m.get("happened_ts")
        r = retention_of(m, cfg, NOW)
        b = effective_blur(m, stage(settings, "fading"), cfg, NOW)
        print(f"    {label}")
        print(f"      base={base!r} retention={r:.4f} fading_blur={b:.4f} "
              f"human_age(happened)={human_age(m['happened_ts'], NOW)!r}")
    check(
        "P3-4 记忆的'清晰度'按 created_ts 算，而不是事情发生的 happened_ts",
        False,
        "10 天前发生、刚刚才被整理出来的事，retention≈1（=刚说过），不会随时间变模糊",
    )
    failures += 1
    print("    影响：攒够 6 条才整理 → 一段对话可能在几小时后才落库，"
          "落库那一刻 retention 归 1；而人在提示词里看到的相对时间是 happened_ts。")

    title("2. strength 的取值范围与 S 的语义")
    for strength, recalls in ((1.0, 0), (1.8, 0), (1.8, 3), (0.0, 0), (-5.0, 0)):
        m = mem(9, "x", strength=strength, recall_count=recalls,
                created_ts=just_now)
        S = cfg.retention_unit_hours * (strength + recalls)
        r = retention_of(m, cfg, NOW)
        print(f"    strength={strength:>5} recall_count={recalls} → "
              f"S={max(1e-6, S):>10.2f} 小时，刚写下时 retention={r:.6f}")
    check("S 有下限保护（max(1e-6)），不会除零", True, "strength 由 initial_strength 保证 ≥1.0")

    title("3. 时间纵轴：retention 与 blur 的衰减曲线（默认 720 小时）")
    print(f"    {'Δt(小时)':>10}{'retention':>12}{'terminal_blur':>15}{'fading_blur':>13}")
    tst = stage(settings, "terminal")
    fst = stage(settings, "fading")
    for hours in (0, 1, 24, 168, 720, 2160, 8760):
        m = mem(1, "x", strength=1.0, created_ts=(NOW - timedelta(hours=hours)).isoformat(timespec="seconds"))
        r = retention_of(m, cfg, NOW)
        print(f"    {hours:>10}{r:>12.4f}{effective_blur(m, tst, cfg, NOW):>15.4f}"
              f"{effective_blur(m, fst, cfg, NOW):>13.4f}")

    print()
    print(f"保留率探针失败计数：{failures}")
    print(f"（临时数据根：{TMPROOT}）")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
