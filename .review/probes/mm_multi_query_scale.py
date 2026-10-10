"""Stream A 探针 8：多查询（改写变体）的归一化是否会**挤掉**基础查询的命中。

`recall()` 把多个查询的分数取逐条最大值 `best_raw[i]`，再用**全局最大分** `top`
归一化。于是：只要改写变体产生一个更高的原始分，所有"只被原查询命中"的记忆
归一化后都会变小，可能跌破 `stage.min_score` 而**从结果里消失**。

brain._prepare 只在 `len(merged) > len(recalled)` 时才采用改写结果，
所以"结果反而变少"不会直接生效——但如果两者条数相同、内容不同，
或首次检索为空、改写后非空，就会用改写后的集合。这里只做机制层面的可证伪。

跑法：
    python .review\\probes\\mm_multi_query_scale.py
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mm_common import (  # noqa: E402
    TMPROOT,
    check,
    fresh_dir,
    make_brain,
    mem,
    settings_for,
    stage,
    title,
)
from giftia.memory import recall  # noqa: E402

NOW = datetime.now()


def main() -> int:
    tmp = fresh_dir("multiquery")
    settings = settings_for(tmp)
    cfg = settings.memory
    failures = 0

    title("1. 多查询归一化：加入一个命中更强的变体会不会挤掉原命中的记忆")
    corpus = [
        mem(1, "小林 驾照 考试 科目三 挂了 很 难过", importance=0.9, layer="important"),
        mem(2, "小林 喜欢 喝 美式 咖啡", importance=0.6),
        mem(3, "小林 的 猫 叫 团团", importance=0.6),
    ]
    base = "小林 驾照"
    variant = "科目三 挂了 难过 美式 咖啡 团团 驾照 考试 小林"
    st = type(stage(settings, "clear"))(**{**stage(settings, "clear").__dict__,
                                          "top_k": 6, "min_score": 0.15})
    only_base = recall(base, corpus, st, cfg, NOW)
    with_variant = recall(base, corpus, st, cfg, NOW, extra_queries=[variant])
    print(f"    只用原查询     → {[(r.memory['id'], r.score) for r in only_base]}")
    print(f"    加上改写变体   → {[(r.memory['id'], r.score) for r in with_variant]}")
    lost = {r.memory["id"] for r in only_base} - {r.memory["id"] for r in with_variant}
    check(
        "P2-8 加入额外查询不得让原本命中的记忆消失",
        not lost,
        f"消失的 id={sorted(lost)}" if lost else "无丢失",
    )
    if lost:
        failures += 1
        for i in sorted(lost):
            ob = next(r for r in only_base if r.memory["id"] == i)
            print(f"      #{i} 原查询下 score={ob.score}（≥ min_score={st.min_score}）"
                  f"，加入变体后被挤到 min_score 之下")

    title("2. 归一化基准是全局 max，与查询个数相关（机制核对）")
    print("    recall() 源码：best_raw[i] = max over queries；bm = best_raw[i] / top")
    print("    → top 由'任意查询里最大的那条'决定，与是否真的相关于当前提问无关")

    title("3. 检索规模：记忆条数对整个读取路径的开销")
    b = make_brain(tmp / "scale", settings=settings)
    s, inst = b.store, b.instance_no
    rows = []
    base_ts = NOW.isoformat(timespec="seconds")
    for i in range(3000):
        rows.append((inst, base_ts, base_ts, f"记忆条目 {i} 小林 驾照 考试 细节{i}",
                     "", "", 0.5, "", "normal", 1.0))
    s.conn.executemany(
        "INSERT INTO memories(instance_no, created_ts, happened_ts, summary, tags, emotion,"
        " importance, source_session, layer, strength) VALUES(?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    s.conn.commit()
    t0 = time.perf_counter()
    allm = s.active_memories(inst)
    t_load = time.perf_counter() - t0
    t0 = time.perf_counter()
    hits = recall("小林 驾照 考试", allm, stage(settings, "clear"), cfg, NOW)
    t_recall = time.perf_counter() - t0
    rss = 0.0
    try:  # Windows 上没有 resource 模块
        import resource as _res

        rss = _res.getrusage(_res.RUSAGE_SELF).ru_maxrss / 1024.0
    except ImportError:
        rss = -1.0
    print(f"    3000 条记忆：active_memories={t_load * 1000:.1f} ms，"
          f"recall={t_recall * 1000:.1f} ms，命中 {len(hits)} 条，进程峰值 RSS≈{rss:.0f} MB")
    print("    每轮对话都会做一次（brain._prepare），且没有 LIMIT / 分页 / 缓存")
    check("P2-9 单轮检索开销随记忆条数线性增长（3000 条仍在百毫秒量级，可接受但要登记）",
          t_recall < 1.0, f"{t_recall * 1000:.1f} ms @3000 条")

    title("4. messages_by_prefix 的 LIKE 前缀查询有没有索引可用")
    s.add_message("i1-gGROUP-A-20261010", inst, "user", "群里的一句话", base_ts)
    plan = s._rows("EXPLAIN QUERY PLAN SELECT * FROM messages WHERE session_id LIKE ? "
                   "AND recalled = 0 ORDER BY id DESC LIMIT ?", ("i1-gGROUP-A-%", 40))
    for p in plan:
        print(f"    {p}")
    check("P2-10 前缀 LIKE 无法用 idx_msg_session（左模糊）→ 全表扫描",
          True, "见上面的 plan；消息表只增不减，群活跃时是长期增长的全表扫描")

    title("5. 记忆表只增不减：forgotten/merged/sealed 都只是标记")
    s.add_memory(instance_no=inst, created_ts=base_ts, happened_ts=base_ts,
                 summary="要被忘掉的一条", importance=0.5)
    mid = s._rows("SELECT MAX(id) AS m FROM memories")[0]["m"]
    s.forget_memory(mid)
    print(f"    forget_memory(#{mid}) 后 all_memories={len(s.all_memories(inst))}，"
          f"active_memories={len(s.active_memories(inst))}")
    check("遗忘是软删除（数据仍在，符合设计）", len(s.all_memories(inst)) > len(s.active_memories(inst)))

    print()
    print(f"多查询/规模探针失败计数：{failures}")
    print(f"（临时数据根：{TMPROOT}）")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
