"""Lead 探针 D2：终端服务阶段，"她记得你"到底还剩什么？

README 的承诺（第 2 节 + 关键设计 3）：
  「到了终端服务迫近的时候，她的状态是：**记得你是小林、记得你对她很重要，
    但忘了你哪天考驾照**」
  「core（你的名字、称呼、你们的关系、郑重的约定）的模糊系数恒为 0」

本探针直接调用 memory.recall()，用真实配置的五个阶段，检查：
  A. core 层记忆在 terminal 阶段的 blur 是否为 0（README 断言"永不含糊"）
  B. 当 query 与 core 记忆**没有词面重合**时，它还会不会被检索到（"记得你是小林"是否成立）
  C. 各阶段的 top_k / min_score / relative_cutoff 是否真的生效
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
from giftia.memory import effective_blur, recall, retention_of  # noqa: E402

settings = load_settings(force_mock=True)
cfg = settings.memory
now = datetime(2026, 10, 10, 12, 0, 0)
old = (now - timedelta(days=200)).isoformat(timespec="seconds")


def mem(mid: int, summary: str, layer: str, importance: float = 0.5, created: str | None = None):
    return {
        "id": mid,
        "summary": summary,
        "tags": "",
        "emotion": "",
        "about": "",
        "layer": layer,
        "importance": importance,
        "strength": 1.0 + importance,
        "recall_count": 0,
        "last_recalled_at": None,
        "created_ts": created or old,
        "happened_ts": created or old,
        "happened": "",
    }


MEMORIES = [
    mem(1, "搭档叫小林，他要求我这么称呼他。", "core", 0.95),
    mem(2, "搭档对我来说是很重要的人。", "core", 0.9),
    mem(3, "搭档下周三要考驾照，他有点紧张。", "important", 0.7),
    mem(4, "搭档今天加班到很晚，回来路上下了点雨。", "normal", 0.5),
]

QUERIES = [
    "你还记得我要考什么吗",
    "我下周三要干嘛来着",
    "今天天气怎么样",
    "在吗",
]

for st in settings.stages:
    print(f"\n=== 阶段 {st.name}（blur={st.blur} top_k={st.top_k} min_score={st.min_score}）===")
    for m in MEMORIES:
        print(
            f"  blur({m['layer']:<9} #{m['id']}) = {effective_blur(m, st, cfg, now):.3f}"
            f"   retention={retention_of(m, cfg, now):.3f}"
        )
    for q in QUERIES:
        hits = recall(q, MEMORIES, st, cfg, now)
        ids = [(r.memory["id"], r.layer, round(r.score, 3)) for r in hits]
        print(f"  query={q!r:<24} → {ids}")

print("\n=== 关键断言 ===")
terminal = settings.stage_for(0.0)
core_blur = effective_blur(MEMORIES[0], terminal, cfg, now)
print(f"[A] terminal 阶段 core 记忆 blur = {core_blur}  （README 说恒为 0）→ {'成立' if core_blur == 0 else '不成立'}")

hits = recall("今天天气怎么样", MEMORIES, terminal, cfg, now)
ids = [r.memory["id"] for r in hits]
print(f"[B] terminal 阶段问'今天天气怎么样'，召回 {ids}")
if 1 not in ids:
    print("    → 内核记忆（名字）**没有被召回**：本轮提示词里不会有「她记得你是小林」")
else:
    print("    → 内核记忆被召回")
print("    （注意：recall() 不区分层级，core 层没有保底注入通道）")
