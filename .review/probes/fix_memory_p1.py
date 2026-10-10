"""team-store 修复探针 2：记忆层（P1-7 事务 / P1-10 游标 / P2 检索质量）。

跑法（在 D:\\ai chatbot 下）：
    python .review\\probes\\fix_memory_p1.py
"""

from __future__ import annotations

import dataclasses
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from giftia.config import MemoryConfig, load_settings  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402
from giftia.memory import (  # noqa: E402
    _doc_counts,
    _prefilter,
    is_cited,
    load_pending,
    mark_cited,
    recall,
    tokenize_weighted,
)
from giftia.store import ALL_SCOPES, Store  # noqa: E402

TMP = ROOT / ".review" / "tmp" / "fix-memory"
PASS = FAIL = 0
NOW = datetime.now()


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  → {detail}" if detail else ""))


def title(text: str) -> None:
    print()
    print("=" * 78)
    print(text)
    print("=" * 78)


def fresh(tag: str) -> Path:
    d = TMP / tag
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def settings_for(tag: str):
    d = fresh(tag)
    return dataclasses.replace(load_settings(force_mock=True), root=d)


def main() -> int:
    settings = settings_for("cfg")
    cfg = settings.memory

    # ── 1. P1-7 consolidate 一个事务 ─────────────────────────────
    title("1. P1-7 consolidate：写记忆 + 标已整理 + 推游标 = 一个事务")
    st_settings = settings_for("txn")
    store = Store(st_settings.db_path)
    inst = 1
    sid = "i1-m-20261010"
    store.add_message(sid, inst, "user", "我下周三要去考驾照，有点紧张", "2026-10-10T09:00:00")
    store.add_message(sid, inst, "user", "科目三练了两周了，还是怕", "2026-10-10T09:01:00")

    drafted: list[str] = []
    real_meta_set = store.meta_set
    fail_on = {"key": "last_consolidated_msg_id"}

    def boom_meta_set(key, value, *, commit=True):
        if key == fail_on["key"]:
            raise sqlite3.OperationalError("disk I/O error（模拟崩溃）")
        return real_meta_set(key, value, commit=commit)

    store.meta_set = boom_meta_set  # type: ignore[method-assign]
    raised = ""
    try:
        from giftia.memory import consolidate

        consolidate(store, MockLLM(st_settings.model), st_settings, session_id=sid,
                    now_iso="2026-10-10T09:05:00", force=True,
                    on_draft=lambda d: drafted.append(d.summary))
    except sqlite3.OperationalError as exc:
        raised = str(exc)
    finally:
        store.meta_set = real_meta_set  # type: ignore[method-assign]

    check("崩溃确实发生在事务内（模拟第 2 个 meta_set 失败）", "disk I/O error" in raised, raised)
    check("记忆一条都没落库（整批回滚）", store.active_memories(inst, ALL_SCOPES) == [],
          str(store.active_memories(inst, ALL_SCOPES)))
    rows = store._rows("SELECT id, consolidated FROM messages ORDER BY id")
    check("消息仍是未整理（下一轮可重试）", all(int(r["consolidated"]) == 0 for r in rows), str(rows))
    # 注意：会话游标的**初次建立**发生在 load_pending（读路径）里，值本身就是
    # min(最早未整理 id - 1, 全局游标) —— 它是"地板"而不是"推进"，回滚后重试会
    # 得到同一个值，绝不会让消息被跳过。真正的不变量是"没被推到第一条消息之后"。
    cur = store.meta_get_int(f"last_consolidated:{sid}", -1)
    check("会话游标没有被推到任何未整理消息之后（保持地板值）", cur < 1, str(cur))
    check("全局游标没有推进", store.meta_get_int("last_consolidated_msg_id", 0) == 0)
    check("on_draft 回调没有在回滚的事务里触发（关系加分不会凭空发生）", drafted == [], str(drafted))

    # 重试：不产生重复记忆
    added1 = consolidate(store, MockLLM(st_settings.model), st_settings, session_id=sid,
                         now_iso="2026-10-10T09:06:00", force=True,
                         on_draft=lambda d: drafted.append(d.summary))
    first = [str(m["summary"]) for m in store.active_memories(inst, ALL_SCOPES)]
    check(f"重试成功写出 {added1} 条记忆", added1 >= 1 and len(first) == added1, str(first))
    check("回调在提交之后才触发（条数与记忆一致）", len(drafted) == added1, str(drafted))
    check("消息已标记已整理",
          all(int(r["consolidated"]) == 1 for r in store._rows("SELECT consolidated FROM messages")))
    check("两个游标都推到了最后一条",
          store.meta_get_int(f"last_consolidated:{sid}", -1) == 2
          and store.meta_get_int("last_consolidated_msg_id", 0) == 2)
    # 再喂同样的两句话：相似度去重仍在，不该重复写
    store.add_message(sid, inst, "user", "我下周三要去考驾照，有点紧张", "2026-10-10T10:00:00")
    store.add_message(sid, inst, "user", "科目三练了两周了，还是怕", "2026-10-10T10:01:00")
    added2 = consolidate(store, MockLLM(st_settings.model), st_settings, session_id=sid,
                         now_iso="2026-10-10T10:06:00", force=True)
    check("重试/重复对话不会产生重复记忆（相似度去重仍在）",
          len(store.active_memories(inst, ALL_SCOPES)) == len(first), f"added2={added2}")
    store.close()

    # ── 2. P1-10 新会话游标 ──────────────────────────────────────
    title("2. P1-10 新会话游标取 min(本会话最早未整理 id - 1, 全局游标)")
    st_settings = settings_for("cursor")
    store = Store(st_settings.db_path)
    for k in range(3):
        store.add_message(f"i1-m-2026100{k+1}", 1, "user", f"会话{k}的一句话内容",
                          f"2026-10-0{k+1}T09:00:00")
    store.meta_set("last_consolidated_msg_id", 2)          # 全局游标已走远
    store.meta_set("last_consolidated:i1-m-20261003", 2)   # 会话 3 整理过
    p1 = load_pending(store, 1, "i1-m-20261001")
    p3 = load_pending(store, 1, "i1-m-20261003")
    print(f"    会话1 pending={[m['id'] for m in p1]}；会话3 pending={[m['id'] for m in p3]}")
    check("新会话自己的 id=1 不会被全局游标跳过", 1 in [m["id"] for m in p1], str([m["id"] for m in p1]))
    check("新会话游标 = min(1-1, 2) = 0",
          store.meta_get_int("last_consolidated:i1-m-20261001", -1) == 0)
    check("已建过游标的会话不受影响（id=2 之后才取）",
          [m["id"] for m in p3] == [3], str([m["id"] for m in p3]))
    check("first_open_message_id 与实际最早未整理消息一致",
          store.first_open_message_id(1, "i1-m-20261001") == 1
          and store.first_open_message_id(1, "i1-m-20261009") is None)
    check("全部整理完之后 pending 为空（不会无限重来）",
          load_pending(store, 1, "i1-m-20261009") == [])
    store.close()

    # ── 3. P2 检索质量：is_cited ─────────────────────────────────
    title("3. P2 is_cited：必须命中至少一个二元组（单字凑数不算）")
    summary = "搭档说过：他下周三要去考驾照，很紧张。"
    cases = [
        ("真的说了这条记忆", "你不是下周三要考驾照吗，紧张吗？", True),
        ("六个字连续重合", "哦对，你要去考驾照对吧。", True),
        ("只共享单字（他/去/要）", "他要去。", False),
        ("只共享单字母词元 a b c（旧逻辑：3 个权重≥2 的'词'就算）",
         "a b c", False),
        ("完全无关的寒暄", "今天天气不错，你在干嘛呢？", False),
        ("纯语气词", "嗯嗯，好的呀。", False),
    ]
    old_rule = tokenize_weighted("a b c 我要去考驾照")
    for label, reply, want in cases:
        got = is_cited(reply, summary, cfg)
        check(f"{label} → is_cited={got}（期望 {want}）", got is want, f"reply={reply!r}")
    # 证明"单字母词元"这条在旧逻辑下会判真：
    reply_counts = tokenize_weighted("a b c")
    old_shared = sum(1 for t, w in old_rule.items() if w >= 2.0 and t in reply_counts)
    bigrams = sum(1 for t in old_rule if len(t) >= 2 and t in reply_counts)
    check("旧逻辑对 'a b c' 会数出 ≥3 个权重≥2 的命中（正是要堵的洞）",
          old_shared >= 3, f"old_shared={old_shared}")
    check("新逻辑的二元组命中数为 0（所以判假）", bigrams == 0, f"bigrams={bigrams}")

    # ── 4. P2 检索质量：mark_cited 最多一条 ──────────────────────
    title("4. P2 mark_cited：一条回复最多强化一条（得分最高的那条）")
    st_settings = settings_for("cited")
    store = Store(st_settings.db_path)
    ids = []
    for s in ("搭档说他下周三要去考驾照，很紧张。",
              "搭档说他下周要去考驾照的科目三。",
              "搭档说他下周要去考场踩点。"):
        ids.append(store.add_memory(instance_no=1, created_ts="2026-10-10T09:00:00",
                                    happened_ts="2026-10-10T09:00:00", summary=s, importance=0.8,
                                    scope="private", subject_id=""))
    from giftia.memory import Recalled

    recalled = [Recalled(memory={"id": i, "summary": s}, score=1.0 - k * 0.1, text=s, blurred=False)
                for k, (i, s) in enumerate(zip(ids, ("搭档说他下周三要去考驾照，很紧张。",
                                                     "搭档说他下周要去考驾照的科目三。",
                                                     "搭档说他下周要去考场踩点。")))]
    cited = mark_cited(store, recalled, "你不是下周三要去考驾照吗？科目三别紧张。", cfg,
                       "2026-10-10T09:10:00")
    counts = {r["id"]: r["recall_count"] for r in store._rows("SELECT id, recall_count FROM memories")}
    print(f"    被强化：{cited} → recall_count={counts}")
    check("一句话最多强化一条", len(cited) == 1, f"强化了 {len(cited)} 条")
    check("强化的是得分最高的那条（recalled[0]）", cited == [ids[0]], str(cited))
    check("另外两条没有被连带 +1", counts[ids[1]] == 0 and counts[ids[2]] == 0, str(counts))
    check("完全不提记忆时一条都不强化",
          mark_cited(store, recalled, "今天天气不错", cfg, "2026-10-10T09:11:00") == [])
    store.close()

    # ── 5. P2 检索质量：分词缓存 ─────────────────────────────────
    title("5. P2 recall 的分词结果按记忆缓存（同一条不重复分词）")
    m = {"id": 42, "summary": "搭档要考驾照", "tags": "", "emotion": "", "about": ""}
    c1, c2 = _doc_counts(m), _doc_counts(m)
    check("同一条记忆第二次取到的是同一个对象（命中缓存）", c1 is c2)
    m2 = {**m, "summary": "搭档要考驾照了"}
    check("文本变了就换一个键（不会读到旧分词）", _doc_counts(m2) is not c1)

    # ── 6. P2 检索质量：预筛不改默认行为 ─────────────────────────
    title("6. P2 recall 预筛：默认 2000 条不改行为，调小才生效")
    check("默认上限够大（2000）", MemoryConfig().recall_prefilter == 2000)
    mems = [
        {"id": i, "summary": f"记忆{i} 关于驾照考试和科目三的细节", "tags": "", "emotion": "",
         "about": "", "importance": 0.5, "layer": "normal", "strength": 1.0, "recall_count": 0,
         "created_ts": "2026-10-01T09:00:00", "happened_ts": "2026-10-01T09:00:00",
         "last_recalled_at": None}
        for i in range(1, 31)
    ]
    mems[0]["layer"] = "core"  # 最早的内核记忆
    stage = settings.stage_for(0.8)
    base_cfg = dataclasses.replace(cfg, recall_prefilter=2000)
    wide_cfg = dataclasses.replace(cfg, recall_prefilter=10000)
    a = [r.memory["id"] for r in recall("驾照 考试", mems, stage, base_cfg, NOW)]
    b = [r.memory["id"] for r in recall("驾照 考试", mems, stage, wide_cfg, NOW)]
    check("默认上限与'不预筛'结果完全一致", a == b, f"{a} vs {b}")
    check("预筛长度本身不改候选（30 < 2000）", len(_prefilter(mems, base_cfg)) == 30)
    narrow = dataclasses.replace(cfg, recall_prefilter=3)
    picked = {m["id"] for m in _prefilter(mems, narrow)}
    check("预筛调小后按层级/重要性截断，且内核记忆被保住", len(picked) == 3 and 1 in picked,
          str(sorted(picked)))

    # ── 7. P2 检索质量：min_score 是相对门槛 / min_raw_score 是绝对 ─
    title("7. P2 min_raw_score：绝对相关度门槛（默认 0.0 不启用）")
    check("默认不启用", MemoryConfig().min_raw_score == 0.0)
    weak = [{"id": 1, "summary": "驾照放在抽屉里。", "tags": "", "emotion": "", "about": "",
             "importance": 0.2, "layer": "normal", "strength": 1.0, "recall_count": 0,
             "created_ts": "2026-10-01T09:00:00", "happened_ts": "2026-10-01T09:00:00",
             "last_recalled_at": None}]
    terminal = settings.stage_for(0.0)
    hits = recall("驾照", weak, terminal, cfg, NOW)
    check(f"候选只有 1 条时 min_score={terminal.min_score} 拦不住（相对门槛：首名恒 1.0）",
          len(hits) == 1, f"{[(h.memory['id'], h.score) for h in hits]}")
    strict = dataclasses.replace(cfg, min_raw_score=50.0)
    check("min_raw_score 调到 50 → 弱相关的那条被绝对门槛拦下",
          recall("驾照", weak, terminal, strict, NOW) == [])
    # 用真实的 BM25 原始分来自校准，别靠猜阈值：
    # min_raw_score 卡在"最高原始分的一半"→ 相关的那条留下，弱的那条被拦；
    # 卡在"最高原始分之上"→ 一条都不留。
    from giftia.memory import bm25_scores, memory_text

    pool = mems[:5]
    raws = bm25_scores(tokenize_weighted("驾照 考试"),
                       [tokenize_weighted(memory_text(m)) for m in pool])
    print(f"    {len(pool)} 条候选的 BM25 原始分：{[round(r, 3) for r in raws]}")
    top_raw = max(raws)
    half = dataclasses.replace(cfg, min_raw_score=top_raw / 2)
    over = dataclasses.replace(cfg, min_raw_score=top_raw * 1.5)
    kept = recall("驾照 考试", pool, stage, half, NOW)
    check("min_raw_score=最高原始分的一半 → 仍然召回相关记忆", len(kept) >= 1,
          f"原始分={[round(r, 3) for r in raws]}")
    check("min_raw_score 高于所有原始分 → 一条都不召回（绝对门槛真的生效）",
          recall("驾照 考试", pool, stage, over, NOW) == [])
    check("min_raw_score=0.0（默认）与不设门槛等价",
          [r.memory["id"] for r in recall("驾照 考试", pool, stage, cfg, NOW)]
          == [r.memory["id"] for r in recall("驾照 考试", pool, stage,
                                             dataclasses.replace(cfg, min_raw_score=0.0), NOW)])

    print()
    print(f"memory 探针：通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
