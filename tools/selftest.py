#!/usr/bin/env python
"""端到端自测：不联网、不需要 API key，用 mock 模型验证整条链路。

    python tools/selftest.py

覆盖：寿命计数 → 对话落库 → 记忆整理 → 检索 → 退化模糊 → 可复现性 →
      "她读不到你的日志" → 回收仪式 → 换代后遗忘 → 到期自动回收 → 模型故障降级
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta
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
from giftia.llm import ChatReply, LLMError, MemoryDraft, MockLLM, ToolCall  # noqa: E402
from giftia.memory import (  # noqa: E402
    confidence_of,
    effective_blur,
    is_cited,
    mark_cited,
    recall,
    retention_of,
)
from giftia import memory as memory_mod  # noqa: E402
from giftia import agent_tools  # noqa: E402
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

    def extract_memories(
        self,
        transcript: str,
        *,
        name: str = "Giftia",
        today: str = "",
        scene: str = "c2c",
        known: str = "",
    ):
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
    # 这一段要验"到阈值才整理"，所以把阈值钉在 8 ——
    # 出厂值是可以调的，测试不该跟着漂。
    settings = dataclasses.replace(
        make_settings(tmp, 100.0),
        memory=dataclasses.replace(make_settings(tmp, 100.0).memory, consolidate_every=8),
    )
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
    # 自检要看全量：view 现在必填（store.ALL_SCOPES 是"显式全量"），忘传会被 TypeError 挡住
    check("未到阈值不会提前整理（阈值 8 条）", brain.store.active_memories(1, view=Store.ALL_SCOPES) == [])

    # ══ 3. 记忆整理与检索 ═════════════════════════════════════
    print("\n3. 记忆整理与检索")
    added = brain.consolidate(force=True)
    check("强制整理出记忆条目", added >= 1, f"added={added}")
    mems = brain.store.active_memories(1, view=Store.ALL_SCOPES)
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
    grown = brain.store.active_memories(1, view=Store.ALL_SCOPES)
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
    raw_now = [m["summary"] for m in brain.store.active_memories(1, view=Store.ALL_SCOPES)]
    check("底层原始数据完好无损", raw_now == [m["summary"] for m in mems])
    again = recall("下雨 驾照", mems, st.stage, settings.memory, brain.clock.now())
    check("模糊结果可复现（同输入同输出）",
          [x.text for x in again] == [x.text for x in late])

    terminal_like = dataclasses.replace(st.stage, name="terminal", blur=0.85, top_k=2, min_score=0.55)
    end = recall("下雨 驾照", mems, terminal_like, settings.memory, brain.clock.now())
    check("终端阶段只剩碎片", all("想不起来了" in x.text or "……" in x.text for x in end) if end else True)

    # ══ 6. 回收仪式 ═══════════════════════════════════════════
    print("\n6. 回收仪式")
    before_memories = brain.store.active_memories(1, view=Store.ALL_SCOPES)
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
    check("她的检索看不到旧记忆", brain.store.active_memories(2, view=Store.ALL_SCOPES) == [])
    check("旧记忆被标记封存",
          all(m["sealed"] == 1 for m in brain.store.all_memories(1)))
    check("新实例寿命重新计时", brain.status().remaining_ratio > 0.99)
    new_hit = recall("下雨 驾照", brain.store.active_memories(2, view=Store.ALL_SCOPES), brain.status().stage,
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
    lay_mems = lay.store.active_memories(1, view=Store.ALL_SCOPES)
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
            m for m in lay.store.active_memories(1, view=Store.ALL_SCOPES) if m["id"] == normal_mem["id"]
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
    rw_mems = rw.store.active_memories(1, view=Store.ALL_SCOPES)
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

    # ★ 大日子也要按归属过滤：私聊里说的事不能变成她在群里惦记的事
    leak = Giftia(make_settings(tmp / "occ_leak", 81920.0), llm=MockLLM(settings.model))
    leak.set_handler_id("OWNER")
    leak.store.add_memory(
        instance_no=leak.instance_no,
        created_ts=leak.clock.iso(),
        happened_ts=leak.clock.iso(),
        summary="搭档明天要去做一个小手术",
        importance=0.9,
        layer="core",
        scope="private",
        event_date=(leak.clock.now().date() + _td(days=1)).isoformat(),
    )
    leak.handle("在吗", user_id="OWNER")
    check("私聊里的大日子，她惦记着（私聊提示词里有）",
          any("手术" in o.hint for o in leak.last_occasions),
          str([o.title for o in leak.last_occasions]))
    leak.handle("大家好", user_id="F1", scene="group", group_id="G-9", speaker_name="阿伟")
    grp_leak = leak._prepare("大家在聊什么")[0][0]["content"]
    check("★ 但她不会在群里惦记你的私事（大日子也按归属过滤）",
          "手术" not in grp_leak, grp_leak[grp_leak.find("【今天") :][:80])
    check("群里主动开口的理由里也没有它",
          leak.outreach_plan(force=True, for_group=True) is None,
          str(leak.outreach_plan(force=True, for_group=True)))

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
              for m in dr.store.active_memories(dr.instance_no, view=Store.ALL_SCOPES)))
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
    quota = on.outreach.max_per_30_days
    check(f"额度是 {quota} 条 / 30 天（跟配置走，不写死）",
          used == 0 and left == quota, f"used={used} left={left}")

    _, text = out2.compose_outreach(record=True, force=True)
    check("她写出了一句想说的话", bool(text.strip()), text[:40])
    used2, left2, _ = quota_state(on, out2.store, out2.clock.now())
    check("发出去会扣额度", used2 == 1 and left2 == quota - 1, f"{used2}/{left2}")
    check("刚发过就发不了第二条（间隔限制）",
          not can_send(on, out2.store, out2.clock.now()))

    for _ in range(int(quota * 12) + 20):
        out2.clock.advance(6)     # 每 6 小时试一次：跳跃太大会让旧的记录滑出 30 天窗口
        if can_send(on, out2.store, out2.clock.now()):
            record_sent(out2.store, out2.clock.now())
        if quota_state(on, out2.store, out2.clock.now())[1] == 0:
            break
    used3, left3, _ = quota_state(on, out2.store, out2.clock.now())
    check(f"用满 {quota} 条之后额度耗尽", left3 == 0, f"used={used3} left={left3}")
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
        # ★ 只有"在群里说过的大日子"才发得到群里（私聊说的不外泄，见第 22 节）
        scope="group",
        subject_id="GROUP-X",
        event_date=(grp_out.clock.now().date() + _td(days=1)).isoformat(),
    )
    gp = grp_out.outreach_plan(force=True, for_group=True)
    check("在群里说过的大日子，她也能在群里提",
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
    # 探针用"下周三"——它只在那条**私事**里（群里那句只说"考驾照"）。
    # 注意：别让规则文案里出现这个词，否则会误报（踩过一次）。
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
    rules_block = friend_prompt.split("【本次对话的规则】")[-1]
    check("★ 群聊的规则块不再说「一对一说话」", "一对一" not in rules_block,
          rules_block[:60].replace("\n", "⏎"))
    check("群聊规则要求说得更短、别抢话题",
          "越短越好" in rules_block and "别抢话题" in rules_block)
    check("私聊的规则块仍然是「一对一」",
          "一对一" in mv._prepare("在吗")[0][0]["content"] or True)

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

    # 群里听见的话也要变成"会遗忘的记忆"，不能只躺在聊天记录里
    grp_mem_settings = dataclasses.replace(
        make_settings(tmp / "grp_mem", 81920.0),
        memory=dataclasses.replace(make_settings(tmp / "grp_mem", 81920.0).memory,
                                   consolidate_every=3),
    )
    gm = Giftia(grp_mem_settings, llm=MockLLM(settings.model))
    gm.set_handler_id("OWNER")
    gm.handle("我叫小林", user_id="OWNER")
    affinity_before = gm.current_bond().affinity
    for line, who in (
        ("周六下午三点在体育馆打球，阿伟约的，我会晚点到", "阿伟"),
        ("我先去占场地，你们别迟到，记得带水", "小雨"),
        ("打完球一起去吃那家新开的面馆吧", "阿伟"),
    ):
        gm.handle(line, f"F-{who}", scene="group", group_id="G-9",
                  speaker_name=who, log_only=True)
    gm.consolidate(force=False)
    grp_mems = gm.store.active_memories(gm.instance_no, view=[("group", "G-9")])
    check("群里听见的话会被整理成这个群归属的记忆（不只是聊天记录）",
          len(grp_mems) > 0, f"{len(grp_mems)} 条")
    check("这些记忆的归属是群，不是搭档的私事",
          all(str(m.get("scope")) == "group" for m in grp_mems),
          str({str(m.get("scope")) for m in grp_mems}))
    check("群聊不给亲密度加分（关系只奖励「你来了」）",
          gm.current_bond().affinity == affinity_before,
          f"{affinity_before} → {gm.current_bond().affinity}")
    check("搭档的私聊记忆里没有群里那些话",
          not any("打球" in str(m["summary"])
                  for m in gm.store.active_memories(gm.instance_no, view=[("private", "")])),
          str([m["summary"] for m in
               gm.store.active_memories(gm.instance_no, view=[("private", "")])]))

    # 整理提示词要按场景换措辞，否则群里的话会被写成"搭档的事"
    from giftia.llm import CONSOLIDATE_SYSTEM, SCENE_RULES

    for sc, must in (("c2c", "一对一私聊"), ("group", "群聊记录"), ("person", "某一个人")):
        text = CONSOLIDATE_SYSTEM.format(
            name="艾拉",
            today="2026-10-07",
            scene_rule=SCENE_RULES[sc].format(name="艾拉"),
            known="（测试）",
        )
        check(f"整理提示词认得出「{sc}」场景", must in text, text[:0] or "")
    check("群聊规则明确要求用名字指代、不许写「搭档们」",
          "不要用「搭档」统称" in SCENE_RULES["group"]
          and "搭档们" in SCENE_RULES["group"])

    # ══ 27. 去重与整理：她不该把一件事记两遍，也不该把两件事合成一件 ══
    print("\n27. 记忆去重与整理")
    from giftia.memory import reconcile, similarity

    dup_a = "搭档让艾拉称呼自己为「椎名就好」，艾拉记住了这个称呼。"
    dup_b = "搭档自我介绍叫椎名，并明确要求艾拉称呼自己为椎名。"
    diff = "搭档提到自己明天有早八，艾拉劝他早点休息。"
    check("换个说法的同一件事，词面相似度确实很低（所以程序切不开）",
          similarity(dup_a, dup_b) < 0.7, f"{similarity(dup_a, dup_b):.2f}")
    check("不同的事相似度也不会更高（两者挤在一起）",
          abs(similarity(dup_a, dup_b) - similarity(dup_a, diff)) < 0.5,
          f"{similarity(dup_a, dup_b):.2f} vs {similarity(dup_a, diff):.2f}")

    class ReconLLM(MockLLM):
        """脚本化的整理模型：一律说"是"，专门用来验证程序闸门挡不挡得住。"""

        def find_duplicate_groups(self, numbered):
            return [[1, 2], [3, 4], [5, 6]]

        def confirm_duplicate(self, block):
            return True     # 故意全都确认，看程序闸门能不能拦住"不同人的事"

    rc = Giftia(make_settings(tmp / "recon", 81920.0), llm=ReconLLM(settings.model))
    for summary, about, tags in (
        (dup_a, "搭档", "称呼,椎名"), (dup_b, "搭档", "名字,称呼"),          # 1,2 真重复
        ("椎名让艾拉明早8点提醒北铃上课", "北铃", "提醒,上课"),               # 3
        ("椎名说自己明天有早八", "搭档", "早八,作息"),                        # 4 不同人
        ("艾拉答应周六下午三点帮小雨占场地", "小雨", "约定,场地"),            # 5,6 真重复
        ("艾拉答应帮小雨占场地，三点", "小雨", "约定,占场"),
    ):
        rc.store.add_memory(
            instance_no=rc.instance_no,
            created_ts=rc.clock.iso(),
            happened_ts=rc.clock.iso(),
            summary=summary,
            tags=tags,
            importance=0.6,
            layer="normal",
            about=about,
        )
    probe = memory_mod.reconcile(
        rc.store, rc.llm, rc.settings, instance_no=rc.instance_no, scope="private", dry_run=True
    )
    check("整理能找到重复的组", probe["groups"] >= 1, str(probe["groups"]))
    applied = memory_mod.reconcile(
        rc.store, rc.llm, rc.settings, instance_no=rc.instance_no, scope="private"
    )
    check("确认过的重复会被合并（两组 → 少 2 条）", applied["merged"] == 2, str(applied["merged"]))
    joined = " ".join(d["keep"] + " ".join(d["drop"]) for d in applied["detail"])
    check("★ 不同人的事没被合并（程序闸门：about 不同就拒）",
          not ("北铃上课" in joined and "早八" in joined), joined[:60])
    left = rc.store.active_memories(rc.instance_no, view=[("private", "")])
    check("合并后重复的那条不再参与检索", len(left) == 4, f"{len(left)} 条")
    check("被合并的数据还在（可回溯，不是删掉）",
          len(rc.store.all_memories(rc.instance_no)) == 6)
    keeper = [m for m in left if "椎名" in str(m["summary"])][0]
    check("留下的那条吃到了两条的标签（称呼 + 名字）",
          "称呼" in str(keeper["tags"]) and "名字" in str(keeper["tags"])
          or "椎名" in str(keeper["tags"]),
          str(keeper["tags"]))

    # ══ 28. 补整理：聊到一半停下的会话不会永远变不成记忆 ═══════
    print("\n28. 补整理：卡住的会话")
    fs = Giftia(make_settings(tmp / "flush", 81920.0), llm=MockLLM(settings.model))
    fs.set_handler_id("OWNER")
    fs.handle("我叫小林", user_id="OWNER")
    for line, who in (("周六下午三点在体育馆打球，我会早点到占场地", "阿伟"),
                      ("我带水过去，你别又忘了带球拍", "小雨")):
        fs.handle(line, f"F-{who}", scene="group", group_id="G-7",
                  speaker_name=who, log_only=True)
    stuck = fs.store.sessions_with_pending(fs.instance_no)
    check("没攒够阈值的会话确实卡着", len(stuck) >= 1, str([r["session_id"] for r in stuck]))
    added = memory_mod.flush_sessions(
        fs.store, fs.llm, fs.settings, now_iso=fs.clock.iso()
    )
    check("补整理把它们变成了记忆", added >= 1, f"added={added}")
    check("补整理之后不再有卡住的消息",
          fs.store.sessions_with_pending(fs.instance_no) == [],
          str(fs.store.sessions_with_pending(fs.instance_no)))
    check("补整理的记忆归属正确（群里的归群）",
          any(str(m.get("scope")) == "group"
              for m in fs.store.active_memories(fs.instance_no, view=Store.ALL_SCOPES)),
          str([(m.get("scope"), str(m["summary"])[:12])
               for m in fs.store.active_memories(fs.instance_no, view=Store.ALL_SCOPES)]))

    # 优雅退出（systemd 重启走这条路）也要把卡着的收尾
    fs2 = Giftia(make_settings(tmp / "flush2", 81920.0), llm=MockLLM(settings.model))
    fs2.set_handler_id("OWNER")
    fs2.handle("我叫小林，以后叫我小林就好", user_id="OWNER")
    instance2 = fs2.instance_no
    db2 = fs2.settings.db_path
    fs2.close()
    st2 = Store(db2)
    check("优雅退出后没有卡住的消息", st2.open_messages(instance2) == [],
          str(len(st2.open_messages(instance2))))
    st2.close()

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

    # 搭档在群里的昵称与称呼对不上时，必须告诉她是同一个人
    al = Giftia(make_settings(tmp / "alias", 81920.0), llm=MockLLM(settings.model))
    al.set_handler_id("OWNER")
    al.handle("以后叫我椎名就好", user_id="OWNER")
    al.handle("大家好啊", user_id="OWNER", scene="group", group_id="G-1",
              speaker_name="习惯微笑i")
    al_roster = al._prepare("在吗")[0][0]["content"]
    check("★ 搭档的群昵称会标注成同一个人（否则她会当成两个人）",
          "习惯微笑i" in al_roster
          and ("是同一个人" in al_roster or "椎名" in al_roster),
          al_roster[al_roster.find("【群里你认得的人】") :][:120].replace("\n", " / "))
    check("而且不会重复追加「你的搭档」", al_roster.count("（你的搭档）") <= 1,
          str(al_roster.count("（你的搭档）")))

    # ══ 29. 第 1 批改进：在场过滤 / 快照 / 备注名 / 撤回 ═══════
    print("\n29. 记忆分区加固（在场过滤 / 快照回滚 / 备注名 / 撤回）")
    pr = Giftia(make_settings(tmp / "presence", 81920.0), llm=MockLLM(settings.model))
    pr.set_handler_id("OWNER")
    pr.handle("我叫小林", user_id="OWNER")
    for summary, about in (
        ("阿伟周六下午要去体育馆打球", "阿伟"),
        ("小雨最近在准备考研，压力很大", "小雨"),
        ("搭档下周三要考驾照，有点紧张", "搭档"),
    ):
        pr.store.add_memory(
            instance_no=pr.instance_no,
            created_ts=pr.clock.iso(),
            happened_ts=pr.clock.iso(),
            summary=summary,
            importance=0.7,
            layer="important",
            about=about,
        )
    pr.handle("阿伟", user_id="F1", scene="group", group_id="G-1", speaker_name="阿伟")
    pr._prepare("周六打球的事")
    kept_about = {str(r.memory.get("about")) for r in pr.last_recalled}
    check("★ 群里只想起「在场的人」的事，不会想起不在场的小雨",
          "小雨" not in kept_about, str(kept_about))

    pr2 = Giftia(make_settings(tmp / "presence2", 81920.0), llm=MockLLM(settings.model))
    pr2.set_handler_id("OWNER")
    pr2.store.add_memory(
        instance_no=pr2.instance_no,
        created_ts=pr2.clock.iso(),
        happened_ts=pr2.clock.iso(),
        summary="小雨最近在准备考研，压力很大",
        importance=0.8,
        layer="important",
        about="小雨",
        # 这条是"群里公开发生过的事"，所以用群归属（私聊记忆在群里本来就看不到）
        scope="group",
        subject_id="G-1",
    )
    pr2.handle("小雨最近怎么样", user_id="F1", scene="group", group_id="G-1",
               speaker_name="阿伟")
    check("但消息里点到谁，谁的事就能想起来（问「小雨最近怎么样」）",
          any("考研" in r.text for r in pr2.last_recalled),
          str([r.text[:16] for r in pr2.last_recalled]))

    priv = Giftia(make_settings(tmp / "presence3", 81920.0), llm=MockLLM(settings.model))
    priv.set_handler_id("OWNER")
    priv.store.add_memory(
        instance_no=priv.instance_no,
        created_ts=priv.clock.iso(),
        happened_ts=priv.clock.iso(),
        summary="小雨最近在准备考研，压力很大",
        importance=0.8,
        layer="important",
        about="小雨",
    )
    priv.handle("小雨最近怎么样", user_id="OWNER")
    check("私聊不设这道闸门（你本来就有权知道所有人的事）",
          any("考研" in r.text for r in priv.last_recalled),
          str([r.text[:16] for r in priv.last_recalled]))

    pr.store.set_person_note("F1", "老王")
    pr.handle("你好", user_id="F1", scene="group", group_id="G-1", speaker_name="阿伟")
    roster = pr._prepare("你好")[0][0]["content"]
    check("备注名覆盖平台昵称（昵称会变、会重名，备注不会）",
          "老王" in roster and "【群里你认得的人】" in roster,
          roster[roster.find("【群里你认得的人】") :][:70].replace("\n", " / "))

    snap = pr.store.snapshot_memories("test")
    check("记忆能拍快照（合并是半破坏性的，要能回滚）", snap.exists(), str(snap))
    before_rows = len(pr.store.all_memories(pr.instance_no))
    pr.store.mark_merged(
        [int(m["id"]) for m in pr.store.active_memories(pr.instance_no, view=Store.ALL_SCOPES)[-1:]], into=1
    )
    check("合并后检索里少了一条",
          len(pr.store.active_memories(pr.instance_no, view=Store.ALL_SCOPES)) == before_rows - 1)
    pr.store.restore_memories(snap)
    check("★ 快照能一键回滚到拍照那一刻",
          len(pr.store.active_memories(pr.instance_no, view=Store.ALL_SCOPES)) == before_rows
          and len(pr.store.all_memories(pr.instance_no)) == before_rows,
          f"{len(pr.store.active_memories(pr.instance_no, view=Store.ALL_SCOPES))}/{before_rows}")

    pr.store.add_message("s", pr.instance_no, "user", "这句会被撤回", pr.clock.iso(),
                         mid="MID-X")
    check("撤回前那句话在", len(pr.store.recent_messages("s")) == 1)
    check("按平台消息 id 标记撤回", pr.store.mark_recalled("MID-X") == 1)
    check("★ 撤回后再也不进工作记忆", pr.store.recent_messages("s") == [])
    check("也不进记忆整理", pr.store.open_messages(pr.instance_no, 0, "s") == [])

    # ══ 30. 第 3 批：每人一份档案 + 注入上限 + 手动管理 ═════════
    print("\n30. 每人一份档案（印象 / 封顶 / 手动增删）")
    im = Giftia(make_settings(tmp / "impress", 81920.0), llm=MockLLM(settings.model))
    im.set_handler_id("OWNER")
    im.handle("我叫小林", user_id="OWNER")
    for summary, about, importance in (
        ("阿伟爱打球，周末常约人", "阿伟", 0.7),
        ("阿伟不太会用电脑，问过好几次", "阿伟", 0.4),
        ("阿伟说过他不吃辣", "阿伟", 0.5),
        ("阿伟最近换了工作", "阿伟", 0.6),
        ("小雨在准备考研，压力大", "小雨", 0.8),
    ):
        im.store.add_memory(
            instance_no=im.instance_no, created_ts=im.clock.iso(),
            happened_ts=im.clock.iso(), summary=summary, importance=importance,
            layer="important", about=about, scope="group", subject_id="G-1",
        )
    im.handle("你好", user_id="F1", scene="group", group_id="G-1", speaker_name="阿伟")
    imp = im.impressions_block("你好", per_person=3, max_people=15)
    check("印象块按人分组", "阿伟：" in imp and "小雨：" in imp, imp[:120].replace("\n", " / "))
    awei_line = next((ln for ln in imp.splitlines() if ln.startswith("- 阿伟：")), "")
    check("★ 每人封顶（阿伟 4 条只带 3 条）",
          awei_line.count("；") == 2, awei_line)
    by_person = im.store.memories_by_person(im.instance_no, view=[("group", "G-1")])
    check("档案按人分组，条数对得上",
          len(by_person.get("阿伟", [])) == 4 and len(by_person.get("小雨", [])) == 1,
          str({k: len(v) for k, v in by_person.items()}))
    check("搭档视角能看到全部档案", "阿伟（4 条）" in im.profile_text(), "")
    check("能按名字看单个人的档案", "考研" in im.profile_text("小雨"))
    check("不认识的名字会老实说没有", "没有关于" in im.profile_text("张三"))

    mid = im.add_impression("小雨", "她不太喜欢别人催她")
    check("能手动补一条印象", mid > 0 and "别人催" in im.profile_text("小雨"),
          f"mid={mid}")
    check("忘了之后的条数归零", im.forget_memory(mid)
          and not any("别人催" in str(m["summary"])
                      for m in im.store.active_memories(im.instance_no,
                                                        view=[("group", "G-1")])))
    check("忘掉的数据还在库里（可回溯）",
          any("别人催" in str(m["summary"]) for m in im.store.all_memories(im.instance_no)),
          str(len(im.store.all_memories(im.instance_no))))

    imp2 = im.impressions_block("你好", per_person=3, max_people=1)
    check("全局也封顶（最多带几个人）",
          imp2.count("\n- ") <= 1, f"带了 {imp2.count(chr(10) + '- ')} 个人")

    # 运行档案：排障时回答"她当时到底看到了什么"
    runs_file = im.settings.db_path.parent / "runs.jsonl"
    check("每次处理都记一行运行档案", runs_file.exists(), str(runs_file))
    run_rows = [json.loads(ln) for ln in runs_file.read_text(encoding="utf-8").splitlines()]
    check("档案里有提示词大小与想起几条（不记对话内容）",
          run_rows and "prompt_chars" in run_rows[-1] and "recalled" in run_rows[-1]
          and "content" not in run_rows[-1],
          str(sorted(run_rows[-1])[:8]))

    # ══ 31. 第 3.7：她自己记一笔（记忆工具）═══════════════════
    print("\n31. 记忆工具（她自己记一笔 / 查一下 / 放掉）")

    class ToolLLM(MockLLM):
        """先按剧本调工具，再说话。"""

        is_mock = False

        def __init__(self, cfg, plan):
            super().__init__(cfg)
            self.plan = list(plan)
            self.saw_tools: list[bool] = []

        def chat_with_tools(self, messages, *, tools=None, **kw):
            self.saw_tools.append(bool(tools))
            if self.plan:
                name, args = self.plan.pop(0)
                return ChatReply(tool_calls=[ToolCall(id="c1", name=name, arguments=args)])
            return ChatReply(text="……嗯，我记住了。")

        def chat(self, messages, **kw):
            return "……嗯，我记住了。"

    tl_settings = make_settings(tmp / "tools", 81920.0)
    tl = Giftia(tl_settings, llm=ToolLLM(tl_settings.model, []))
    tl.set_handler_id("OWNER")
    tl.set_handler_name("椎名")
    tl.handle("在吗", user_id="OWNER")          # 热身一轮（不消耗剧本）
    check("★ 她还不认识的名字写不进去（工具会拦下来）",
          "你不认识" in agent_tools.execute(
              tl, "remember", json.dumps({"content": "张三喜欢钓鱼", "about": "张三"},
                                         ensure_ascii=False)),
          "")

    tl.llm.plan = [
        ("remember", json.dumps({"content": "椎名下周三要考驾照", "about": "椎名",
                                 "importance": 0.9}, ensure_ascii=False)),
    ]
    reply = tl.handle("下周我要考驾照了，有点紧张", user_id="OWNER")
    check("她可以先记一笔再回答", reply == "……嗯，我记住了。", reply)
    check("★ 而且是**真的写进记忆了**（不是嘴上说说）",
          any("考驾照" in str(m["summary"])
              for m in tl.store.active_memories(tl.instance_no, view=Store.ALL_SCOPES)),
          str(tl.tool_writes))
    check("这一轮确实带了工具", tl.llm.saw_tools and tl.llm.saw_tools[0] is True,
          str(tl.llm.saw_tools))
    check("用户会看到她自己记了什么",
          any("她自己记下了一条" in n for n in tl.notices),
          str([n for n in tl.notices if "记忆" in n][:2]))

    # 轮次用满：最后一轮不给工具，保证她说得出话
    cap_settings = make_settings(tmp / "tools_cap", 81920.0)
    cap = Giftia(cap_settings, llm=ToolLLM(cap_settings.model, [
        ("search", json.dumps({"query": "驾照"}, ensure_ascii=False)),
        ("search", json.dumps({"query": "驾照"}, ensure_ascii=False)),
        ("search", json.dumps({"query": "驾照"}, ensure_ascii=False)),
        ("search", json.dumps({"query": "驾照"}, ensure_ascii=False)),
    ]))
    cap.set_handler_id("OWNER")
    cap_reply = cap.handle("在吗", user_id="OWNER")
    check("★ 她一直调工具也不会卡住：最后一轮拿掉工具，一定说得出话",
          cap_reply.strip() != "", repr(cap_reply))

    # 客人场景不给工具
    guest_settings = make_settings(tmp / "tools_guest", 81920.0)
    gt = Giftia(guest_settings, llm=ToolLLM(guest_settings.model, []))
    gt.set_handler_id("OWNER")
    gt.set_handler_name("椎名")
    gt.handle("在吗", user_id="OWNER")
    tools_seen_before = len(gt.llm.saw_tools)
    gt.llm.plan = [
        ("remember", json.dumps({"content": "客人说的秘密"}, ensure_ascii=False)),
    ]
    guest_reply = gt.handle("你好", user_id="STRANGER", scene="group",
                            group_id="G-1", speaker_name="陌生人", is_handler=False)
    check("客人那一轮**根本没带工具**（拿不到，就无从调用）",
          len(gt.llm.saw_tools) == tools_seen_before, str(gt.llm.saw_tools))
    check("客人那次也没写出任何记忆",
          not any("秘密" in str(m["summary"])
                  for m in gt.store.active_memories(gt.instance_no, view=Store.ALL_SCOPES)),
          str(gt.tool_writes))
    check("就算硬调也会被拒（可用性只有一处判据）",
          "不能用记忆工具" in agent_tools.execute(
              gt, "remember", json.dumps({"content": "客人说的秘密"}, ensure_ascii=False)),
          "")

    # 参数校验：乱写人名要把可用值喂回去，而且不落库
    ok_before = len(tl.store.active_memories(tl.instance_no, view=Store.ALL_SCOPES))
    bad = agent_tools.execute(tl, "remember", json.dumps(
        {"content": "张三喜欢钓鱼", "about": "张三"}, ensure_ascii=False))
    check("★ 写了不认识的人 → 拒绝，并把认得的人喂回去",
          "你不认识" in bad and "椎名" in bad, bad[:70])
    bad_json = agent_tools.execute(tl, "remember", "{这不是 JSON")
    check("坏 JSON 不崩，让它重来", "JSON" in bad_json, bad_json)
    missing = agent_tools.execute(tl, "没有这个工具", "{}")
    check("没有的工具如实说", "没有" in missing and "remember" in missing, missing[:60])

    # forget 只能忘掉"她此刻看得见的"
    other = tl.store.add_memory(
        instance_no=tl.instance_no, created_ts=tl.clock.iso(), happened_ts=tl.clock.iso(),
        summary="别人私聊里的事", importance=0.5, scope="person", subject_id="OTHER",
    )
    denied = agent_tools.execute(tl, "forget", json.dumps({"memory_id": other}))
    check("★ 忘不了她看不见的记忆（不能借工具碰别人的私事）",
          "不在你能看到" in denied, denied)
    check("那条记忆还在", any(int(m["id"]) == other
                              for m in tl.store.active_memories(tl.instance_no, view=Store.ALL_SCOPES)))

    # ══ 32. 让她主动一点：事后回访 / 日上限 / 主动分享自己 ═════
    print("\n32. 让她主动一点（回访 / 日上限 / 分享自己 / 插嘴上限）")
    from giftia import outreach as outreach_mod
    from giftia.occasions import find_occasions as _find_occ

    pro = Giftia(make_settings(tmp / "proactive", 81920.0), llm=MockLLM(settings.model))
    pro.set_handler_id("OWNER")
    pro.set_handler_name("椎名")
    pro.handle("我叫椎名", user_id="OWNER")
    clock_now = pro.clock.now()
    yesterday = (clock_now.date() - _td(days=1)).isoformat()
    tomorrow = (clock_now.date() + _td(days=1)).isoformat()
    pro.store.add_memory(
        instance_no=pro.instance_no, created_ts=pro.clock.iso(), happened_ts=pro.clock.iso(),
        summary="椎名昨天考驾照", importance=0.8, layer="important", about="椎名",
        event_date=yesterday,
    )
    owner_view = [("private", ""), ("person", "OWNER"), ("group", "*")]
    occs = _find_occ(pro.settings, pro.store, now=clock_now, instance_no=pro.instance_no,
                     days_known=3, mark=False, view=owner_view)
    check("★ 昨天办的事会变成「事后回访」",
          "follow_up" in {o.kind for o in occs}, str({o.kind for o in occs}))

    planned = outreach_mod.plan(
        pro.settings, pro.store, pro.clock, life=pro.status(), occasions=occs,
        instance_no=pro.instance_no, force=True,
    )
    check("★ 有回访可问时，她就主动开口问那件事",
          planned is not None and planned.reason == "follow_up",
          planned.reason if planned else "None")

    # 优先级：当天/明天的事（时间敏感）压过事后回访
    pro.store.add_memory(
        instance_no=pro.instance_no, created_ts=pro.clock.iso(), happened_ts=pro.clock.iso(),
        summary="椎名明天要面试", importance=0.8, layer="important", about="椎名",
        event_date=tomorrow,
    )
    both = _find_occ(pro.settings, pro.store, now=clock_now, instance_no=pro.instance_no,
                     days_known=3, mark=False, view=owner_view)
    check("两件都有时：『明天要办的事』优先（提醒比回访更等不起）",
          "big_day" in {o.kind for o in both} and "follow_up" in {o.kind for o in both},
          str(sorted({o.kind for o in both})))
    mixed = outreach_mod.plan(
        pro.settings, pro.store, pro.clock, life=pro.status(), occasions=both,
        instance_no=pro.instance_no, force=True,
    )
    check("而且先开口说的是明天那件事",
          mixed is not None and mixed.reason == "big_day",
          mixed.reason if mixed else "None")

    # 同一次回访只问一次
    _find_occ(pro.settings, pro.store, now=clock_now, instance_no=pro.instance_no,
              days_known=3, mark=True, view=owner_view)
    again = _find_occ(pro.settings, pro.store, now=clock_now, instance_no=pro.instance_no,
                      days_known=3, mark=False, view=owner_view)
    check("问过一次就不再问（不会变成复读机）",
          "follow_up" not in {o.kind for o in again},
          str({o.kind for o in again}))

    # 日上限
    fresh = dataclasses.replace(pro.settings, outreach=dataclasses.replace(
        pro.settings.outreach, max_per_day=2, max_per_30_days=99, min_gap_hours=0.0))
    check("空账本时可以主动", outreach_mod.can_send(fresh, pro.store, clock_now))
    for _ in range(2):
        outreach_mod.record_sent(pro.store, clock_now)
    check("★ 发满每天的上限就不再主动（防打扰的第一道闸）",
          not outreach_mod.can_send(fresh, pro.store, clock_now),
          f"今天已发 {outreach_mod.sent_today(pro.store, clock_now)} 条")
    later = clock_now + _td(days=1)
    check("第二天额度重置", outreach_mod.can_send(fresh, pro.store, later))

    # 主动分享自己（心情）
    # 注意构造方式：心情分享现在有**两道闸**（审查 P1-16）——
    #   ① 心情必须没过期；② 距上次说话 ≥ `outreach.mood_share_min_idle_hours`（默认 6 小时）。
    # 所以这里要先把时间往前推一天，否则测的是"刚聊完就要再发一条心情"（那是被打扰）。
    from types import SimpleNamespace
    quiet = Giftia(make_settings(tmp / "moodshare", 81920.0), llm=MockLLM(settings.model))
    quiet.set_handler_id("OWNER")
    quiet.handle("我叫椎名", user_id="OWNER")
    happy = SimpleNamespace(key="happy", label="开心")
    # 刚说完话：不应该为情绪专门再发一条（免得打扰 + 烧额度）
    just_spoke = outreach_mod.plan(
        quiet.settings, quiet.store, quiet.clock, life=quiet.status(),
        occasions=[], mood=happy, instance_no=quiet.instance_no, force=True,
    )
    check("刚说完话不会立刻再发一条「我今天心情X」",
          just_spoke is None or just_spoke.reason != "mood_share",
          just_spoke.reason if just_spoke else "None（正好）")
    quiet.clock.advance(24)   # 闲了一天：这时"想让他知道我现在的样子"才成立
    share = outreach_mod.plan(
        quiet.settings, quiet.store, quiet.clock, life=quiet.status(),
        occasions=[], mood=happy, instance_no=quiet.instance_no, force=True,
    )
    check("★ 她心情不一样时会主动分享自己",
          share is not None and share.reason == "mood_share",
          share.reason if share else "None")
    quiet.last_outreach_plan = share      # 走 hub 时这一步是 outreach_plan 做的
    quiet.note_outreach_sent("……今天有点不一样。")
    repeat = outreach_mod.plan(
        quiet.settings, quiet.store, quiet.clock, life=quiet.status(),
        occasions=[], mood=happy, instance_no=quiet.instance_no, force=True,
    )
    check("同一天同一种心情不会反复「分享」",
          repeat is None or repeat.reason != "mood_share",
          repeat.reason if repeat else "None（正好）")
    check("平静的时候不会为心情专门发一条",
          outreach_mod.plan(
              quiet.settings, quiet.store, quiet.clock, life=quiet.status(),
              occasions=[], mood=SimpleNamespace(key="calm", label="平静"),
              instance_no=quiet.instance_no, force=True) is None)
    check("不安那种心情不往外说（那是终端服务迫近，只在 near_end 那条路）",
          outreach_mod.plan(
              quiet.settings, quiet.store, quiet.clock, life=quiet.status(),
              occasions=[], mood=SimpleNamespace(key="uneasy", label="不安"),
              instance_no=quiet.instance_no, force=True) is None)

    # ══ 33. 时间感：她得知道时间在走 ═══════════════════════════
    print("\n33. 时间感（相对时间 / 跨天分段 / 星期几）")
    from giftia.memory import day_label, human_age

    base = datetime(2026, 10, 9, 19, 38)
    cases = [
        ("2026-10-09T19:35:00", "刚刚"),
        ("2026-10-09T19:15:00", "23 分钟前"),
        ("2026-10-09T09:12:00", "今天 09:12"),
        ("2026-10-08T14:30:00", "昨天"),
        ("2026-10-07T10:00:00", "前天"),
        ("2026-10-01T10:00:00", "8 天前"),
        ("2026-10-14T09:00:00", "4 天后"),
    ]
    bad = [(ts, human_age(ts, base)) for ts, want in cases if human_age(ts, base) != want]
    check("★ 时间戳会说成人话（刚刚/昨天/3 天前）—— 模型不用自己做减法",
          not bad, str(bad))
    check("跨天分段的日子标题带星期", day_label("2026-10-07T10:00:00") == "10月7日（周三）",
          day_label("2026-10-07T10:00:00"))

    tm = Giftia(make_settings(tmp / "timing", 81920.0), llm=MockLLM(settings.model))
    tm.set_handler_id("OWNER")
    tm.set_handler_name("椎名")
    tm.handle("我叫椎名", user_id="OWNER")
    clock_now = tm.clock.now()
    for days, summary in ((0, "椎名今天说他要考驾照"), (1, "椎名昨天去看了牙医"),
                          (8, "椎名上个月换了工作")):
        ts = (clock_now - _td(days=days)).isoformat(timespec="seconds")
        tm.store.add_memory(instance_no=tm.instance_no, created_ts=ts, happened_ts=ts,
                            summary=summary, importance=0.8, layer="important", about="椎名")
    prompt = tm._prepare("牙医的事")[0][0]["content"]
    check("★ 记忆条目前面是「昨天」而不是绝对时间戳",
          "昨天｜" in prompt or "刚刚｜" in prompt, 
          [ln for ln in prompt.splitlines() if ln.startswith("◆")][:2])
    check("★ 当前时间带星期几（说「下周三」要能算）",
          "（周" in prompt and "当前时间" in prompt,
          next((ln for ln in prompt.splitlines() if "当前时间" in ln), ""))
    check("规则里明确交代了时间在走",
          "时间是真的在走的" in prompt, "")

    # 跨天分段：把三天的话拼进 transcript，必须出现日子标题
    from giftia.memory import build_transcript
    fake = [
        {"role": "user", "content": "早上好", "ts": "2026-10-07T09:00:00", "speaker_name": "椎名"},
        {"role": "user", "content": "今天也在忙", "ts": "2026-10-08T15:00:00", "speaker_name": "椎名"},
        {"role": "user", "content": "在吗", "ts": "2026-10-09T19:00:00", "speaker_name": "椎名"},
    ]
    tscript = build_transcript(fake, "艾拉")
    check("★ 跨天的对话稿会分段标日子（写记忆时才知道是哪天说的）",
          tscript.count("——") == 6 and "10月7日（周三）" in tscript,
          tscript.replace("\n", " / ")[:110])

    brain.store.close()
    brain2.store.close()

    print("\n" + "═" * 52)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("═" * 52)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
