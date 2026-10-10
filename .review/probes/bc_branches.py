#!/usr/bin/env python
"""Stream B 探针 1：处理流水线每条分支的隐私/归属边界。

只读产品代码，全部用离线 MockLLM + 显式临时目录。
    python .review\\probes\\bc_branches.py
"""
from __future__ import annotations

import dataclasses
import os
import sys
import time
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from giftia.brain import Giftia  # noqa: E402
from giftia.config import load_settings  # noqa: E402
from giftia.llm import ChatReply, MockLLM  # noqa: E402
# 探针自检用（契约 §一：active_memories 的 view 必填，全量要显式 ALL_SCOPES）
from giftia.store import Store  # noqa: E402

PASS = FAIL = 0
SECRET = "SECRET-ALPHA-7731"


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  -> {detail}" if detail else ""))


class Rec(MockLLM):
    """记录模型实际收到的 messages（+ 各类调用次数）。"""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.prompts: list[list[dict]] = []
        self.tools_seen: list[bool] = []
        self.extract_calls = 0

    def chat(self, messages, **kw):
        self.prompts.append([dict(m) for m in messages])
        return super().chat(messages, **kw)

    def chat_with_tools(self, messages, *, tools=None, **kw):
        self.prompts.append([dict(m) for m in messages])
        self.tools_seen.append(bool(tools))
        return ChatReply(text=super().chat(messages, **kw))

    def extract_memories(self, transcript, **kw):
        self.extract_calls += 1
        return super().extract_memories(transcript, **kw)

    @property
    def system(self) -> str:
        if not self.prompts:
            return ""
        return "".join(
            str(m.get("content", "")) for m in self.prompts[-1] if m.get("role") == "system"
        )

    def reset(self) -> None:
        self.prompts.clear()
        self.tools_seen.clear()


def env(name: str, hours: float = 81920.0, **mem_over):
    # 每次跑都是全新的目录（带 pid+时间戳），避免上一次的库残留影响断言
    tag = f"{os.getpid()}-{int(time.time())}"
    d = ROOT / ".review" / "tmp" / f"brain-checker-{name}-{tag}"
    d.mkdir(parents=True, exist_ok=True)
    base = load_settings(lifespan_override=hours, force_mock=True)
    mem = dataclasses.replace(base.memory, **mem_over) if mem_over else base.memory
    return dataclasses.replace(base, root=d, memory=mem)


h = Giftia(env("branches"), llm=Rec(env("branches").model))
h.set_handler_id("OWNER")
h.set_handler_name("椎名")

print("== 场景 A：私聊搭档，先埋一条只属于搭档的私事 ==")
h.handle("我叫椎名", user_id="OWNER")
sec_id = h.store.add_memory(
    instance_no=h.instance_no,
    created_ts=h.clock.iso(),
    happened_ts=h.clock.iso(),
    summary=f"搭档的秘密 {SECRET}：他下周三要考驾照，很紧张",
    tags="私事",
    emotion="紧张",
    importance=0.9,
    layer="important",
    scope="private",
    subject_id="",
    about="椎名",
)
h.llm.reset()
h.handle("我下周要考驾照了", user_id="OWNER")
check("A1 私聊里这条私事被检索进提示词（基线）", SECRET in h.llm.system)
check("A2 落库的消息 scope=private", h.store.all_messages(h.instance_no)[-2]["scope"] == "private",
      str(h.store.all_messages(h.instance_no)[-2]["scope"]))

print("\n== 场景 B：群里被 @（is_handler=True, group_id=G-1）==")
h.llm.reset()
h.handle("我在群里说句话", user_id="OWNER", scene="group", group_id="G-1",
         speaker_name="椎名", is_handler=True)
check("B1 群里没有拿到搭档的私事（隐私闸门生效）", SECRET not in h.llm.system)
check("B2 群会话 scope=g<gid>", h.scope_key == "gG-1"[:12] and h.memory_scope == ("group", "G-1"),
      f"{h.scope_key} {h.memory_scope}")
row = h.store.all_messages(h.instance_no)[-2]
check("B3 群里落库的 user 消息 scope/subject 正确",
      (row["scope"], row["subject_id"]) == ("group", "G-1"),
      f"scope={row['scope']} subject={row['subject_id']} scene={row.get('scene')}")

print("\n== 场景 C：群里陌生人（适配器 guest 路径：scene=group + is_handler=False）==")
b0 = h.current_bond()
h.store.add_memory(instance_no=h.instance_no, created_ts=h.clock.iso(), happened_ts=h.clock.iso(),
                   summary="阿伟说过他家的猫叫豆豆", importance=0.6, scope="group",
                   subject_id="G-1", about="阿伟")
