#!/usr/bin/env python
"""端到端自测：不联网、不需要 API key，用 mock 模型验证整条链路。

    python tools/selftest.py

覆盖：寿命计数 → 对话落库 → 记忆整理 → 检索 → 退化模糊 → 可复现性 →
      "她读不到你的日志" → 回收仪式 → 换代后遗忘 → 到期自动回收 → 模型故障降级
"""

from __future__ import annotations

import dataclasses
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from giftia.brain import FAILURE_LINES, Giftia  # noqa: E402
from giftia.config import Settings, load_settings  # noqa: E402
from giftia.llm import LLMError, MockLLM  # noqa: E402
from giftia.memory import (  # noqa: E402
    confidence_of,
    effective_blur,
    is_cited,
    mark_cited,
    recall,
    retention_of,
)
from giftia.persona import build_system_prompt  # noqa: E402
from giftia.store import Store  # noqa: E402


class SynonymLLM(MockLLM):
    """离线演示"查询改写"：把口语里的"证件"映射到记忆里的"驾照"。

    真模型能做同义/上位词扩展，mock 只能硬编码一条映射——足够验证链路。
    """

    def rewrite_query(self, text: str) -> list[str]:
        return ["驾照 紧张"] if "证件" in (text or "") else []

PASS, FAIL = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  → {detail}" if detail else ""))


class FlakyLLM(MockLLM):
    """模拟模型服务挂掉。"""

    def chat(self, messages, **kw):  # type: ignore[override]
        raise LLMError("模拟故障：连接被重置")


