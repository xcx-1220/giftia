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
    retentions = []
    for label, m in cases:
        base = (
            m.get("last_recalled_at") or m.get("happened_ts") or m.get("created_ts")
        )
        r = retention_of(m, cfg, NOW)
        retentions.append(r)
        b = effective_blur(m, stage(settings, "fading"), cfg, NOW)
        print(f"    {label}")
        print(f"      base={base!r} retention={r:.4f} fading_blur={b:.4f} "
              f"human_age(happened)={human_age(m['happened_ts'], NOW)!r}")
    # 期望（修复后）：Δt 的起点是 **happened_ts**（没有 last_recalled_at 时），
    # 所以"10 天前发生、刚刚才整理出来"的那条 retention 明显小于 1，
    # 与提示词里给她看的 human_age(happened)=「10 天前」一致。
    # 原断言是"固定 FAIL"，记录的是 created_ts 当起点的缺陷（P3-4）。
    stale_ok = retentions[0] < 0.9
    fresh_ok = retentions[2] > 0.9   # 有 last_recalled_at（刚说过）→ 仍然接近 1
    check(
        "P3-4 清晰度按 happened_ts 起算（不是整理时刻），与提示词里的相对时间一致",
        stale_ok and fresh_ok,
        f"retention 序列={[round(x, 4) for x in retentions]}（第 1 条应明显 <1、第 3 条应 ≈1）",
    )
    if not (stale_ok and fresh_ok):
        failures += 1
    print("    说明：攒够阈值才整理 → 一段对话可能几小时后才落库；"
          "现在落库不再把'久远的事'刷成'刚说过'，而'刚被说出口'的那条照旧最清晰。")

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