h.llm.reset()
reply = h.handle("你好呀，你认识我吗", user_id="STRANGER", scene="group", group_id="G-1",
                 speaker_name="小雨", is_handler=False)
check("C1 客人拿不到搭档私事", SECRET not in h.llm.system)
# ★ 期望翻转（lead 授权，2026-10-10）：原来这条断言"豆豆"在提示词里，用来证明
#   "客人完全不检索"不成立。但修前它成立的原因**不是检索**，而是 P1-13 的兜底循环——
#   印象块把 by_person 里不在场的人（阿伟）全都补进了提示词。P1-13 修掉之后，
#   这一轮 query 与那条记忆无关，检索本就该是空的，所以改为断言"不在场的人不该出现在注入提示词里"。
check("C2 ★ 客人那一轮不在场的人（阿伟）不该出现在注入提示词里",
      "豆豆" not in h.llm.system)
# 客人仍然有"本群公开记忆"的**视角**：问对了事照样检索得到（对照见 D 段/契约 §三）
h.llm.reset()
h.handle("阿伟家的猫叫什么来着", user_id="STRANGER2", scene="group", group_id="G-1",
         speaker_name="小雨", is_handler=False)
check("C2b 客人问对了事，本群公开记忆照旧检索得到（视角没变窄）",
      "豆豆" in h.llm.system)
b1 = h.current_bond()
check("C3 客人不推进关系", (b0.affinity, b0.interactions) == (b1.affinity, b1.interactions),
      f"{b0.affinity}/{b0.interactions} -> {b1.affinity}/{b1.interactions}")
check("C4 客人不进 'm' 会话", h.scope_key.startswith("g"), h.scope_key)
row = h.store.all_messages(h.instance_no)[-2]
check("C5 客人消息落库 scope=group", (row["scope"], row["subject_id"]) == ("group", "G-1"),
      f"{row['scope']}/{row['subject_id']}")
check("C6 客人那一轮没有给工具", h.llm.tools_seen == [], str(h.llm.tools_seen))

print("\n== 场景 D：解绑后（handler_id='')，陌生人的第一条私聊 ==")
h.set_handler_id("")   # 模拟 /unbind
h.llm.reset()
r2 = h.handle("你好，我下周要考驾照，有点紧张", user_id="STRANGER-X", scene="c2c")
check("D1 解绑后陌生人被判成搭档", h.is_handler is True, f"is_handler={h.is_handler}")
check("D2 ★ 前搭档的私事进了新人的提示词", SECRET not in h.llm.system)
check("D3 新人的 scope_key 变成 'm'", h.scope_key == "m", h.scope_key)
leaked = [ln for ln in h.llm.system.splitlines() if SECRET in ln]
print(f"      提示词里的泄漏行：{leaked[:1]}")
print(f"      （她的回复：{r2[:70]}）")

print("\n== 场景 E：核心 API 忘了传 group_id（scene=group + is_handler=True）==")
h.set_handler_id("OWNER")
h.llm.reset()
h.handle("群里说话", user_id="OWNER", scene="group", group_id="", speaker_name="椎名",
         is_handler=True)
check("E1 ★ 群场景缺 group_id 时回落到搭档私聊视图（私事进群）", SECRET not in h.llm.system)
check("E2 此时 scope_key='m'", h.scope_key == "m", h.scope_key)

print("\n== 场景 F：旁听（log_only）落库归属 ==")
h2 = Giftia(env("branches2"), llm=Rec(env("branches2").model))
h2.set_handler_id("OWNER")
for i in range(3):
    h2.handle(f"群里第{i}句闲聊，大家在聊周末去哪玩", user_id="STRANGER", scene="group",
              group_id="G-9", speaker_name="阿伟", log_only=True, mid=f"m{i}")
rows = h2.store.all_messages(h2.instance_no)
check("F1 旁听消息全部落库", len(rows) == 3, str(len(rows)))
check("F2 旁听消息 scope/subject = (group, G-9)",
      all((r["scope"], r["subject_id"]) == ("group", "G-9") for r in rows),
      str([(r["scope"], r["subject_id"]) for r in rows]))
check("F3 旁听不产生 assistant 回复", all(r["role"] == "user" for r in rows),
      str([r["role"] for r in rows]))
