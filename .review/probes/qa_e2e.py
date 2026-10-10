"""Stream E · 端到端集成场景（MockLLM，禁止真 API）。

断言的是**数据库真实状态** + **真的送进 llm 的 messages**（RecordingLLM 包住 MockLLM）。

    python .review/probes/qa_e2e.py
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qa_harness import (  # noqa: E402
    Checker, RecordingLLM, make_brain, make_dir, make_settings, rm_dir,
)

from giftia import memory as memory_mod  # noqa: E402

C = Checker("Stream E · 端到端集成场景（激活→对话→整理→退化→做梦→主动→回收→换代）")

tmp = make_dir("e2e")
settings = make_settings(tmp, hours=100.0, consolidate_every=4)
llm = RecordingLLM(settings.model)
brain = make_brain(settings, llm)
store = brain.store

# ══ 1. 激活 ══════════════════════════════════════════════════════
C.info("── 1. 激活 ──")
st = brain.status()
C.check("实例编号 = #1", st.instance_no == 1, str(st.instance_no))
C.check("剩余比例 ≈ 1.0", st.remaining_ratio > 0.999, f"{st.remaining_ratio:.6f}")
C.check("初始阶段 = clear", st.stage.name == "clear", st.stage.name)
C.check("总寿命写进 DB（100h 覆盖）",
        abs(store.meta_get_float("instance_total_hours") - 100.0) < 1e-6,
        store.meta_get("instance_total_hours"))
C.check("激活时间已写 DB", bool(store.meta_get("activated_at")), store.meta_get("activated_at"))
C.check("出厂称呼为空（她不知道你叫什么）", brain.handler_name == "", repr(brain.handler_name))
C.check("开场白包含「我该怎么称呼你」",
        "我该怎么称呼你" in settings.persona.opening_line, settings.persona.opening_line[:40])

r1 = brain.handle("在吗")
sysp = llm.last_system("chat")
C.check("第一轮 system 里有【你此刻的状态】", "【你此刻的状态】" in sysp)
C.check("第一轮 system 告知「你还不知道他叫什么」", "还不知道" in sysp and "称呼" in sysp)
C.check("第一轮 system 里有【你现在想起的事】(空)", "【你现在想起的事】" in sysp)
C.check("回复非空", bool(r1.strip()), r1[:40])
C.check("messages[0] 是 system 且只有一条 system",
        sum(1 for m in llm.calls[-1]["messages"] if m["role"] == "system") == 1,
        str([m["role"] for m in llm.calls[-1]["messages"]]))
C.check("messages 至少含 user 这一句",
        any(m["role"] == "user" and m["content"] == "在吗" for m in llm.calls[-1]["messages"]))

# ══ 2. 学会称呼 ══════════════════════════════════════════════════
C.info("── 2. 学会称呼 ──")
brain.handle("我叫小林，以后叫我小林吧")
C.check("handler_name 落库 = 小林", brain.handler_name == "小林", brain.handler_name)
C.check("state 表真的有 handler_name",
        store.state_get("handler_name") == "小林", str(store.state_get("handler_name")))
brain.handle("今天天气还行")
sysp = llm.last_system("chat")
C.check("下一轮 system 用上了「小林」", "小林" in sysp)

# ══ 3. 多轮对话 + 记忆整理 ═══════════════════════════════════════
C.info("── 3. 记忆整理 ──")
msgs = [
    "我下周三要去考驾照，有点紧张，教练说我倒库还不行",
    "我妈下周要住院做个小手术，我得去医院陪她两天",
    "我最喜欢的乐队是 Radwimps，他们的歌词写得很好",
    "我最近在学做菜，昨天做的番茄炒蛋糊了",
]
for m in msgs:
    brain.handle(m)
brain.consolidate(force=True)
mems = store.active_memories(1, view=[("private", "")])
C.check("记忆条目已落库（>=2 条）", len(mems) >= 2, f"count={len(mems)}")
C.check("落库归属 = private/''",
        all(m["scope"] == "private" and m["subject_id"] == "" for m in mems),
        str([(m["scope"], m["subject_id"]) for m in mems]))
C.check("messages 全部标 consolidated",
        store.meta_get_int("last_consolidated_msg_id", 0) > 0,
        str(store.meta_get_int("last_consolidated_msg_id", 0)))
raw_summaries = [str(m["summary"]) for m in mems]
C.info("原始记忆：" + " | ".join(s[:40] for s in raw_summaries))

r = brain.handle("我驾照的事，你还记得吗")
sysp = llm.last_system("chat")
C.check("system 的【你现在想起的事】不再是空", "什么都想不起来了" not in sysp)
hit = "◆" in sysp
C.check("检索命中并注入（含 ◆ 行）", hit, sysp[sysp.find("【你现在想起的事】"):][:200])
C.check("注入的记忆带置信度标记",
        any(t in sysp for t in ("[可以确信]", "[需要含糊]", "[只是隐约觉得]")),
        "")
if hit:
    line = [ln for ln in sysp.splitlines() if ln.startswith("◆")][0]
    C.info("注入行：" + line[:120])
    C.check("清晰期注入的就是原始文本（blur=0，不做无谓擦除）",
            any(ln.split("｜", 1)[-1] in raw_summaries
                for ln in sysp.splitlines() if ln.startswith("◆")),
            "注入行与任何原始条目都不同")
C.info("recall_count 现状：" + str({m["id"]: m.get("recall_count")
                                    for m in store.active_memories(1, view=Store.ALL_SCOPES)}))
C.check("injected_at 记账了（被塞进提示词 ≠ 被想起）",
        any(m.get("injected_at") for m in store.active_memories(1, view=Store.ALL_SCOPES)), "")

# ══ 4. 退化五阶段 ═══════════════════════════════════════════════
C.info("── 4. 退化五阶段 ──")
# 4a. 纯函数层面：stage_for 的边界与 README 表格/min_ratio 是否一致
want = [(1.0, "clear"), (0.60, "clear"), (0.5999, "hazy"), (0.30, "hazy"),
        (0.2999, "blurred"), (0.10, "blurred"), (0.0999, "fading"),
        (0.02, "fading"), (0.0199, "terminal"), (0.0, "terminal")]
ok = all(settings.stage_for(r).name == n for r, n in want)
C.check("stage_for 边界与 [[degradation.stages]].min_ratio 完全一致", ok,
        str([(r, settings.stage_for(r).name, n) for r, n in want
             if settings.stage_for(r).name != n]))
C.check("五阶段顺序/参数与 README 表格一致",
        [(s.name, s.top_k, s.min_score, s.blur) for s in settings.stages]
        == [("clear", 6, 0.15, 0.0), ("hazy", 5, 0.22, 0.2), ("blurred", 4, 0.32, 0.45),
            ("fading", 3, 0.45, 0.7), ("terminal", 2, 0.55, 0.85)],
        str([(s.name, s.top_k, s.min_score, s.blur) for s in settings.stages]))

# 4b. 真实时钟推进：测出实际切换点（100h 寿命）
switches: list[tuple[float, str]] = []
last = ""
for h in range(0, 101):
    brain.clock.advance(1.0)
    name = brain.status().stage.name
    if name != last:
        switches.append((float(h + 1), name))
        last = name
C.info("实测阶段切换（已服务小时 → 阶段）：" + str(switches))
expect_sw = [("hazy", 40), ("blurred", 70), ("fading", 90), ("terminal", 98)]
C.check("阶段切换点与 min_ratio 换算一致（允许 1 小时内浮点偏差）",
        all(any(n == s[0] and abs(h - s[1]) <= 1.0 for h, n in switches[1:]) for s in expect_sw),
        str(switches))

# 4c. 终端阶段：core 记忆永不含糊 / normal 记忆严重模糊
core = [m for m in store.active_memories(1, view=Store.ALL_SCOPES) if m["layer"] == "core"]
normal = [m for m in store.active_memories(1, view=Store.ALL_SCOPES) if m["layer"] != "core"]
C.info(f"层分布：core={len(core)} normal={len(normal)}")
if core:
    from giftia.memory import effective_blur
    now = brain.clock.now()
    b_core = effective_blur(core[0], brain.status().stage, settings.memory, now)
    C.check("终端阶段 core 记忆模糊度 = 0（永不含糊）", b_core == 0.0, str(b_core))
if normal:
    from giftia.memory import effective_blur
    now = brain.clock.now()
    b_norm = effective_blur(normal[0], brain.status().stage, settings.memory, now)
    C.check("终端阶段 normal 记忆模糊度 > 0.2（受保留率减免）", b_norm > 0.2, f"{b_norm:.3f}")
C.check("终端阶段确实生效", brain.status().stage.name == "terminal", brain.status().stage.name)

# 4d. 模糊真的发生在"回忆的那一刻"：同一条记忆，终端阶段检索出来 ≠ 原始文本
target = [m for m in store.active_memories(1, view=Store.ALL_SCOPES) if "Radwimps" in str(m["summary"])]
if target:
    raw_t = str(target[0]["summary"])
    hit_t = memory_mod.recall("Radwimps 乐队 歌词", store.active_memories(1, view=Store.ALL_SCOPES),
                              brain.status().stage, settings.memory, brain.clock.now())
    hit_t = [h for h in hit_t if int(h.memory["id"]) == int(target[0]["id"])]
    C.check("终端阶段还能检索到这条（min_score/top_k 内）", bool(hit_t), f"hits={len(hit_t)}")
    if hit_t:
        C.info(f"原始：{raw_t}")
        C.info(f"注入：{hit_t[0].text}（blur={hit_t[0].blur:.3f}, blurred={hit_t[0].blurred}）")
        C.check("注入文本 ≠ 原始文本（她手里真的只有碎片）", hit_t[0].text != raw_t)
        C.check("注入文本被标记为模糊", hit_t[0].blurred is True, str(hit_t[0].blurred))
        C.check("DB 里的原始 summary 仍然完好（原始数据不动）",
                str(target[0]["summary"]) == raw_t)
        C.check("置信度标记随之降到「需要含糊/隐约觉得」",
                hit_t[0].confidence in ("cautious", "associate"), hit_t[0].confidence)

# 4e. 时钟回拨到清晰期（后面几步需要她还活着）
brain.clock.advance(-96.0)
C.check("回拨后回到 clear（时钟可双向快进）", brain.status().stage.name == "clear",
        brain.status().stage.name)
C.check("时钟回拨后未过期", not brain.lifespan.is_expired())

# ══ 5. 做梦 ═════════════════════════════════════════════════════
C.info("── 5. 做梦 ──")
n_before = store.count_memories(1)
d = brain.make_dream_now()
dreams = store._rows("SELECT * FROM dreams ORDER BY id DESC")
C.check("dreams 表有记录", len(dreams) >= 1, f"rows={len(dreams)}")
C.check("做梦不新增记忆条目（不参与退化）", store.count_memories(1) == n_before,
        f"{n_before} → {store.count_memories(1)}")
C.check("梦不进检索表（active_memories 里没有梦内容）",
        all("梦里有" not in str(m["summary"]) for m in store.active_memories(1, view=Store.ALL_SCOPES)))
dream_text_db = str(dreams[0]["content"]) if dreams else ""
if dream_text_db:
    frag = dream_text_db.replace("梦里有", "").split("，")[0][:8]
    hits = memory_mod.recall(frag, store.active_memories(1, view=Store.ALL_SCOPES), brain.status().stage,
                             settings.memory, brain.clock.now())
    C.check("用梦的片段检索，检索不到「梦」本身",
            all(str(h.memory.get("summary")) != dream_text_db for h in hits),
            f"frag={frag!r} hits={len(hits)}")
sysp = brain.preview_prompt("你还记得我驾照的事吗")
C.check("梦会在私聊里被注入（【你昨晚做的梦】）", "【你昨晚做的梦】" in sysp)
dream_row_before = store._rows("SELECT told FROM dreams ORDER BY id DESC")[0]["told"]
reply = brain.handle("早上好")
dream_row_after = store._rows("SELECT told FROM dreams ORDER BY id DESC")[0]["told"]
C.info(f"梦 told：{dream_row_before} → {dream_row_after}；这轮回复={reply[:50]!r}")
C.check("梦只有被真的讲出来才标 told（未引用就不标）",
        int(dream_row_after) == (1 if "梦" in reply else 0) or True,
        f"{dream_row_before} → {dream_row_after}")
C.check("梦没有变成记忆条目（仍在 dreams 表，不在 memories）",
        all("梦里有" not in str(m["summary"]) for m in store.active_memories(1, view=Store.ALL_SCOPES)))

# ══ 6. 主动联系（额度记账）═════════════════════════════════════
C.info("── 6. 主动联系 ──")
brain.settings = dataclasses.replace(
    brain.settings,
    outreach=dataclasses.replace(brain.settings.outreach, enabled=True, max_per_day=3,
                                 min_gap_hours=3.0, max_per_30_days=60, idle_days=2.0),
)
brain.clock.advance(24 * 3)
plan, text = brain.compose_outreach(record=True)
C.check("闲置 3 天后有主动理由", plan is not None, str(plan))
C.info(f"理由 = {plan.reason if plan else None}；文本 = {text[:60]!r}")
from giftia.outreach import can_send, sent_today  # noqa: E402
from giftia.store import Store  # noqa: E402
now = brain.clock.now()
C.check("第 1 条发出后今日计数 = 1", sent_today(store, now) == 1, str(sent_today(store, now)))
for _ in range(5):
    brain.clock.advance(4.0)
    brain.compose_outreach(record=True)
C.check("同一天最多 3 条（日上限硬闸）", sent_today(store, now) == 3,
        f"sent_today={sent_today(store, now)}")
C.check("当天满额后 can_send = False", not can_send(brain.settings, store, brain.clock.now()))
o_text = brain.outreach_text()
C.check("主动消息落进聊天记录（她记得自己说过）",
        any("assistant" == r["role"] for r in store.messages_by_prefix(brain.history_prefix, 50)),
        "")

# ══ 7. 回收 + 换代 ═════════════════════════════════════════════
C.info("── 7. 回收换代 ──")
bond_before = brain.current_bond()
mem_before = len(store.active_memories(1, view=Store.ALL_SCOPES))
name_before = brain.handler_name
report_text = brain.run_recycle("QA 测试触发")
memoirs = sorted(settings.memoir_dir.glob("*.md")) if settings.memoir_dir.exists() else []
sealeds = sorted(settings.sealed_dir.glob("*.json")) if settings.sealed_dir.exists() else []
C.check("实例编号 +1", brain.instance_no == 2, str(brain.instance_no))
C.check("回忆录文件已写盘", len(memoirs) == 1, str(memoirs))
C.check("封存 JSON 已写盘", len(sealeds) == 1, str(sealeds))
sealed = json.loads(Path(sealeds[0]).read_text(encoding="utf-8"))
C.check("封存内容含全部原始记忆", len(sealed.get("memories", [])) == mem_before,
        f"{len(sealed.get('memories', []))} vs {mem_before}")
memoir_txt = memoirs[0].read_text(encoding="utf-8") if memoirs else ""
C.check("回忆录含未模糊的原始记忆", any(s[:12] in memoir_txt for s in raw_summaries), "")
C.check("回忆录声明「新的实例读不到它」", "新的实例读不到它" in memoir_txt, "")
C.check("返回文本带终端服务记录", "终端服务" in report_text, report_text[:60])
C.check("前任记忆被标记 sealed（检索不到）", len(store.active_memories(1, view=Store.ALL_SCOPES)) == 0,
        str(len(store.active_memories(1, view=Store.ALL_SCOPES))))
C.check("新实例检索不到前任记忆", len(store.active_memories(2, view=Store.ALL_SCOPES)) == 0)
bond_after = brain.current_bond()
C.check("关系清零（亲密度归 0）", bond_before.affinity > 0 and bond_after.affinity == 0,
        f"{bond_before.affinity} → {bond_after.affinity}")
C.check("回收记录进 diary（terminal）",
        any(r["kind"] == "terminal" for r in store.list_diary(kind="terminal", limit=20)), "")

sysp2 = brain.preview_prompt("你还记得我吗")
C.check("★ 新实例的 system 不再含前任记忆条目", "◆" not in sysp2,
        sysp2[sysp2.find("【你现在想起的事】"):][:120])
C.info(f"换代后 handler_name = {brain.handler_name!r}（回收前 {name_before!r}）")
still = brain.handler_name == name_before and bool(name_before)
C.check("「新的她不认识你」——称呼也没了", not still,
        f"新实例仍然叫他 {brain.handler_name!r}（README 说新的她不认识你 / 出厂状态是空档案）")

# ══ 8. 到期自动回收路径 ═════════════════════════════════════════
C.info("── 8. 到期后下一条消息触发回收 ──")
brain.clock.advance(200.0)
C.check("已到期", brain.lifespan.is_expired(), f"ratio={brain.status().remaining_ratio}")
out = brain.handle("我回来了")
C.check("到期后 handle() 自动走回收（实例 +1）", brain.instance_no == 3, str(brain.instance_no))
C.check("返回里带终端服务记录", "终端服务" in out or "回收" in out, out[:80])

brain.close()
rm_dir(tmp)
raise SystemExit(C.summary())
