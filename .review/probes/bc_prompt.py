#!/usr/bin/env python
"""Stream B 探针 3：system 提示词的块顺序/前缀稳定性、印象注入的在场闸门、工作记忆裁剪。

    python .review\\probes\\bc_prompt.py
"""
from __future__ import annotations

import dataclasses
import os
import re
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
from giftia.llm import MockLLM  # noqa: E402

PASS = FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  -> {detail}" if detail else ""))


class Rec(MockLLM):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.prompts: list[list[dict]] = []

    def chat(self, messages, **kw):
        self.prompts.append([dict(m) for m in messages])
        return super().chat(messages, **kw)

    def chat_with_tools(self, messages, *, tools=None, **kw):
        self.prompts.append([dict(m) for m in messages])
        return super().chat_with_tools(messages, tools=tools, **kw)

    @property
    def system(self) -> str:
        return str(self.prompts[-1][0]["content"]) if self.prompts else ""

    @property
    def messages(self) -> list[dict]:
        return self.prompts[-1] if self.prompts else []

    def reset(self):
        self.prompts.clear()


def env(name: str, hours: float = 81920.0, **mem_over):
    tag = f"{os.getpid()}-{int(time.time())}"
    d = ROOT / ".review" / "tmp" / f"brain-checker-{name}-{tag}"
    d.mkdir(parents=True, exist_ok=True)
    base = load_settings(lifespan_override=hours, force_mock=True)
    mem = dataclasses.replace(base.memory, **mem_over) if mem_over else base.memory
    return dataclasses.replace(base, root=d, memory=mem)


def blocks_of(system: str) -> list[str]:
    """把 system 拆成块（块首是【…】或以人格内核开头）。"""
    heads = [m.start() for m in re.finditer(r"(?m)^【", system)]
    out = []
    if heads and heads[0] > 0:
        out.append(system[: heads[0]])
    for i, h in enumerate(heads):
        end = heads[i + 1] if i + 1 < len(heads) else len(system)
        out.append(system[h:end])
    return out


# ══ 1. 块顺序与前缀稳定性 ══════════════════════════════════════
print("== 1. 一条 system 的块顺序 / 人格内核是否在开头且不变 ==")
h = Giftia(env("prompt"), llm=Rec(env("prompt").model))
h.set_handler_id("OWNER")
h.set_handler_name("椎名")
h.handle("我叫椎名", user_id="OWNER")
h.llm.reset()
h.handle("今天加班好累", user_id="OWNER")
s1 = h.llm.system
bl = blocks_of(s1)
titles = [b.split("\n", 1)[0][:24] for b in bl]
print("      块顺序：")
for i, t in enumerate(titles):
    print(f"        [{i}] {t}")
check("1a 第一块是人格内核（不含【】标题）", not titles[0].startswith("【"), titles[0])
core = h.settings.persona.core_text("椎名").strip()
check("1b system 以 persona.core_text() 原样开头（内核在最前面）", s1.startswith(core),
      f"core 头 40 字={core[:40]!r} system 头 40 字={s1[:40]!r}")
print(f"      人格内核本身含 {len(re.findall(r'(?m)^【', core))} 个【】子块，"
      f"共 {len(core)} 字；README 说的'六块'是简化描述")

h.llm.reset()
h.clock.advance(61 / 3600.0)   # 时间往前走 61 秒
h.handle("刚才说到哪了", user_id="OWNER")
s2 = h.llm.system
n = 0
while n < min(len(s1), len(s2)) and s1[n] == s2[n]:
    n += 1
print(f"      两轮 system 的公共前缀 = {n} 字节 / 全长 {len(s1)} 与 {len(s2)}")
blk_at_diff = s1[:n].count("【")
print(f"      第一处差异落在第 {blk_at_diff} 个【】块之后")
check("1c 人格内核是稳定前缀（两轮完全一致）", s2.startswith(bl[0]))
diff_ctx = s1[max(0, n - 30): n + 40].replace("\n", "\\n")
print(f"      差异点上下文：…{diff_ctx}…")

# ══ 2. 印象块：在场闸门是否真的生效 ════════════════════════════
print("\n== 2. 【你对他们的印象】是否只带在场的人 ==")
g = Giftia(env("impress"), llm=Rec(env("impress").model))
g.set_handler_id("OWNER")
g.set_handler_name("椎名")
# 30 条群记忆，分属 15 个人（每人 2 条），全部 scope=group
people = [f"成员{i}" for i in range(15)]
for i, who in enumerate(people):
    for k in range(2):
        g.store.add_memory(instance_no=g.instance_no, created_ts=g.clock.iso(),
                           happened_ts=g.clock.iso(),
                           summary=f"{who}提过他{k}号要去外地出差，家里有点事",
                           importance=0.6, layer="normal", scope="group",
                           subject_id="GK", about=who)
