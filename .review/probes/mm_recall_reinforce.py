"""Stream A 探针 4：召回强化能否被刷 / 会不会被误记 / 非"说出口"路径会不会 +1。

README 承诺：
  「只有她真的把这条记忆说出口，才算被想起：recall_count += 1，保留率随之上升。」
  「被塞进提示词 ≠ 被想起（mark_injected 只记账，不强化）」

逐条打脸：
  1. is_cited 的字面重合判定有多松（common suffix / 停用词）
  2. 一句话能把**多条**记忆一起 +1 吗（一条回复强化 N 条）
  3. 检索/注入/做梦/工具 search 会不会误强化
  4. 同一个 id 在 recalled 里出现两次会不会 +2（mark_cited 的 IN (...) 语义）
  5. 强化后模糊度是否真的下降（机制是否闭环）

跑法：
    python .review\\probes\\mm_recall_reinforce.py
"""

from __future__ import annotations

import sys
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
from giftia.memory import (  # noqa: E402
    Recalled,
    effective_blur,
    is_cited,
    mark_cited,
    recall,
)
from giftia.store import Store  # noqa: E402

NOW = datetime.now()


def main() -> int:
    tmp = fresh_dir("reinforce")
    settings = settings_for(tmp)
    cfg = settings.memory
    failures = 0

    title("1. is_cited 的判定有多松（cite_min_shared=%d, cite_min_chars=%d）"
          % (cfg.cite_min_shared, cfg.cite_min_chars))
    cases = [
        ("完全无关的寒暄", "今天天气不错，你在干嘛呢？", "搭档说：他下周三要去考驾照，很紧张。"),
        ("只共享一个公共词「搭档说」", "搭档说：他今天心情很好。", "搭档说：他下周三要去考驾照，很紧张。"),
        ("共享「搭档说：他」", "搭档说：他明天要去剪头发。", "搭档说：他下周三要去考驾照，很紧张。"),
        ("真的说了这条记忆", "你不是下周三要考驾照吗，紧张吗？", "搭档说：他下周三要去考驾照，很紧张。"),
        ("模型自己编的相似句", "我记得你下周三有个考试，别紧张。", "搭档说：他下周三要去考驾照，很紧张。"),
        ("六个字的连续重合（如「要去考驾照」）", "哦对，你要去考驾照对吧。", "搭档说：他下周三要去考驾照，很紧张。"),
        ("纯标点/语气词回复", "嗯嗯，好的呀。", "搭档说：他下周三要去考驾照，很紧张。"),
        ("回复里出现一个二元组「体检」", "体检结果出来了吗", "搭档说：他明天要去医院做体检。"),
    ]
    false_positives = 0
    for label, reply, summary in cases:
        got = is_cited(reply, summary, cfg)
        print(f"    {label:<34} → is_cited={got}")
        if label in ("完全无关的寒暄", "纯标点/语气词回复") and got:
            false_positives += 1
    check(
        "无关回复不得判为引用（避免「没提过的事莫名变清晰」）",
        false_positives == 0,
        f"误判 {false_positives} 例",
    )
    if false_positives:
        failures += 1

    # 「搭档说：他」 是不是就够 3 个共享词？
    r_counts = None
    from giftia.memory import tokenize_weighted

    summary = "搭档说：他下周三要去考驾照，很紧张。"
    reply = "搭档说：他明天要去剪头发。"
    shared = [t for t in tokenize_weighted(summary) if t in tokenize_weighted(reply)]
    print(f"    样例共享词元：{sorted(set(shared))}")
    print(f"    summary 词元权重≥2 的命中数 = "
          f"{sum(1 for t, w in tokenize_weighted(summary).items() if w >= 2.0 and t in tokenize_weighted(reply))}")

    title("2. 一条回复能不能同时强化多条记忆")
    b = make_brain(tmp / "multi", settings=settings)
    store = b.store
    inst = b.instance_no
    ids = []
    summaries = [
        "搭档说他下周三要去考驾照，很紧张。",
        "搭档说他下周要去考驾照的科目三。",
        "搭档说他下周要去考场踩点。",
    ]
    for s in summaries:
        ids.append(store.add_memory(
            instance_no=inst, created_ts=NOW.isoformat(timespec="seconds"),
            happened_ts=NOW.isoformat(timespec="seconds"), summary=s, importance=0.8,
        ))
    rec = [
        Recalled(memory={"id": i, **{"summary": s}}, score=1.0, text=s, blurred=False)
        for i, s in zip(ids, summaries)
    ]
    reply = "你不是下周三要去考驾照吗？科目三别紧张。"
    cited = mark_cited(store, rec, reply, cfg, NOW.isoformat(timespec="seconds"))
    rows = {r["id"]: r["recall_count"] for r in store._rows(
        "SELECT id, recall_count FROM memories ORDER BY id")}
    print(f"    回复：{reply!r}")
    print(f"    被强化的 id：{cited} → recall_count={rows}")
    check(
        "P2-3 一条回复可以同时给多条记忆 +1（模糊判定下的连带强化）",
        not (len(cited) > 1),
        f"一句话强化了 {len(cited)} 条",
    )
    if len(cited) > 1:
        failures += 1

    title("3. 同一个 id 重复出现会不会 +2")
    dup = [rec[0], rec[0]]
    before = {r["id"]: r["recall_count"] for r in store._rows(
        "SELECT id, recall_count FROM memories ORDER BY id")}
    store.mark_cited([ids[0], ids[0]], NOW.isoformat(timespec="seconds"))
    after = {r["id"]: r["recall_count"] for r in store._rows(
        "SELECT id, recall_count FROM memories ORDER BY id")}
    print(f"    mark_cited([{ids[0]}, {ids[0]}]) → {before[ids[0]]} → {after[ids[0]]}"
          f"（IN (?,?) 去重，只 +1）")
    check("重复 id 只 +1（SQL IN 天然去重）", after[ids[0]] == before[ids[0]] + 1,
          f"{before[ids[0]]} → {after[ids[0]]}")

    title("4. 非「说出口」路径会不会误强化")
    b2 = make_brain(tmp / "paths", settings=settings)
    s2, i2 = b2.store, b2.instance_no
    mid = s2.add_memory(
        instance_no=i2, created_ts=NOW.isoformat(timespec="seconds"),
        happened_ts=NOW.isoformat(timespec="seconds"),
        summary="搭档说他下周三要去考驾照，很紧张。", importance=0.9,
    )
    mems = s2.active_memories(i2, view=Store.ALL_SCOPES)
    hits = recall("考驾照", mems, stage(settings, "clear"), cfg, NOW)
    s2.mark_injected([r.memory["id"] for r in hits], NOW.isoformat(timespec="seconds"))
    row = s2._rows("SELECT recall_count, injected_at FROM memories WHERE id = ?", (mid,))[0]
    print(f"    检索 + mark_injected 之后：recall_count={row['recall_count']} "
          f"injected_at={row['injected_at']}")
    check("mark_injected 不增加 recall_count", row["recall_count"] == 0, str(row["recall_count"]))
    if row["recall_count"] != 0:
        failures += 1

    # 工具 search
    from giftia import agent_tools

    b2.handle("考驾照的事", user_id="handler", scene="c2c")
    out = agent_tools._search(b2, {"query": "考驾照"})
    row2 = s2._rows("SELECT recall_count FROM memories WHERE id = ?", (mid,))[0]
    print(f"    工具 search 输出首行：{out.splitlines()[0][:60]!r}")
    print(f"    search 之后 recall_count={row2['recall_count']}")
    check("工具 search 不增加 recall_count（只说「想起来这些」）",
          row2["recall_count"] == 0, str(row2["recall_count"]))
    if row2["recall_count"] != 0:
        failures += 1

    title("5. 强化是否真的让模糊度下降（机制闭环）")
    m_before = mem(1, "搭档说他下周三要去考驾照，很紧张。", importance=0.8, strength=1.8,
                   created_ts=(NOW.replace(year=NOW.year - 1)).isoformat(timespec="seconds"),
                   recall_count=0)
    m_after = {**m_before, "recall_count": 3,
               "last_recalled_at": NOW.isoformat(timespec="seconds")}
    st = stage(settings, "fading")
    print(f"    fading 阶段：recall_count=0 → blur={effective_blur(m_before, st, cfg, NOW):.4f}")
    print(f"                 recall_count=3（刚说过） → blur={effective_blur(m_after, st, cfg, NOW):.4f}")
    check("说过之后确实更清晰", effective_blur(m_after, st, cfg, NOW) < effective_blur(m_before, st, cfg, NOW))

    title("6. 时钟回拨后'强化'是否还能生效/反向生效")
    # 回拨到"她还没说过"之前：last_recalled_at 变成未来 → Δt=0 → 保留率=1
    back = NOW.replace(year=NOW.year - 1)
    print(f"    last_recalled_at={m_after['last_recalled_at']}，把 now 回拨一年："
          f"blur={effective_blur(m_after, st, cfg, back):.4f}")
    print("    说明：_age_hours 有 max(0.0)，回拨只让 Δt 归零 —— 保留率恒为 1，"
          "刻意回拨时间可以把任意记忆钉在'最清晰'状态。")

    print()
    print(f"召回强化探针失败计数：{failures}")
    print(f"（临时数据根：{TMPROOT}）")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
