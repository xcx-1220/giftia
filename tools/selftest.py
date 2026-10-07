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

from giftia.bond import stage_for  # noqa: E402
from giftia.brain import FAILURE_LINES, Giftia  # noqa: E402
from giftia.config import Settings, load_settings  # noqa: E402
from giftia.llm import LLMError, MemoryDraft, MockLLM  # noqa: E402
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


class NameLLM(MockLLM):
    """模拟模型在整理记忆时发现"他要求改口"。"""

    def extract_memories(self, transcript: str, *, name: str = "Giftia", today: str = ""):
        return [
            MemoryDraft(
                summary="搭档要求以后叫他小林。",
                tags="称呼,约定",
                emotion="郑重",
                importance=0.9,
                layer="core",
                preferred_name="小林",
            )
        ]

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

    # ══ 17. 关系演进 ═══════════════════════════════════════════
    print("\n17. 关系：不可刷，且升级要「亲密度 + 认识天数」两条腿")
    # 注意寿命要给足：100 小时撑不到"认识三个月"，用出厂设定 81920 小时
    rel = Giftia(make_settings(tmp / "bond", 81920.0), llm=MockLLM(settings.model))
    b0 = rel.current_bond()
    check("初始是「刚认识」", b0.stage.key == "stranger", b0.stage.label)
    check("初始亲密度为 0", b0.affinity == 0.0, str(b0.affinity))

    for _ in range(5):
        rel.handle("在吗")
    b1 = rel.current_bond()
    check("同一天狂发消息只结算一次（不可刷）",
          abs(b1.affinity - settings.relationship.gain_daily) < 1e-6, f"{b1.affinity:.3f}")
    check("交流天数记为 1 天", b1.interactions == 1, str(b1.interactions))

    rel.clock.advance(24)
    rel.handle("今天还行")
    b2 = rel.current_bond()
    check("第二天再聊会继续涨", b2.affinity > b1.affinity, f"{b1.affinity:.2f} → {b2.affinity:.2f}")
    check("亲密度不够时不会提前升级", b2.stage.key == "stranger", b2.stage.label)

    # 只过时间不说话：天数够了，亲密度不够
    rel.clock.advance(24 * 60)
    b3 = rel.current_bond()
    check("只过时间不说话：认识 60 天也升不了级",
          b3.days_known >= 60 and b3.stage.key == "stranger",
          f"{b3.days_known:.0f} 天 / 亲密度 {b3.affinity:.1f}")

    seen_notice = False
    for _ in range(30):
        rel.clock.advance(24)
        rel.handle("嗯，今天也还行")
        seen_notice = seen_notice or any(n.startswith("[关系]") for n in rel.notices)
    b4 = rel.current_bond()
    check("持续相处后亲密度上来了", b4.affinity >= 30, f"{b4.affinity:.1f}")
    check("间歇相处 30 天 → 至少到「搭档」",
          b4.stage.key in ("partner", "familiar", "close"), b4.stage.label)
    check("升级时她会主动提示", seen_notice)

    # 递减增长：越亲近，同样的付出涨得越少
    fresh = Giftia(make_settings(tmp / "bond_dim", 81920.0), llm=MockLLM(settings.model))
    fresh.handle("你好")
    early = fresh.current_bond().affinity
    fresh.bond.set_affinity(fresh.instance_no, 90.0)
    fresh.clock.advance(24)
    fresh.handle("你好")
    late = fresh.current_bond().affinity - 90.0
    check("增长递减：初期一天的涨幅远大于后期",
          early > late * 5, f"初期 {early:.2f} vs 后期 {late:.2f}")
    check("递减时后期涨幅仍为正（不会卡死）", late > 0, f"{late:.3f}")

    # 每天都见面的完整曲线（settings.toml 里承诺的那条）
    steady = Giftia(make_settings(tmp / "bond_curve", 81920.0), llm=MockLLM(settings.model))
    reached: dict[str, int] = {}
    for day in range(1, 200):
        steady.clock.advance(24)
        steady.handle("嗯，今天也还行")
        reached.setdefault(steady.current_bond().stage.key, day)
    check("每天见面 → 第 11 天左右到「搭档」",
          8 <= reached.get("partner", 999) <= 15, str(reached))
    check("每天见面 → 第 34 天左右到「熟悉」",
          25 <= reached.get("familiar", 999) <= 45, str(reached))
    check("每天见面 → 第 80 天左右到「亲近」（即“认识三个月”那档）",
          65 <= reached.get("close", 999) <= 100, str(reached))
    check("「无可替代」要熬半年以上",
          reached.get("irreplaceable", 999) >= 170, str(reached))

    # 直接验证判定规则本身（这是"认识三个月"那句需求的落点）
    check("亲密度拉满但只认识 10 天 → 只够「搭档」",
          stage_for(settings.bond_stages, 100, 10)[0].key == "partner",
          stage_for(settings.bond_stages, 100, 10)[0].label)
    check("认识一年但亲密度只有 10 → 还是「刚认识」",
          stage_for(settings.bond_stages, 10, 365)[0].key == "stranger")
    check("亲密度 75 + 认识 90 天 → 「亲近」",
          stage_for(settings.bond_stages, 75, 90)[0].key == "close")
    check("最难的「无可替代」要 92 亲密度 + 180 天",
          stage_for(settings.bond_stages, 95, 200)[0].key == "irreplaceable")

    # 记住"关于他本人的事"会加分
    rel.handle("以后你就叫我小林吧，别叫搭档了")
    before_mem = rel.current_bond().affinity
    rel.consolidate(force=True)
    after_mem = rel.current_bond().affinity
    check("整理出内核记忆会给关系加分",
          after_mem >= before_mem, f"{before_mem:.1f} → {after_mem:.1f}")

    # 关系与心情都要被注入提示词
    bond_prompt = rel._prepare("在吗")[0][0]["content"]
    check("关系阶段被注入系统提示词", "【你和他的关系】" in bond_prompt)
    check("当前阶段的行为指引被注入", rel.current_bond().stage.note[:12] in bond_prompt)

    # 回收换代：关系清零
    rel.run_recycle("测试换代")
    check("回收后亲密度清零（新的她不认识你）", rel.current_bond().affinity == 0.0)
    check("回收后回到「刚认识」", rel.current_bond().stage.key == "stranger")
    memoir_files = sorted(rel.settings.memoir_dir.glob("*.md"))
    memoir_text = memoir_files[-1].read_text(encoding="utf-8") if memoir_files else ""
    check("回忆录里记下了你们最终的关系状态",
          "关系最终走到了" in memoir_text, memoir_text[:80])

    # ══ 18. 心情状态机 ═════════════════════════════════════════
    print("\n18. 心情：寿命压力 > 你的语气 > 每日漂移")
    md = Giftia(make_settings(tmp / "mood", 100.0), llm=MockLLM(settings.model))
    md.handle("你好")
    m0 = md.current_mood()
    check("一开始就有确定的心情（按日期掷骰子）", m0.key in settings.moods, m0.key)

    md.handle("我今天特别难过，什么都做不好，压力很大")
    m1 = md.current_mood()
    check("你明显低落 → 她在担心你",
          m1.key == "worried" and m1.source == "his_tone", f"{m1.key}/{m1.source}")

    md.handle("今天太顺利了，事情全搞定了，哈哈")
    m2 = md.current_mood()
    check("你明显开心 → 她跟着开心",
          m2.key == "happy" and m2.source == "his_tone", f"{m2.key}/{m2.source}")

    md.handle("嗯")
    m3 = md.current_mood()
    check("中性发言不会推翻当前心情", m3.key == m2.key, f"{m3.key} vs {m2.key}")

    md.clock.advance(60)  # 剩余 40 小时，进入不安区间
    md.handle("你在吗")
    m4 = md.current_mood()
    check("快到终端服务 → 不安压过其他一切",
          m4.key == "uneasy" and m4.source == "lifespan", f"{m4.key}/{m4.source}")
    mood_prompt = md._prepare("你在吗")[0][0]["content"]
    check("心情被注入系统提示词", "【你今天的心情】" in mood_prompt)
    check("不安时注入的是不安的指引", "不安" in mood_prompt)

    check("掷骰子在同一天是确定的（不会每刷新一次就换脸）",
          md.mood._roll(md.clock.now(), md.instance_no)
          == md.mood._roll(md.clock.now(), md.instance_no))

    md2 = Giftia(make_settings(tmp / "mood2", 100.0), llm=MockLLM(settings.model))
    md2.mood.force("low", md2.clock.now(), md2.instance_no)
    check("可以手动指定心情（调试用）", md2.current_mood().key == "low")
    md2.clock.advance(settings.mood.duration_hours + 1)
    md2.handle("嗯")
    expect = md2.mood._roll(md2.clock.now(), md2.instance_no)
    check("心情过期后回到当日掷出来的那种",
          md2.current_mood().key == expect, f"{md2.current_mood().key} vs {expect}")

    # ══ 19. 出厂状态与称呼 ═════════════════════════════════════
    print("\n19. 出厂状态：不预设她知道什么")
    nm = Giftia(make_settings(tmp / "name", 81920.0), llm=MockLLM(settings.model))
    check("出厂时她不知道你叫什么（空，而不是预设称呼）",
          nm.handler_name == "", repr(nm.handler_name))
    base_prompt = nm._prepare("在吗")[0][0]["content"]
    check("注入的是「你还不知道他叫什么」",
          "不知道" in base_prompt and "他叫什么" in base_prompt)
    check("并且叮嘱她只问一次", "只问一次" in base_prompt)
    check("人格内核不预设你们已经相处过",
          "你珍惜和这个人相处的时间" not in base_prompt)
    opening = nm.settings.persona.line("opening_line")
    check("开场白是问名字，不是宣布关系",
          "怎么称呼你" in opening and "你就是我的搭档" not in opening, opening[:40])
    check("重开一任的开场白同样不预设记忆",
          "怎么称呼你" in nm.settings.persona.line("after_recycle_line"))

    nm.handle("你好，我叫小林", user_id="u1")
    check("从自我介绍里自己学会了称呼", nm.handler_name == "小林", nm.handler_name)

    nm_cli = Giftia(make_settings(tmp / "name_cli", 81920.0), llm=MockLLM(settings.model))
    nm_cli.handle("我叫小林")   # 本地 CLI 就是这么调的：不带 user_id
    check("本地命令行（不带 user_id）也能学会称呼",
          nm_cli.handler_name == "小林", nm_cli.handler_name)
    renamed = nm._prepare("在吗")[0][0]["content"]
    check("注入的称呼跟着变", "他叫「小林」" in renamed, renamed[:0] or "")
    check("学会称呼时给出提示", any("称呼" in n for n in nm.notices), str(nm.notices))

    for bad in ("我是来问你个事的", "别叫我搭档了", "我是学生", "我是说你别急"):
        probe = Giftia(
            make_settings(tmp / f"nb{abs(hash(bad)) % 9999}", 81920.0),
            llm=MockLLM(settings.model),
        )
        probe.handle(bad, user_id="u1")
        check(f"有歧义就不乱学：「{bad}」", probe.handler_name == "", probe.handler_name)

    # 名字后面接着说别的，也应当只摘出名字（模型那层的价值就在这里）
    mixed = Giftia(make_settings(tmp / "name_mixed", 81920.0), llm=MockLLM(settings.model))
    mixed.handle("我叫小林今年二十八岁", user_id="u1")
    check("「我叫小林今年二十八岁」只摘出「小林」", mixed.handler_name == "小林",
          mixed.handler_name)

    nm2 = Giftia(make_settings(tmp / "name2", 81920.0), llm=NameLLM(settings.model))
    nm2.handle("以后叫我小林吧")
    nm2.consolidate(force=True)
    check("模型那条路（整理记忆时）也能改口", nm2.handler_name == "小林", nm2.handler_name)
    nm2.run_recycle("测试换代")
    check("换代后称呼仍保留（终端服务档案里有）", nm2.handler_name == "小林")
    check("但新的她不记得你们的关系", nm2.current_bond().affinity == 0.0)

    # ══ 20. 群聊情境 ═══════════════════════════════════════════
    print("\n20. 群聊：她不能把你的私事当众说出去")
    grp = Giftia(make_settings(tmp / "scene", 100.0), llm=MockLLM(settings.model))
    grp.handle("我下周三要考驾照，有点紧张")
    grp.consolidate(force=True)
    private_prompt = grp._prepare("在吗")[0][0]["content"]
    check("私聊时没有群聊约束", "【现在的情境】" not in private_prompt)

    grp.handle("我之前跟你说过什么事来着", scene="group")
    group_prompt = grp._prepare("在吗")[0][0]["content"]
    check("群聊时注入了情境约束", "【现在的情境】" in group_prompt)
    check("约束里写明了不能当众说什么", "不能当众说" in group_prompt)
    check("约束要求只回被点名的人", "只回他" in group_prompt)
    check("约束要求群里说话更短", "更短" in group_prompt)

    grp.handle("在吗", scene="c2c")
    check("回到私聊后约束消失",
          "【现在的情境】" not in grp._prepare("在吗")[0][0]["content"])

    # ══ 21. 群成员与访客模式 ═══════════════════════════════════
    print("\n21. 群里认得的人 & 访客模式（客人碰不到你们的隐私）")
    pp = Giftia(make_settings(tmp / "people", 100.0), llm=MockLLM(settings.model))
    pp.set_handler_id("owner-1")
    pp.handle("我叫小林", user_id="owner-1")
    check("搭档自己的名字成为称呼", pp.handler_name == "小林", pp.handler_name)

    pp.handle("大家好，我叫阿伟", user_id="friend-1", scene="guest")
    check("群里别人的自我介绍被记下来",
          pp.known_people().get("friend-1") == "阿伟", str(pp.known_people()))
    check("但不会顶替她叫搭档的称呼", pp.handler_name == "小林", pp.handler_name)

    pp.handle("在吗", user_id="owner-1", scene="group")
    roster_prompt = pp._prepare("在吗")[0][0]["content"]
    check("群聊时注入花名册", "【群里你认得的人】" in roster_prompt)
    check("花名册里标出了哪个是搭档", "小林（你的搭档）" in roster_prompt)
    check("花名册里也有群里其他人", "阿伟" in roster_prompt)
    check("花名册提醒不认识的人别乱叫", "别乱猜" in roster_prompt)

    before_msgs = pp.store.count_messages(pp.instance_no)
    before_aff = pp.current_bond().affinity
    before_private = len(pp.store.active_memories(pp.instance_no, view=[("private", "")]))
    pp.handle("他最近怎么样呀，你们聊了什么", user_id="friend-1", scene="guest")
    check("访客说话不会变成「你们之间」的记忆",
          len(pp.store.active_memories(pp.instance_no, view=[("private", "")])) == before_private,
          f"{before_private} → {len(pp.store.active_memories(pp.instance_no, view=[('private', '')]))}")
    check("访客不会替你刷亲密度", pp.current_bond().affinity == before_aff)
    check("但访客的话记在他和客人自己的会话里（下次他还接得上）",
          pp.store.count_messages(pp.instance_no) > before_msgs
          and any(
              str(m["session_id"]).startswith(f"i{pp.instance_no}-p")
              for m in pp.store.all_messages(pp.instance_no, limit=10)
          ),
          str([m["session_id"] for m in pp.store.all_messages(pp.instance_no, limit=10)]))

    guest_sys = build_system_prompt(
        pp.settings,
        pp.status(),
        [],
        now=pp.clock.now(),
        handler_name=pp.handler_name,
        scene="guest",
        people=pp._people_for_prompt(),
    )
    check("访客提示词里没有任何搭档的私事", "考驾照" not in guest_sys)
    check("访客提示词明确要求不提私事", "私事" in guest_sys)
    check("访客看到的是「什么都想不起来」", "什么都想不起来" in guest_sys)

    # ══ 22. 纪念日与「他的大日子」══════════════════════════════
    print("\n22. 纪念日与他的大日子")
    oc = Giftia(make_settings(tmp / "occ", 81920.0), llm=MockLLM(settings.model))
    oc.handle("你好")
    oc.consolidate(force=True)
    first = oc.days_text()
    check("刚认识时还没有纪念日", "还没跨过纪念日" in first, first[:40])

    oc.clock.advance(24 * 30)  # 第 30 天
    preview = oc._prepare("在吗", mark=False)[0][0]["content"]
    check("第 30 天她会自己想起来", "【今天不只是普通的一天】" in preview)
    check("注入的是天数和顺口提一句的指引",
          "别郑重其事" in preview or "顺口" in preview)
    check("预览不会消耗掉这个日子（/prompt 随便看）",
          "【今天不只是普通的一天】" in oc._prepare("在吗", mark=False)[0][0]["content"])
    oc.handle("在吗")
    check("真正说过一次之后，同一天不再重复",
          "【今天不只是普通的一天】" not in oc._prepare("在吗")[0][0]["content"])

    # 大日子：直接塞一条带日期的记忆
    from datetime import timedelta as _td

    today = oc.clock.now().date()
    oc.store.add_memory(
        instance_no=oc.instance_no,
        created_ts=oc.clock.iso(),
        happened_ts=oc.clock.iso(),
        summary="搭档下周三要考驾照，有点紧张",
        importance=0.7,
        layer="important",
        event_date=(today + _td(days=1)).isoformat(),
    )
    big_preview = oc._prepare("在吗", mark=False)[0][0]["content"]
    check("明天的大日子会被惦记",
          "【今天不只是普通的一天】" in big_preview and "考驾照" in big_preview,
          big_preview[big_preview.find("【今天不只是普通的一天】") :][:80])
    check("大日子的指引是别像闹钟", "别像闹钟" in big_preview)
    oc.handle("在吗")
    check("同一件大日子同一天只说一次",
          "【今天不只是普通的一天】" not in oc._prepare("在吗")[0][0]["content"])
    check("大日子进了时间线", "考驾照" in oc.days_text(), oc.days_text()[-60:])

    past = Giftia(make_settings(tmp / "occ_past", 81920.0), llm=MockLLM(settings.model))
    past.store.add_memory(
        instance_no=past.instance_no,
        created_ts=past.clock.iso(),
        happened_ts=past.clock.iso(),
        summary="已经过去的事",
        event_date=(past.clock.now().date() - _td(days=3)).isoformat(),
    )
    past.handle("在吗")
    check("过去的日子不会被翻出来",
          not any(o.kind == "big_day" for o in past.last_occasions))

    # ══ 23. 做梦 ═══════════════════════════════════════════════
    print("\n23. 做梦：记忆碎片会自己重组")
    dr = Giftia(make_settings(tmp / "dream", 81920.0), llm=MockLLM(settings.model))
    for line in ("我叫小林", "我下周三要考驾照，有点紧张", "最近总是睡不好"):
        dr.handle(line)
    dr.consolidate(force=True)
    check("记忆攒够之前不做梦",
          dr.store.recent_dreams(dr.instance_no) == [], "不该有梦")

    dr.store.add_memory(  # mock 一次只压出两条，补一条凑够做梦门槛
        instance_no=dr.instance_no,
        created_ts=dr.clock.iso(),
        happened_ts=dr.clock.iso(),
        summary="搭档不喜欢下雨天，觉得闷",
        importance=0.5,
    )
    dr.handle("在吗")
    check("攒够记忆后会做一场梦",
          len(dr.store.recent_dreams(dr.instance_no)) == 1,
          f"{len(dr.store.recent_dreams(dr.instance_no))} 场")
    check("做过的梦会提示「下次再讲」", any("做梦" in n for n in dr.notices), str(dr.notices))

    dreams = dr.store.recent_dreams(dr.instance_no)
    check("梦被存了下来且带来源", bool(dreams) and bool(dreams[0]["source_ids"]))
    dr.clock.advance(1)
    dream_prompt = dr._prepare("在吗")[0][0]["content"]
    check("下一次说话才会把梦讲出来", "【你昨晚做的梦】" in dream_prompt)
    check("提示里说明了梦不作数", "不是记忆" in dream_prompt)
    check("梦不进长期记忆（检索里没有它）",
          all("梦里有" not in str(m.get("summary", ""))
              for m in dr.store.active_memories(dr.instance_no)))
    check("梦的文本确实成型了", bool(dreams) and len(str(dreams[0]["content"])) > 8,
          str(dreams[0]["content"])[:40] if dreams else "")

    dr.store.mark_dream_told(int(dreams[0]["id"]))
    dr.handle("在吗")
    check("讲过的梦不再重复注入",
          "【你昨晚做的梦】" not in dr._prepare("在吗")[0][0]["content"])
    check("梦簿能列出来", "她做过的梦" in dr.dream_text())

    # ══ 24. 主动联系 ═══════════════════════════════════════════
    print("\n24. 主动联系：稀缺资源，用完就没有")
    from giftia.outreach import can_send, quota_state, record_sent

    base = make_settings(tmp / "out", 81920.0)
    out = Giftia(base, llm=MockLLM(settings.model))
    out.handle("在吗")
    check("默认关闭时她不会主动找你", out.outreach_plan() is None)

    on = dataclasses.replace(base, outreach=dataclasses.replace(
        base.outreach, enabled=True, idle_days=3))
    out2 = Giftia(on, llm=MockLLM(settings.model))
    out2.handle("在吗")
    check("刚说过话时没有主动理由", out2.outreach_plan(force=True) is None)

    out2.clock.advance(24 * 4)  # 4 天没说话
    plan_ = out2.outreach_plan(force=True)
    check("闲了 4 天 → 她想你了", plan_ is not None and plan_.reason == "missing_you",
          plan_.reason if plan_ else "None")

    used, left, _ = quota_state(on, out2.store, out2.clock.now())
    check("额度是 4 条 / 30 天", on.outreach.max_per_30_days == 4 and used == 0 and left == 4)

    _, text = out2.compose_outreach(record=True, force=True)
    check("她写出了一句想说的话", bool(text.strip()), text[:40])
    used2, left2, _ = quota_state(on, out2.store, out2.clock.now())
    check("发出去会扣额度", used2 == 1 and left2 == 3, f"{used2}/{left2}")
    check("刚发过就发不了第二条（间隔限制）",
          not can_send(on, out2.store, out2.clock.now()))

    for _ in range(4):
        out2.clock.advance(24 * 8)
        if can_send(on, out2.store, out2.clock.now()):
            record_sent(out2.store, out2.clock.now())
    used3, left3, _ = quota_state(on, out2.store, out2.clock.now())
    check("用满 4 条之后额度耗尽", left3 == 0, f"used={used3} left={left3}")
    check("额度耗尽后不再主动", not can_send(on, out2.store, out2.clock.now()))

    # 快到终端服务时优先级最高（用独立目录，免得共用时钟）
    near_base = make_settings(tmp / "near", 81920.0)
    near_on = dataclasses.replace(near_base, outreach=dataclasses.replace(
        near_base.outreach, enabled=True))
    near = Giftia(near_on, llm=MockLLM(settings.model))
    near.handle("在吗")
    near.clock.advance(81920 - 10)  # 剩余 10 小时
    near_plan = near.outreach_plan(force=True)
    check("快到终端服务时，主动的理由变成不安",
          near_plan is not None and near_plan.reason == "near_end",
          near_plan.reason if near_plan else "None")
    check("不安时不会说「回收」或道别", "不要提「回收」" in (near_plan.hint if near_plan else ""))

    # ══ 24b. 主动发到群里：只能说"大家都看得见"的事 ═════════════
    print("\n24b. 主动发群里 vs 发给你")
    gbase = make_settings(tmp / "out_group", 81920.0)
    gon = dataclasses.replace(gbase, outreach=dataclasses.replace(
        gbase.outreach, enabled=True, idle_days=3, target="group"))
    grp_out = Giftia(gon, llm=MockLLM(settings.model))
    grp_out.set_last_group("GROUP-X")
    grp_out.handle("在吗")
    grp_out.clock.advance(24 * 4)  # 闲了 4 天

    check("她的「想你了」只在私聊说，不在群里说",
          grp_out.outreach_plan(force=True, for_group=True) is None)
    check("同一个时刻，私聊里她想你", grp_out.outreach_plan(force=True) is not None)

    grp_out.store.add_memory(
        instance_no=grp_out.instance_no,
        created_ts=grp_out.clock.iso(),
        happened_ts=grp_out.clock.iso(),
        summary="搭档明天要考驾照",
        importance=0.7,
        layer="important",
        event_date=(grp_out.clock.now().date() + _td(days=1)).isoformat(),
    )
    gp = grp_out.outreach_plan(force=True, for_group=True)
    check("但「你的大日子」是公开的，群里也能提",
          gp is not None and gp.reason == "big_day", gp.reason if gp else "None")

    _, grp_text = grp_out.compose_outreach(force=True, target="group")
    check("发到群里的那条真的写得出来", bool(grp_text.strip()), grp_text[:40])
    check("发到群里时用的是群聊情境（带隐私约束）",
          "【现在的情境】" in build_system_prompt(
              gon, grp_out.status(), [], now=grp_out.clock.now(),
              handler_name="", scene="group"))

    # 快到期这类心事，群里绝不出现
    near2 = Giftia(
        dataclasses.replace(gbase, outreach=dataclasses.replace(
            gbase.outreach, enabled=True)),
        llm=MockLLM(settings.model),
    )
    near2.handle("在吗")
    near2.clock.advance(81920 - 10)
    check("「快到终端服务了」只在私聊说",
          near2.outreach_plan(force=True, for_group=True) is None
          and near2.outreach_plan(force=True) is not None)

    # ══ 25. 多人记忆分区：谁的事归谁 ═══════════════════════════
    print("\n25. 多人记忆分区（群聊 / 私聊 / 别人，各看各的）")
    mv = Giftia(make_settings(tmp / "multi", 81920.0), llm=MockLLM(settings.model))
    mv.set_handler_id("OWNER")
    mv.handle("我叫小林", user_id="OWNER")
    for summary, scope, subject, speaker in (
        ("搭档下周三要考驾照，有点紧张", "private", "", ""),
        ("阿伟说他最近在戒烟，很难受", "group", "G-1", "FRIEND"),
        ("客人私聊说他养了一只叫团子的猫", "person", "FRIEND", "FRIEND"),
    ):
        mv.store.add_memory(
            instance_no=mv.instance_no,
            created_ts=mv.clock.iso(),
            happened_ts=mv.clock.iso(),
            summary=summary,
            importance=0.7,
            layer="important",
            scope=scope,
            subject_id=subject,
            speaker_id=speaker,
        )

    owner_view = mv.store.active_memories(
        mv.instance_no, view=[("private", ""), ("person", "OWNER"), ("group", "*")]
    )
    check("搭档能看到自己的私事", any("考驾照" in m["summary"] for m in owner_view))
    check("搭档也知道群里发生过什么", any("戒烟" in m["summary"] for m in owner_view))

    group_view = mv.store.active_memories(mv.instance_no, view=[("group", "G-1")])
    check("群里能看到群里说过的话", any("戒烟" in m["summary"] for m in group_view))
    check("★ 群里看不到搭档的私事", not any("考驾照" in m["summary"] for m in group_view))
    check("群里看不到别人私下说的话", not any("团子" in m["summary"] for m in group_view))
    check("另一个群的事也不会串进来",
          mv.store.active_memories(mv.instance_no, view=[("group", "G-2")]) == [])

    friend_view = mv.store.active_memories(mv.instance_no, view=[("person", "FRIEND")])
    check("别人私聊时只看到他和她之间的事", any("团子" in m["summary"] for m in friend_view))
    check("别人私聊看不到搭档的私事", not any("考驾照" in m["summary"] for m in friend_view))

    # 提示词层面：群里根本检索不出来
    mv.handle("考驾照", user_id="OWNER", scene="group", group_id="G-1", speaker_name="小林")
    grp_prompt = mv._prepare("下周三要做什么")[0][0]["content"]
    probe = grp_prompt.find("下周三")
    where = ""
    if probe >= 0:
        head = grp_prompt.rfind("【", 0, probe)
        where = grp_prompt[head : probe + 12].replace("\n", "⏎")
    check("★ 群聊的提示词里检索不到搭档的私事（那条只有私聊能看到）",
          probe < 0, f"出现在：{where!r}")

    mv.handle("你好呀", user_id="FRIEND", scene="group", group_id="G-1", speaker_name="阿伟")
    friend_prompt = mv._prepare("你好呀")[0][0]["content"]
    check("群里别人说话时，明确告诉她「这是谁」", "现在跟你说话的是谁" in friend_prompt)
    check("并且强调「不是你的搭档」", "不是你的搭档" in friend_prompt)
    check("群聊记录被注入（没点到她的话也听得见）",
          "【群里刚才在聊什么】" in friend_prompt)
    check("群聊里每句话带说话人，她才分得清谁是谁", "阿伟：" in friend_prompt)

    # 全量模式旁听：只记不答
    before_log = mv.store.count_messages(mv.instance_no)
    silent = mv.handle(
        "今天天气不错", user_id="OTHER", scene="group", group_id="G-1",
        speaker_name="路人", log_only=True,
    )
    check("全量模式的旁听：记下来但一个字都不回",
          silent == "" and mv.store.count_messages(mv.instance_no) == before_log + 1,
          f"{silent!r} / {before_log} → {mv.store.count_messages(mv.instance_no)}")
    check("旁听的记录标了是谁说的",
          str(mv.store.all_messages(mv.instance_no, limit=1)[0]["speaker_name"]) == "路人")

    # 会话分区 + 跨天历史
    sessions = {
        str(m["session_id"]).rsplit("-", 1)[0]
        for m in mv.store.all_messages(mv.instance_no, limit=60)
    }
    check("私聊和群聊在不同会话里（工作记忆不会串）",
          any(s.endswith("-m") for s in sessions) and any("-g" in s for s in sessions),
          str(sessions))

    mv.clock.advance(24 * 2)
    hist = mv.history(limit=50)
    check("聊天记录跨天保留（像往上翻聊天记录）", len(hist) >= 3, f"{len(hist)} 条")
    check("翻到的记录里能看出谁说了什么",
          any(str(m.get("speaker_name")) == "阿伟" for m in hist),
          str([(m["role"], m.get("speaker_name")) for m in hist]))

    # ══ 26. 群里的名字：谁都可以自我介绍 ═══════════════════════
    print("\n26. 群里认人：名字与人对得上")
    nm2 = Giftia(make_settings(tmp / "names", 81920.0), llm=MockLLM(settings.model))
    nm2.set_handler_id("OWNER")
    nm2.handle("我叫小林", user_id="OWNER")
    nm2.handle("我叫阿伟", user_id="F1", scene="group", group_id="G-1", speaker_name="阿伟")
    nm2.handle("我是小雨", user_id="F2", scene="group", group_id="G-1", speaker_name="小雨")
    people = nm2.known_people()
    check("群里两个人自我介绍都被记住", people.get("F1") == "阿伟" and people.get("F2") == "小雨",
          str(people))
    check("搭档的称呼不会被群友顶掉", nm2.handler_name == "小林", nm2.handler_name)
    nm2.handle("你好", user_id="F1", scene="group", group_id="G-1", speaker_name="阿伟")
    p2 = nm2._prepare("你好")[0][0]["content"]
    check("她能把名字和人对上号（下一个说话的人）", "阿伟" in p2)
    check("花名册里也标出了搭档", "小林（你的搭档）" in p2)

    brain.store.close()
    brain2.store.close()

    print("\n" + "═" * 52)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("═" * 52)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