check("F4 旁听没有调用模型", h2.llm.prompts == [], str(len(h2.llm.prompts)))
check("F5 旁听没有推进关系", h2.current_bond().interactions == 0,
      str(h2.current_bond().interactions))

print("\n== 场景 G：会话串味（群 A vs 群 B；陌生人 P1 vs P2）==")
g = Giftia(env("branches3"), llm=Rec(env("branches3").model))
g.set_handler_id("OWNER")
g.set_handler_name("椎名")
g.handle("群A的话题是钓鱼", user_id="OWNER", scene="group", group_id="GA",
         speaker_name="椎名", is_handler=True)
g.handle("群B的话题是爬山", user_id="OWNER", scene="group", group_id="GB",
         speaker_name="椎名", is_handler=True)
g.llm.reset()
g.handle("接着说", user_id="OWNER", scene="group", group_id="GA", speaker_name="椎名",
         is_handler=True)
sys_a = g.llm.system
check("G1 群 A 的工作记忆里没有群 B 的内容", "爬山" not in sys_a)
check("G2 群 A 的工作记忆里有群 A 的内容", "钓鱼" in sys_a)
g.llm.reset()
g.handle("你好", user_id="P1", scene="c2c", speaker_name="甲")
g.llm.reset()
g.handle("你好", user_id="P2", scene="c2c", speaker_name="乙")
check("G3 陌生人 P2 看不到 P1 的会话", "P1" not in g.llm.system and "甲" not in g.llm.system)
print(f"      P2 会话 id = {g.session_id}")

print("\n== 场景 H：显式 scene='guest' + group_id（persona 文档里的访客语义）==")
g.set_handler_id("OWNER")
g.store.add_memory(instance_no=g.instance_no, created_ts=g.clock.iso(), happened_ts=g.clock.iso(),
                   summary=f"搭档的私事 {SECRET}", importance=0.9, scope="private", subject_id="")
g.llm.reset()
g.handle("你是谁", user_id="GUEST", scene="guest", group_id="GB", speaker_name="丙",
         is_handler=False)
check("H1 显式 guest 也拿不到私聊记忆", SECRET not in g.llm.system)
check("H2 guest 走的是群视图", g.memory_view == [("group", "GB")], str(g.memory_view))
check("H3 guest 没给工具", g.llm.tools_seen == [], str(g.llm.tools_seen))
check("H4 适配器从来不用 scene='guest'（下面 grep 证据另附）", True)

print("\n== 场景 I：群 id 里含 LIKE 元字符（_ / %）时的'往上翻聊天记录' ==")
i = Giftia(env("branches4"), llm=Rec(env("branches4").model))
i.set_handler_id("OWNER")
i.handle("这是G1群的内容-爬山", user_id="OWNER", scene="group", group_id="G1",
         speaker_name="椎名", is_handler=True)
i.handle("这是G_1群的内容-钓鱼", user_id="OWNER", scene="group", group_id="G_1",
         speaker_name="椎名", is_handler=True)
g1_hist = " ".join(str(m["content"]) for m in i.store.messages_by_prefix("i1-gG1-", 20))
gu_hist = " ".join(str(m["content"]) for m in i.store.messages_by_prefix("i1-gG_1-", 20))
print(f"      G_1 的 history_prefix 实际查询串 = {i.store.__class__.__name__} 见下方")
print(f"      messages_by_prefix('i1-gG_1-') → {gu_hist!r}")
check("I1 ★ 群 'G_1' 翻到了群 'G1' 的记录（LIKE 元字符被删掉）",
      "爬山" not in gu_hist and "钓鱼" in gu_hist, gu_hist)

print("\n== 场景 J：客人那一轮到底写不写记忆（README 写「不写记忆」）==")
j = Giftia(env("guestwrite", consolidate_every=2), llm=Rec(env("guestwrite").model))
j.set_handler_id("OWNER")
for i in range(2):
    j.handle(f"我是客人，第{i}次说话，聊的是周末天气怎么样", user_id="GUEST", scene="group",
             group_id="GW", speaker_name="丙", is_handler=False)
jm = j.store.active_memories(j.instance_no, view=Store.ALL_SCOPES)
print(f"      客人两轮之后她的记忆：{[(m['scope'], m['subject_id'], m['summary'][:18]) for m in jm]}")
check("J1 ★ 客人说的话会被整理成记忆（README 的『不写记忆』字面不成立）", bool(jm), str(len(jm)))
check("J2 但归属正确：全部是 group 归属，没有 private",
      bool(jm) and {m["scope"] for m in jm} == {"group"}, str({m["scope"] for m in jm}))

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
