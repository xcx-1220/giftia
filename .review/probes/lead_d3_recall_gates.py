"""Lead 探针 D3：五阶段的 top_k / min_score / relative_cutoff 是否真的都是有效闸门。

README 第 4 节表格宣称五个阶段"动态收紧 top_k、min_score、模糊比例"。
本探针人工造 30 条与查询都有词面重合的记忆（覆盖不同层级/新旧），看：
  A. 每个阶段实际返回几条（top_k 是否真的封顶）
  B. 相对阈值裁剪 relative_cutoff 是否真的在起作用
  C. 换一个 query 后，排名第一是否总是被"归一化"到 1.0（分数是相对量而非绝对量）
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
sys.path.insert(0, str(ROOT))
for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from giftia.config import load_settings  # noqa: E402
from giftia.memory import recall, retention_of  # noqa: E402

settings = load_settings(force_mock=True)
cfg = settings.memory
now = datetime(2026, 10, 10, 12, 0, 0)

# 30 条：都含"考"和"驾照"，新旧/层级/重要性不同 —— 制造一个拥挤的候选集
LAYERS = ("core", "important", "normal")
memories = []
for i in range(1, 31):
    age_days = i * 3
    created = (now - timedelta(days=age_days)).isoformat(timespec="seconds")
    memories.append({
        "id": i,
        "summary": f"搭档说过第 {i} 件关于考试和驾照的事，细节是第 {i} 号。",
        "tags": "考试,驾照",
        "emotion": "紧张" if i % 3 else "",
        "about": "小林" if i % 2 else "",
        "layer": LAYERS[i % 3],
        "importance": 0.3 + (i % 5) * 0.15,
        "strength": 1.0 + (i % 5) * 0.15,
        "recall_count": i % 4,
        "last_recalled_at": None,
        "created_ts": created,
        "happened_ts": created,
    })

print(f"候选集 {len(memories)} 条，查询：'我下周三要考驾照，你还记得吗'\n")
print(f"{'阶段':<10}{'top_k':>6}{'min_score':>11}{'cutoff':>8}{'实际返回':>9}  分数")
for st in settings.stages:
    hits = recall("我下周三要考驾照，你还记得吗", memories, st, cfg, now)
    scores = ", ".join(f"{h.score}" for h in hits)
    print(f"{st.name:<10}{st.top_k:>6}{st.min_score:>11}{cfg.relative_cutoff:>8}{len(hits):>9}  [{scores}]")

print("\n=== A. top_k 是否是有效封顶？===")
worst = settings.stages[0]
hits = recall("我下周三要考驾照，你还记得吗", memories, worst, cfg, now)
print(f"clear 阶段 top_k={worst.top_k}，实际返回 {len(hits)} 条 → "
      f"{'未触顶（候选被 evidence gate / cutoff 先筛掉了）' if len(hits) < worst.top_k else '触顶'}")

print("\n=== B. 相对阈值裁剪：把 cutoff 拉到 0.99 看剩几条 ===")
import dataclasses  # noqa: E402
strict = dataclasses.replace(cfg, relative_cutoff=0.99)
for st in settings.stages:
    hits = recall("我下周三要考驾照，你还记得吗", memories, st, strict, now)
    print(f"  {st.name:<10} cutoff=0.99 → {len(hits)} 条")

print("\n=== C. 分数是相对量：同一条记忆，换个候选集分数就变 ===")
single = [memories[0]]
h1 = recall("我下周三要考驾照，你还记得吗", memories, settings.stages[0], cfg, now)
h2 = recall("我下周三要考驾照，你还记得吗", single, settings.stages[0], cfg, now)
print(f"  30 条候选时 #1 的分数 = {[h.score for h in h1 if h.memory['id'] == 1]}")
print(f"  只有 #1 时 #1 的分数   = {[h.score for h in h2]}")
print("  → 分数是 bm25/首名 的归一化值，单独出现时恒为 1.0；min_score 因此是相对门槛")
print(f"\n  参考 retention（200 天前、被想起 2 次）={retention_of(memories[0], cfg, now):.4f}")
