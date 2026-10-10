#!/usr/bin/env python
"""Stream B 探针 4：工具循环（轮次封顶 / 最后一轮无工具 / 写库上限 / 错误路径 / 异常逃逸）。

    python .review\\probes\\bc_tools.py
"""
from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
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

from giftia import agent_tools  # noqa: E402
from giftia.brain import Giftia  # noqa: E402
from giftia.config import load_settings  # noqa: E402
from giftia.llm import ChatReply, MockLLM, ToolCall  # noqa: E402

PASS = FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  -> {detail}" if detail else ""))


class ToolLLM(MockLLM):
    """按剧本无限要求调工具；记录每轮有没有给工具、最后一轮的 messages。"""

    is_mock = False

    def __init__(self, cfg, plan=None, forever=None):
        super().__init__(cfg)
        self.plan = list(plan or [])
        self.forever = forever          # ("name", args) 一直要求调工具
        self.tools_seen: list[bool] = []
        self.final_messages: list[dict] | None = None
        self.tool_rounds = 0

    def chat(self, messages, **kw):
        self.tools_seen.append(False)
        self.final_messages = [dict(m) for m in messages]
        return "……嗯，我说完了。"

    def chat_with_tools(self, messages, *, tools=None, **kw):
        self.tools_seen.append(bool(tools))
        self.tool_rounds += 1
        if self.forever is not None:
            name, args = self.forever
            return ChatReply(tool_calls=[ToolCall(id=f"c{self.tool_rounds}", name=name,
                                                  arguments=args)])
        if self.plan:
            batch = self.plan.pop(0)
            calls = [ToolCall(id=f"c{i}", name=n, arguments=a) for i, (n, a) in enumerate(batch)]
            return ChatReply(tool_calls=calls)
        return ChatReply(text="……嗯，我记住了。")


def env(name: str, hours: float = 81920.0, **mem_over):
    tag = f"{os.getpid()}-{int(time.time())}"
    d = ROOT / ".review" / "tmp" / f"brain-checker-{name}-{tag}"
    d.mkdir(parents=True, exist_ok=True)
    base = load_settings(lifespan_override=hours, force_mock=True)
    mem = dataclasses.replace(base.memory, **mem_over) if mem_over else base.memory
    return dataclasses.replace(base, root=d, memory=mem)


def fresh(name, llm, **mem_over):
    st = env(name, **mem_over)
    h = Giftia(st, llm=llm)
    h.set_handler_id("OWNER")
    h.set_handler_name("椎名")
    return h


REM = json.dumps({"content": "椎名喜欢喝美式", "about": "椎名"}, ensure_ascii=False)

print("== 1. tools_max_rounds=3：一直要求调工具会不会死循环 ==")
h = fresh("tools3", ToolLLM(env("tools3").model, forever=("search", json.dumps({"query": "驾照"}, ensure_ascii=False))))
reply = h.handle("在吗", user_id="OWNER")
print(f"      chat_with_tools 调用次数={h.llm.tool_rounds}，每轮是否带工具={h.llm.tools_seen}")
check("1a 工具轮 = rounds-1 = 2（硬封顶，无死循环）", h.llm.tool_rounds == 2, str(h.llm.tool_rounds))
check("1b 最后一轮没有工具（tools_seen 最后一项 False）", h.llm.tools_seen[-1] is False,
      str(h.llm.tools_seen))
check("1c 她一定说得出话（回复非空）", bool(reply.strip()), repr(reply))
nudge = [m for m in (h.llm.final_messages or []) if m.get("role") == "system"
         and "不要再调用任何工具" in str(m.get("content"))]
check("1d 最后一轮带上了 nudge", len(nudge) == 1, str(len(nudge)))
check("1e 总模型调用次数 = tools_max_rounds", h.llm.tool_rounds + h.llm.tools_seen.count(False) == 3,
      str(h.llm.tools_seen))

print("\n== 2. tools_max_rounds=1 / 5 ==")
h1 = fresh("tools1", ToolLLM(env("tools1").model, forever=("search", "{}")),
           tools_max_rounds=1)
r1 = h1.handle("在吗", user_id="OWNER")
check("2a rounds=1 时一次工具都不给（直接 chat）", h1.llm.tools_seen == [False], str(h1.llm.tools_seen))

h5 = fresh("tools5", ToolLLM(env("tools5").model, forever=("search", "{}")),
           tools_max_rounds=5)
r5 = h5.handle("在吗", user_id="OWNER")
check("2b rounds=5 时工具轮=4，最后一轮无工具", h5.llm.tools_seen == [True] * 4 + [False],
      str(h5.llm.tools_seen))