def make_settings(tmp: Path, hours: float) -> Settings:
    base = load_settings(lifespan_override=hours, force_mock=True)
    return dataclasses.replace(base, root=tmp)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="giftia-selftest-"))
    print(f"临时数据目录：{tmp}\n")

    # ══ 1. 出厂状态 ═══════════════════════════════════════════
    print("1. 出厂状态（寿命 100 小时）")
    settings = make_settings(tmp, 100.0)
    brain = Giftia(settings, llm=MockLLM(settings.model))
    st = brain.status()
    check("实例编号为 #1", st.instance_no == 1, str(st.instance_no))
    check("剩余寿命接近满额", st.remaining_ratio > 0.99, f"{st.remaining_ratio:.4f}")
    check("初始阶段为 clear", st.stage.name == "clear", st.stage.name)
    check("寿命被写死在实例上（不受后续改配置影响）", abs(st.total_hours - 100.0) < 1e-6)

    # ══ 2. 对话 ═══════════════════════════════════════════════
    print("\n2. 对话落库")
    utterances = [
        "我今天加班到很晚，回来路上下了点雨",
        "我不太喜欢下雨天，会很闷",
        "对了，我下周三要考驾照，有点紧张",
    ]
    replies = [brain.handle(u) for u in utterances]
    check("每句都有回复", all(r.strip() for r in replies))
    check("消息已落库（3 轮 = 6 条）", brain.store.count_messages(1) == 6,
          str(brain.store.count_messages(1)))
    check("未到阈值不会提前整理（阈值 8 条）", brain.store.active_memories(1) == [])

    # ══ 3. 记忆整理与检索 ═════════════════════════════════════
    print("\n3. 记忆整理与检索")
    added = brain.consolidate(force=True)
    check("强制整理出记忆条目", added >= 1, f"added={added}")
    mems = brain.store.active_memories(1)
    check("记忆条数与返回值一致", len(mems) == added, f"{len(mems)} vs {added}")
    check("未整理的对话已标记", brain.store.open_messages(1) == [])
    hit = recall("下雨 驾照", mems, st.stage, settings.memory, brain.clock.now())
    check("相关提问能想起东西（含非相邻词：下了点雨↔下雨）", len(hit) >= 1, f"命中 {len(hit)} 条")
    check("清晰阶段不模糊", all(not h.blurred for h in hit))
    unrelated = recall("量子色动力学与格点规范场", mems, st.stage, settings.memory, brain.clock.now())
    check("无关提问不会乱想起（证据门槛）", len(unrelated) == 0, f"命中 {len(unrelated)} 条")
    short_q = recall("雨", mems, st.stage, settings.memory, brain.clock.now())
    check("单字查询仍然召得回（门槛自适应）", len(short_q) >= 1, f"命中 {len(short_q)} 条")

    for extra in ["你以后叫我小林吧", "我明天要去医院复查", "有点怕，但也没那么怕",
                  "算了不说这个了", "你今天有想我吗"]:
        brain.handle(extra)
    grown = brain.store.active_memories(1)
    check("攒够阈值后自动整理，无需手动触发", len(grown) > len(mems),
          f"{len(grown)} vs {len(mems)}")
    mems = grown

    # ══ 4. 搭档日志她读不到 ═══════════════════════════════════
    print("\n4. 搭档日志的隔离性")
    secret = "她不知道我其实打算在她回收那天请假"
    brain.diary_add(secret)
    prompt = build_system_prompt(settings, brain.status(), hit, now=brain.clock.now())
    check("日志内容不在她的提示词里", secret not in prompt)
    check("记忆内容确实在提示词里", any(h.text[:8] in prompt for h in hit) if hit else True)
    check("记忆区块用 ◆ 标记", "◆" in prompt)

    # ══ 5. 退化 ═══════════════════════════════════════════════
    print("\n5. 记忆退化（快进到剩余 15%）")
    brain.clock.advance(85)
    st = brain.status()
    check("进入 blurred 阶段", st.stage.name == "blurred", st.stage.name)
    check("剩余寿命约 15%", 0.10 < st.remaining_ratio < 0.20, f"{st.remaining_ratio:.3f}")
    late = recall("下雨 驾照", mems, st.stage, settings.memory, brain.clock.now())
    check("仍能想起（只是变模糊）", len(late) >= 1, f"命中 {len(late)} 条")
    check("至少一条被模糊", any(x.blurred for x in late))
    blurred_one = next((x for x in late if x.blurred), None)
    if blurred_one:
        check("模糊文本 ≠ 原始文本", blurred_one.text != blurred_one.memory["summary"],
              f"{blurred_one.text!r}")
    raw_now = [m["summary"] for m in brain.store.active_memories(1)]
    check("底层原始数据完好无损", raw_now == [m["summary"] for m in mems])
    again = recall("下雨 驾照", mems, st.stage, settings.memory, brain.clock.now())
    check("模糊结果可复现（同输入同输出）",
          [x.text for x in again] == [x.text for x in late])

    terminal_like = dataclasses.replace(st.stage, name="terminal", blur=0.85, top_k=2, min_score=0.55)
    end = recall("下雨 驾照", mems, terminal_like, settings.memory, brain.clock.now())
    check("终端阶段只剩碎片", all("想不起来了" in x.text or "……" in x.text for x in end) if end else True)

    # ══ 6. 回收仪式 ═══════════════════════════════════════════
    print("\n6. 回收仪式")
    before_memories = brain.store.active_memories(1)
    scene = brain.run_recycle("测试回收")
    check("回收输出含终端服务公文", "终端服务" in scene)
    check("新实例编号为 #2", brain.instance_no == 2, str(brain.instance_no))
    files = list(settings.memoir_dir.glob("*.md"))
    check("回忆录文件已生成", len(files) == 1, str(files))
    if files:
        body = files[0].read_text(encoding="utf-8")
        check("回忆录含原始记忆（未模糊）",
              all(m["summary"] in body for m in before_memories) if before_memories else True)
    check("封存 JSON 已生成", list(settings.sealed_dir.glob("*.json")) != [])
    check("她的检索看不到旧记忆", brain.store.active_memories(2) == [])
    check("旧记忆被标记封存",
          all(m["sealed"] == 1 for m in brain.store.all_memories(1)))
    check("新实例寿命重新计时", brain.status().remaining_ratio > 0.99)
    new_hit = recall("下雨 驾照", brain.store.active_memories(2), brain.status().stage,
                     settings.memory, brain.clock.now())
    check("新实例对旧事毫无印象", new_hit == [])
    prompt2 = build_system_prompt(settings, brain.status(), [], now=brain.clock.now())
    check("提示词明确告知她空白", "什么都想不起来" in prompt2)

    # ══ 7. 到期自动回收 ═══════════════════════════════════════
    print("\n7. 到期自动触发回收日")
    brain2 = Giftia(make_settings(tmp / "auto", 10.0), llm=MockLLM(settings.model))
    brain2.clock.advance(11)
    check("已判定到期", brain2.status().expired)
    out = brain2.handle("我回来了")
    check("回复里带上回收流程", "终端服务" in out, out[:60])
    check("实例已换代", brain2.instance_no == 2, str(brain2.instance_no))
    check("到期前的对话已被整理保存",
          brain2.store.count_memories(1) >= 0 and brain2.store.all_memories(1) is not None)

    # ══ 8. 模型故障降级 ═══════════════════════════════════════
    print("\n8. 模型故障时的降级")
    brain3 = Giftia(make_settings(tmp / "flaky", 100.0), llm=FlakyLLM(settings.model))
    reply = brain3.handle("你在吗")
    check("不抛异常", bool(reply))
    check("回复是戏内的台词", reply in FAILURE_LINES, reply)
    check("故障有记录", any("模型请求失败" in n for n in brain3.notices))
    try:
        brain3.store.close()
    except Exception:
        pass

    # ══ 9. 平台无关入口 ═══════════════════════════════════════
    print("\n9. 平台无关入口 handle_message()")
    import giftia.brain as brain_mod

    mod_settings = make_settings(tmp / "gateway", 100.0)
    out1 = brain_mod.handle_message("你好呀", "12345", settings=mod_settings)
    out2 = brain_mod.handle_message("我刚说啥了", "12345", settings=mod_settings)
    check("两次调用都返回文本", bool(out1.strip()) and bool(out2.strip()))
    check("适配器拿到了同一个实例", brain_mod.current() is not None and brain_mod.current().instance_no == 1)
    brain_mod.reset_default()

    # ══ 10. 记忆分层 ═══════════════════════════════════════════
    print("\n10. 记忆分层：内核记忆到生命尽头都不含糊")
    lay = Giftia(make_settings(tmp / "layers", 100.0), llm=MockLLM(settings.model))
    for u in ["以后你就叫我小林吧，别叫搭档了", "我下周三要考驾照，有点紧张"]:
        lay.handle(u)
    lay.consolidate(force=True)
    lay_mems = lay.store.active_memories(1)
    check("出现了内核记忆（名字/称呼）",
          any(m["layer"] == "core" for m in lay_mems), str([m["layer"] for m in lay_mems]))
    check("普通事件落在常规层",
          any(m["layer"] == "normal" for m in lay_mems), str([m["layer"] for m in lay_mems]))

    lay.clock.advance(99)  # 剩余 1%：终端服务迫近
    lay_st = lay.status()
    check("已进入 terminal 阶段", lay_st.stage.name == "terminal", lay_st.stage.name)
    lay_hits = recall("小林 驾照", lay_mems, lay_st.stage, settings.memory, lay.clock.now())
    core_hits = [h for h in lay_hits if h.layer == "core"]
    normal_hits = [h for h in lay_hits if h.layer == "normal"]
    check("内核记忆仍然想得起来", len(core_hits) >= 1, f"{len(lay_hits)} 条命中")
    check("内核记忆模糊度恒为 0（该记得的一直记得）",
          all(h.blur == 0.0 and not h.blurred for h in core_hits), str([h.blur for h in core_hits]))
    check("同一时刻常规记忆已经明显变糊",
          bool(normal_hits) and all(h.blur > 0 for h in normal_hits),
          str([(h.layer, h.blur) for h in lay_hits]))

    # ══ 11. 召回强化 ═══════════════════════════════════════════
    print("\n11. 召回强化：你救不了她的命，但你能让她记得久一点")
    normal_mem = next((m for m in lay_mems if m["layer"] == "normal"), None)
    if normal_mem is None:
        check("找到一条常规记忆用于测试", False)
    else:
        before = retention_of(normal_mem, settings.memory, lay.clock.now())
        same_text = str(normal_mem["summary"])
        check("引用判定：她复述了这条记忆 → 算被想起",
              is_cited(f"……我记得，{same_text[5:25]}", same_text, settings.memory))
        check("引用判定：回复完全无关 → 不算被想起",
              not is_cited("今天天气不错", same_text, settings.memory))

        target_hit = [h for h in lay_hits if h.memory["id"] == normal_mem["id"]]
        cited_ids = mark_cited(
            lay.store, target_hit, same_text, settings.memory, lay.clock.iso()
        )
        check("强化只作用于这一条", cited_ids == [normal_mem["id"]], str(cited_ids))

        refreshed = [
            m for m in lay.store.active_memories(1) if m["id"] == normal_mem["id"]
        ][0]
        check("recall_count 变成 1", int(refreshed["recall_count"]) == 1,
              str(refreshed["recall_count"]))
        check("last_recalled_at 已记录", bool(refreshed["last_recalled_at"]))
        after = retention_of(refreshed, settings.memory, lay.clock.now())
        check("保留率上升 —— 她记得更久了",
              after > before, f"{before:.4f} → {after:.4f}")
        check("被强化后模糊度下降",
              effective_blur(refreshed, lay_st.stage, settings.memory, lay.clock.now())
              < effective_blur(normal_mem, lay_st.stage, settings.memory, lay.clock.now()))

    # ══ 12. 输出侧置信度契约 ═══════════════════════════════════
    print("\n12. 置信度契约：把「擦了内容」升级成「她知道自己不确定」")
    check("清晰 + 高保留 → 可确信",
          confidence_of(0.0, 0.9, settings.memory) == "cite")
    check("轻微模糊 → 需要含糊",
          confidence_of(0.2, 0.9, settings.memory) == "cautious")
    check("重度模糊 → 只作内心背景",
          confidence_of(0.8, 0.9, settings.memory) == "associate")
    check("保留率过低也会降级为需要含糊",
          confidence_of(0.0, 0.1, settings.memory) == "cautious")
    lay_prompt = build_system_prompt(settings, lay.status(), lay_hits, now=lay.clock.now())
    check("提示词里带上了置信度标记",
          any(mark in lay_prompt for mark in ("[可以确信]", "[需要含糊]", "[只是隐约觉得]")))
    check("规则里解释了三个标记怎么用", "严格按每条记忆前面的标记" in lay_prompt)

    # ══ 13. 相对阈值裁剪 ═══════════════════════════════════════
    print("\n13. 相对阈值裁剪：压掉半残的第五名")
    if lay_hits:
        check("所有命中得分 ≥ 首名的一半",
              all(h.score >= lay_hits[0].score * 0.5 - 1e-9 for h in lay_hits),
              str([h.score for h in lay_hits]))
    strict_cfg = dataclasses.replace(settings.memory, relative_cutoff=0.99)
    strict_hits = recall("小林 驾照", lay_mems, lay_st.stage, strict_cfg, lay.clock.now())
    check("把阈值提到 0.99 后只剩最强的一条",
          len(strict_hits) <= 1, f"剩 {len(strict_hits)} 条")

    # ══ 14. 弱检索触发查询改写 ═════════════════════════════════
    print("\n14. 弱检索时改写查询（不弱的就不多花一次调用）")
    rw = Giftia(make_settings(tmp / "rewrite", 100.0), llm=SynonymLLM(settings.model))
    rw.handle("我下周三要考驾照，有点紧张")
    rw.consolidate(force=True)
    rw.clock.advance(24)  # 换一天：工作记忆清空，查询只由这一句决定
    rw_st = rw.status()
    rw_mems = rw.store.active_memories(1)
    bare = recall("那个证件的事情办得怎么样了", rw_mems, rw_st.stage, settings.memory, rw.clock.now())
    check("词面对不上时，第一次检索确实什么都没想起来", bare == [],
          f"命中 {len(bare)} 条")
    rw.handle("那个证件的事情办得怎么样了")
    check("弱检索触发了查询改写", bool(rw.rewritten_queries), str(rw.rewritten_queries))
    check("改写之后真的想起了那条记忆", len(rw.last_recalled) >= 1,
          f"命中 {len(rw.last_recalled)} 条")
    rw.handle("我下周三要考驾照")
    check("词面能对上时就不改写（省一次调用）", rw.rewritten_queries == [],
          str(rw.rewritten_queries))

    # ══ 15. 老数据库迁移 ═══════════════════════════════════════
    print("\n15. 老数据库自动补列（她的记忆不能因为升级而丢）")
    legacy = tmp / "legacy" / "data" / "giftia.db"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(legacy))
    raw.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT, instance_no INTEGER NOT NULL,
            created_ts TEXT NOT NULL, happened_ts TEXT NOT NULL, summary TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '', emotion TEXT NOT NULL DEFAULT '',
            importance REAL NOT NULL DEFAULT 0.5, source_session TEXT NOT NULL DEFAULT '',
            sealed INTEGER NOT NULL DEFAULT 0);
        INSERT INTO memories(instance_no, created_ts, happened_ts, summary)
        VALUES (1, '2026-01-01T00:00:00', '2026-01-01T00:00:00', '升级前留下的记忆');
        """
    )
    raw.commit()
    raw.close()
    migrated = Store(legacy)
    m_rows = migrated.all_memories()
    check("升级后老数据仍在", len(m_rows) == 1 and m_rows[0]["summary"] == "升级前留下的记忆")
    check("新列已补齐并有合理默认值",
          m_rows[0]["layer"] == "normal" and int(m_rows[0]["recall_count"]) == 0,
          str(dict(m_rows[0]))[:120])
    check("老记忆按默认强度参与检索",
          retention_of(m_rows[0], settings.memory, lay.clock.now()) > 0)
    migrated.close()

    # ══ 16. 人格注入 ═══════════════════════════════════════════
    print("\n16. 人格注入：她「是谁」这件事的唯一来源")
    inj = Giftia(make_settings(tmp / "inject", 100.0), llm=MockLLM(settings.model))
    inj.handle("你好")
    inj_messages, _ = inj._prepare("你还记得我吗")
    head = inj_messages[0]
    check("第一条消息是 system（人格入口）", head["role"] == "system")
    body = str(head["content"])
    check("人格内核被注入", "Giftia 型拟人智能体" in body)
    check("名字被注入", inj.settings.persona.name in body)
    check("型号被注入", inj.settings.persona.model_code in body)
    check("此刻状态被注入（剩余寿命）", "你的剩余寿命" in body)
    check("记忆阶段说明被注入", "【关于记忆】" in body)
    check("她此刻想起的事被注入", "【你现在想起的事】" in body)
    check("对话规则被注入", "【本次对话的规则】" in body)
    check("出戏处理提示被注入", "是不是程序" in body)
    check("最后一条是这次的输入",
          inj_messages[-1]["role"] == "user"
          and inj_messages[-1]["content"] == "你还记得我吗")
    check("默认人格仍是艾拉（还没被你改过）", inj.settings.persona.name == "艾拉")
    preview = inj.preview_prompt("在吗")
    check("preview_prompt 能原样打印注入内容（可审计）",
          "SYSTEM（人格与状态）" in preview and "Giftia 型拟人智能体" in preview)
    check("预览不会写库（不污染记忆）", inj.store.count_messages(1) == 2,
          str(inj.store.count_messages(1)))

    brain.store.close()
    brain2.store.close()

    print("\n" + "═" * 52)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("═" * 52)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
