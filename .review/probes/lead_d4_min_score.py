"""Lead 探针 D4：min_score 到底是"绝对相关度门槛"还是"相对门槛"？

background：memory.recall() 里
    bm = best_raw[i] / top          # 归一化：首名恒为 1.0
    score = bm * (w_bm25 + w_importance*importance + w_recency*retention + w_layer*layer_w)
    if score < stage.min_score: continue

如果 bm 是"与本次查询的 BM25 相关度"，那么 min_score 就是绝对门槛；
如果 bm 是 **除以本次候选集里的最高分**，那么 min_score 就变成一个相对门槛：
哪怕查询与这条记忆只有一点字面重合，只要它是全场最高分，score ≈ 常数（1.0~1.5），
永远 >= 任何 min_score（最高 0.55）。下面用两个实验区分这两种解释。
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
from giftia.memory import EVIDENCE_GATE, bm25_scores, overlap_evidence, recall, tokenize_weighted  # noqa: E402

settings = load_settings(force_mock=True)
cfg = settings.memory
now = datetime(2026, 10, 10, 12, 0, 0)
terminal = settings.stage_for(0.0)   # min_score 最高（0.55）、top_k 最小（2）
clear = settings.stage_for(1.0)


def mk(mid: int, summary: str, layer: str = "normal", importance: float = 0.5) -> dict:
    created = (now - timedelta(days=30)).isoformat(timespec="seconds")
    return {
        "id": mid, "summary": summary, "tags": "", "emotion": "", "about": "",
        "layer": layer, "importance": importance, "strength": 1.0,
        "recall_count": 0, "last_recalled_at": None,
        "created_ts": created, "happened_ts": created,
    }


print("── 实验 1：弱相关候选（能过证据门槛，但相关度低）在 min_score=0.55 下会不会被拦 ──")
# 关键：先过"证据门槛"（至少一个二元组重合，否则根本进不了打分），再看 min_score。
# "我下周三要考驾照" 的二元组含"驾照"，所以记忆里必须有"驾照"才进得了打分环节。
q = "我下周三要考驾照"
weak = [mk(1, "搭档的驾照放在抽屉里。", importance=0.15)]
print(f"  query={q!r}  记忆={weak[0]['summary']!r}（仅 1 个二元组重合、重要性最低）")
hits = recall(q, weak, terminal, cfg, now)
print(f"  terminal(min_score=0.55) 召回 {[(h.memory['id'], h.score) for h in hits]}")
print("  → 只有 1 个候选时 bm = best/top = 1.0，score ≈ 1.15~1.45，"
      "永远 >= 0.55：min_score 在候选集很小时形同虚设")

print("\n── 实验 2：同一个弱相关记忆，加一个'更相关'的记忆做对比 ──")
pair = weak + [mk(2, "搭档下周三要考驾照，他很紧张。", importance=0.7)]
hits2 = recall(q, pair, terminal, cfg, now)
print(f"  2 个候选 → 召回 {[(h.memory['id'], h.score) for h in hits2]}")
print(f"  → 现在 #1 的 bm 被 #2 的 BM25 归一化掉，于是掉出结果（说明 min_score 是**相对**门槛）")

print("\n── 实验 3：同一句话，候选集里有没有'高分对手'决定它会不会被想起 ──")
probe_q = "你喜欢什么"
b1 = [mk(10, "搭档喜欢喝咖啡。")]
b2 = [mk(10, "搭档喜欢喝咖啡。"), mk(11, "搭档喜欢喝咖啡，而且喜欢在下雨天喝，还喜欢加很多糖。")]
for label, bucket in (("单独一条", b1), ("旁边有一条更详尽的", b2)):
    h = recall(probe_q, bucket, clear, cfg, now)
    print(f"  {label:<16} → {[(x.memory['id'], x.score) for x in h]}")

print("\n── 实验 4：BM25 原始分是否被丢弃（对比 bm25_scores 的绝对值）──")
docs = [tokenize_weighted(f"{m['summary']} {m['tags']} {m['emotion']} {m['about']}") for m in pair]
raw = bm25_scores(tokenize_weighted(q), docs)
print(f"  原始 BM25 = {[round(x, 4) for x in raw]}")
print(f"  归一化后  = {[round(x / max(raw), 4) for x in raw]}")
print(f"  → 原始分只有几百毫厘的差别，归一化后被放大成 1.0 vs {raw[0]/max(raw):.3f}")

print("\n── 实验 5：证据门槛（EVIDENCE_GATE）到底是几 ──")
qc = tokenize_weighted(q)
for m in pair:
    d = tokenize_weighted(m["summary"])
    print(f"  #{m['id']} overlap_evidence={overlap_evidence(qc, d):.1f} "
          f"（门槛 {min(EVIDENCE_GATE, sum(1.0 if len(t) == 1 else 2.0 for t in qc)):.1f}）")

print("\n── 实验 6：内核记忆（core）在 terminal 阶段能不能靠 min_score 保底 ──")
core = [mk(1, "搭档叫小林。", layer="core", importance=0.95)]
h = recall("你还记得我要考什么吗", core, terminal, cfg, now)
print(f"  问'考什么'，库里只有'搭档叫小林' → 召回 {[(x.memory['id'], x.score) for x in h]}")
core_plus = core + [mk(2, "搭档下周三要考驾照。", layer="important", importance=0.7)]
h2 = recall("你还记得我要考什么吗", core_plus, terminal, cfg, now)
print(f"  再加上'考驾照'那条      → 召回 {[(x.memory['id'], x.score) for x in h2]}")
print("  → 内核记忆没有保底通道：query 不沾边就不召回（'她记得你是小林'依赖你正好提到名字/称呼）")