check("2c rounds=5 也说得出话", bool(r5.strip()), repr(r5))

print("\n== 3. 一轮最多写 2 条记忆 ==")
plan = [[("remember", json.dumps({"content": f"第{i}条要多记的事", "importance": 0.9},
                                 ensure_ascii=False)) for i in range(3)],
        [("remember", json.dumps({"content": "第4条要多记的事", "importance": 0.9},
                                 ensure_ascii=False))]]
h3 = fresh("writes", ToolLLM(env("writes").model, plan=plan))
h3.handle("我要说四件事", user_id="OWNER")
mems = [m for m in h3.store.active_memories(h3.instance_no) if m["source_session"] == "tool"]
print(f"      实际写入 {len(mems)} 条：{[m['summary'] for m in mems]}")
check("3a 一轮最多写 2 条（4 次 remember 只落 2 条）", len(mems) == 2, str(len(mems)))
check("3b 超限的那两次没进 tool_writes", len(h3.tool_writes) == 2, str(h3.tool_writes))

print("\n== 4. 错误入参 / 未知工具不会崩 ==")
h4 = fresh("errs", ToolLLM(env("errs").model))
h4.handle("热身", user_id="OWNER")
cases = [
    ("未知工具", agent_tools.execute(h4, "没有这个工具", "{}")),
    ("坏 JSON", agent_tools.execute(h4, "remember", "{这不是 JSON")),
    ("参数不是对象", agent_tools.execute(h4, "remember", "[1,2,3]")),
    ("不认识的 about", agent_tools.execute(h4, "remember",
        json.dumps({"content": "张三喜欢钓鱼", "about": "张三"}, ensure_ascii=False))),
    ("memory_id 不是编号", agent_tools.execute(h4, "forget", json.dumps({"memory_id": "abc"}))),
    ("content 为空", agent_tools.execute(h4, "remember", "{}")),
]
for label, out in cases:
    check(f"4 {label}：返回人话不抛异常", bool(str(out).strip()), repr(out)[:60])
    print(f"        {label} → {str(out)[:70]}")
check("4g 不认识的 about 会把可用值喂回去", "椎名" in cases[3][1], cases[3][1][:60])

print("\n== 5. forget 有没有上限（写有 2 条上限，删没有）==")
h5b = fresh("forget", ToolLLM(env("forget").model))
h5b.handle("热身", user_id="OWNER")
ids = [h5b.store.add_memory(instance_no=h5b.instance_no, created_ts=h5b.clock.iso(),
                            happened_ts=h5b.clock.iso(), summary=f"第{i}条", importance=0.5,
                            scope="private") for i in range(6)]
outs = [agent_tools.execute(h5b, "forget", json.dumps({"memory_id": i})) for i in ids]
alive = [m["id"] for m in h5b.store.active_memories(h5b.instance_no)]
check("5a 一次 execute 能连删多条（没有 MAX_WRITES_PER_TURN 那样的上限）",
      all(m not in alive for m in ids), str(alive))
print(f"      删除结果：{outs}")

print("\n== 6. execute 的'永远不抛异常'承诺 vs DB 写失败 ==")
h6 = fresh("dbdown", ToolLLM(env("dbdown").model,
                             forever=("remember", json.dumps({"content": "这条写不进去"},
                                                             ensure_ascii=False))))
h6.handle("热身", user_id="OWNER")
real_add = h6.store.add_memory


def boom(*a, **kw):
    raise sqlite3.OperationalError("database is locked")


h6.store.add_memory = boom  # type: ignore[method-assign]
try:
    out = agent_tools.execute(h6, "remember", json.dumps({"content": "这条写不进去"},
                                                         ensure_ascii=False))
    check("6a DB 写失败时 execute 仍然返回文本（承诺成立）", True, repr(out)[:60])
except Exception as exc:
    check("6a DB 写失败时 execute 仍然返回文本（承诺成立）", False,
          f"抛出了 {type(exc).__name__}: {exc}")

n_before = h6.store.count_messages(h6.instance_no)
try:
    h6.handle("我要记一笔", user_id="OWNER")
    check("6b DB 写失败时 handle() 不抛异常", True)
    exc_txt = ""
except Exception as exc:
    exc_txt = f"{type(exc).__name__}: {exc}"
    check("6b DB 写失败时 handle() 不抛异常", False, exc_txt)
n_after = h6.store.count_messages(h6.instance_no)
print(f"      handle() 抛出：{exc_txt or '（无）'}")
print(f"      抛出前后 messages 条数：{n_before} -> {n_after}"
      f"（用户的这句话{'已落库但没有回复' if n_after > n_before else '没落库'}）")
h6.store.add_memory = real_add  # type: ignore[method-assign]

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