g.handle("大家好", user_id="OWNER", scene="group", group_id="GK", speaker_name="椎名",
         is_handler=True)
g.llm.reset()
# 说话的是"成员3"，跟别人无关，也没点任何人的名字
g.handle("我随便说一句，今天天气不错", user_id="U3", scene="group", group_id="GK",
         speaker_name="成员3", is_handler=False)
sysmsg = g.llm.system
imp = ""
for b in blocks_of(sysmsg):
    if b.startswith("【你对他们的印象】"):
        imp = b
present_leak = [p for p in people if p != "成员3" and p in imp]
check("2a ★ 印象块只带在场的人（文档称'只带本轮真的出现过的人'）", not present_leak,
      f"不在场却出现：{present_leak[:5]}（共 {len(present_leak)} 人）")
print(f"      印象块行数：{len([l for l in imp.splitlines() if l.startswith('- ')])}")
print(f"      印象块前 3 行：{imp.splitlines()[1:4]}")

# 私聊场景下印象块是否带全部群成员
g.llm.reset()
g.handle("今天怎么样", user_id="OWNER")
imp_priv = ""
for b in blocks_of(g.llm.system):
    if b.startswith("【你对他们的印象】"):
        imp_priv = b
priv_people = [p for p in people if p in imp_priv]
print(f"      私聊里印象块包含的群成员数：{len(priv_people)}")
check("2b 私聊里带群成员印象是（符合'你本来就有权知道'）", len(priv_people) > 0)

# ══ 3. 花名册是否按群隔离 ══════════════════════════════════════
print("\n== 3. 【群里你认得的人】花名册的群隔离 ==")
r = Giftia(env("roster"), llm=Rec(env("roster").model))
r.set_handler_id("OWNER")
r.set_handler_name("椎名")
r.handle("我是阿伟", user_id="U-A", scene="group", group_id="GB", speaker_name="阿伟")
r.llm.reset()
r.handle("在吗", user_id="OWNER", scene="group", group_id="GA", speaker_name="椎名",
         is_handler=True)
roster = ""
for b in blocks_of(r.llm.system):
    if b.startswith("【群里你认得的人】"):
        roster = b
check("3a ★ 群 A 的花名册里没有只在群 B 出现过的人", "阿伟" not in roster, roster.replace("\n", " | ")[:120])
print(f"      群 A 花名册：{roster.replace(chr(10), ' | ')[:160]}")

# ══ 4. 工作记忆裁剪 ════════════════════════════════════════════
print("\n== 4. session_window / session_char_budget 裁剪 ==")
w = Giftia(env("window", session_window=4, session_char_budget=60),
           llm=Rec(env("window").model))
w.set_handler_id("OWNER")
for i in range(8):
    w.handle(f"第{i}句：这是一段用来测试裁剪的长句子，长度会超过预算" * 2, user_id="OWNER")
w.llm.reset()
w.handle("最后这一句必须原样出现在消息末尾", user_id="OWNER")
msgs = w.llm.messages
check("4a messages[0] 永远是 system", msgs[0]["role"] == "system", str([m["role"] for m in msgs]))
check("4b 最后一条永远是当前用户消息",
      msgs[-1]["role"] == "user" and msgs[-1]["content"] == "最后这一句必须原样出现在消息末尾",
      str(msgs[-1])[:80])
hist = [len(m["content"]) for m in msgs[1:-1]]
print(f"      裁剪后历史条数={len(hist)} 字符={hist} budget={w.settings.memory.session_char_budget}")
check("4c 历史字符总量受预算约束（允许一条超预算的兜底）",
      sum(hist) <= w.settings.memory.session_char_budget + max(hist or [0]),
      f"sum={sum(hist)}")
check("4d system 不受字符预算裁剪（它不在 _window 里）", len(msgs[0]["content"]) > 60)
print(f"      system 长度={len(msgs[0]['content'])}，历史长度={sum(hist)}")
roles = "".join("u" if m["role"] == "user" else "a" for m in msgs[1:-1])
print(f"      历史角色序列：{roles or '(空)'}")

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
