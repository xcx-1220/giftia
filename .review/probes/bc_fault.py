#!/usr/bin/env python
"""Stream B 探针 6：异常路径与状态一致性（模型失败 / 发送失败 / 非 LLMError 逃逸 / 运行档案）。

    python .review\\probes\\bc_fault.py
"""
from __future__ import annotations

import dataclasses
import json
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

from giftia.brain import FAILURE_LINES, Giftia  # noqa: E402
from giftia.config import load_settings  # noqa: E402
from giftia.llm import ChatReply, LLMError, MockLLM, ToolCall  # noqa: E402
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


class Flaky(MockLLM):
    def __init__(self, cfg, fail_times=999):
        super().__init__(cfg)
        self.fail_times = fail_times
        self.calls = 0

    def chat(self, messages, **kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise LLMError("模拟故障：连接被重置")
        return super().chat(messages, **kw)


class Boom(MockLLM):
    """非 LLMError 的异常（模拟未来换 provider / 库内部错误）。"""

    def chat(self, messages, **kw):
        raise ValueError("模型层冒出来的莫名异常")


class Empty(MockLLM):
    def chat(self, messages, **kw):
        return ""


class WriteThenFail(MockLLM):
    """每一轮都调工具写记忆，最后一轮（无工具）chat 抛 LLMError。"""

    is_mock = False

    def __init__(self, cfg):
        super().__init__(cfg)
        self.round = 0

    def chat(self, messages, **kw):
        raise LLMError("最后一轮模型挂了")

    def chat_with_tools(self, messages, *, tools=None, **kw):
        self.round += 1
        return ChatReply(tool_calls=[ToolCall(
            id=f"c{self.round}", name="remember",
            arguments=json.dumps({"content": f"第{self.round}件答应过的事 {SECRET}"},
                                 ensure_ascii=False))])


def env(name: str, hours: float = 81920.0, **mem_over):
    tag = f"{os.getpid()}-{int(time.time())}"
    d = ROOT / ".review" / "tmp" / f"brain-checker-{name}-{tag}"
    d.mkdir(parents=True, exist_ok=True)
    base = load_settings(lifespan_override=hours, force_mock=True)
    mem = dataclasses.replace(base.memory, **mem_over) if mem_over else base.memory
    return dataclasses.replace(base, root=d, memory=mem)


def fresh(name, llm):
    st = env(name)
    h = Giftia(st, llm=llm)
    h.set_handler_id("OWNER")
    h.set_handler_name("椎名")
    return h


print("== 1. 模型请求失败（LLMError）：她会不会'以为自己说过' ==")
h = fresh("flaky", Flaky(env("flaky").model))
before = h.store.count_messages(h.instance_no)
reply = h.handle("在吗", user_id="OWNER")
after = h.store.count_messages(h.instance_no)
rows = h.store.all_messages(h.instance_no)[-2:]
print(f"      她的回复：{reply[:40]}")
check("1a 失败时用 FAILURE_LINES 兜底（不抛异常）", reply in FAILURE_LINES, reply[:40])
check("1b 用户消息与兜底回复都落库（2 条）", after - before == 2, f"{before} -> {after}")
check("1c 兜底回复也进了她的聊天记录",
      rows[-1]["role"] == "assistant" and rows[-1]["content"] == reply, str(rows[-1])[:60])
check("1d last_reply_id 指向那条兜底回复", h.last_reply_id is not None, str(h.last_reply_id))

# 发送失败回报 → 从她的记忆里撤回
n0 = h.store.count_messages(h.instance_no)
h.note_reply_delivery("")
n1 = h.store.count_messages(h.instance_no)
check("1e ★ 发送失败回报后，那条回复从记忆里撤回", n1 == n0 - 1, f"{n0} -> {n1}")
diary = h.store.list_diary(kind="terminal", limit=3)
check("1f 留下了一条'发送失败'档案", any("发送失败" in (r["title"] or "") for r in diary),
      str([r["title"] for r in diary]))

print("\n== 2. 部分送达：按用户实际看到的内容留存 ==")
h2 = fresh("partial", Flaky(env("partial").model, fail_times=0))
reply2 = h2.handle("今天过得怎么样", user_id="OWNER")
h2.note_reply_delivery(reply2[:4])
row = h2.store.all_messages(h2.instance_no)[-1]
check("2a 记忆里保存的是实际送达的那部分", row["content"] == reply2[:4],
      f"{row['content']!r} vs {reply2[:4]!r}")
check("2b 留了'发送不完整'档案",
      any("发送不完整" in (r["title"] or "") for r in h2.store.list_diary(kind="terminal", limit=3)))

print("\n== 3. 非 LLMError 异常：handle() 只捕获 LLMError ==")
h3 = fresh("boom", Boom(env("boom").model))
try:
    h3.handle("在吗", user_id="OWNER")
    check("3a 非 LLMError 也被兜住（不抛异常）", True)
    txt = ""
except Exception as exc:
    txt = f"{type(exc).__name__}: {exc}"
    check("3a 非 LLMError 也被兜住（不抛异常）", False, txt)
print(f"      handle() 抛出：{txt or '（无）'}")
rows3 = h3.store.all_messages(h3.instance_no)
print(f"      抛出后 DB 状态：{[(r['role'], r['content'][:12]) for r in rows3]}")

print("\n== 4. 先写了记忆、最后一轮模型挂了 ==")
h4 = fresh("writethenfail", WriteThenFail(env("writethenfail").model))
reply4 = h4.handle("我答应你一件事", user_id="OWNER")
mems = [m["summary"] for m in h4.store.active_memories(h4.instance_no, view=Store.ALL_SCOPES)]
check("4a 记忆写进去了（工具那一轮成功）", any(SECRET in s for s in mems), str(mems))
check("4b 回复走了失败兜底", reply4 in FAILURE_LINES, reply4[:30])
print(f"      → 结果：她记住了「{mems}」，但用户只看到「{reply4[:20]}」")

print("\n== 5. 空回复 / 空输入 ==")
h5 = fresh("empty", Empty(env("empty").model))
r5 = h5.handle("   ", user_id="OWNER")
check("5a 空白输入直接返回空，不落库", r5 == "" and h5.store.count_messages(h5.instance_no) == 0,
      f"{r5!r} rows={h5.store.count_messages(h5.instance_no)}")
r5b = h5.handle("在吗", user_id="OWNER")
rows5 = h5.store.all_messages(h5.instance_no)
# ★ 期望翻转（lead 授权，2026-10-10）：原来这条断言"模型返回空串时仍会写一条空的 assistant
#   记录"（observed 缺陷：她"以为自己说了句空话"，note_reply_delivery("") 还会因为
#   "" == "" 把空记录当成"完整送达"）。修后空回复用 FAILURE_LINES 兜底：既不写空记录，
#   也不把空串当成送达文本。正向对照见 5a（空白**输入**仍然直接返回空、不落库）。
check("5b ★ 模型返回空串 → 不写空的 assistant 记录，落库的是 FAILURE_LINES 兜底台词",
      len(rows5) == 2 and rows5[-1]["content"] in FAILURE_LINES
      and str(rows5[-1]["content"]).strip() != "",
      str([(r["role"], r["content"][:16]) for r in rows5]))
print(f"      空回复被兜底成的文本：{r5b!r}")

print("\n== 6. runs.jsonl 不记对话内容（README 3.7 的宣称）==")
h6 = fresh("runs", Flaky(env("runs").model, fail_times=0))
h6.handle(f"我的秘密是 {SECRET}，别告诉别人", user_id="OWNER")
runs = h6.settings.db_path.parent / "runs.jsonl"
txt = runs.read_text(encoding="utf-8") if runs.exists() else ""
check("6a runs.jsonl 存在且有行", bool(txt.strip()), str(runs))
check("6b ★ 运行档案里没有对话内容", SECRET not in txt, txt.strip()[:120])
print(f"      最后一行：{txt.strip().splitlines()[-1] if txt.strip() else '(空)'}")

print("\n== 7. 记忆整理中途 DB 失败：会不会留下半写状态 ==")


class TwoDrafts(MockLLM):
    """固定产出两条互不相似的记忆草稿。"""

    def extract_memories(self, transcript, **kw):
        from giftia.llm import MemoryDraft

        return [
            MemoryDraft(summary="阿伟要去北京出差半个月", importance=0.6),
            MemoryDraft(summary="阿伟家的猫叫豆豆，很怕生", importance=0.6),
        ]


h7 = Giftia(env("halfwrite"), llm=TwoDrafts(env("halfwrite").model))
h7.set_handler_id("OWNER")
for i in range(2):
    h7.handle(f"阿伟说他第{i}次要出差，要去一个很远的地方办事", user_id="STRANGER",
              scene="group", group_id="GH", speaker_name="阿伟", log_only=True, mid=f"h{i}")
open_before = len(h7.store.open_messages(h7.instance_no))
real = h7.store.add_memory
calls = {"n": 0}


def flaky_add(*a, **kw):
    calls["n"] += 1
    if calls["n"] == 2:
        raise __import__("sqlite3").OperationalError("database is locked")
    return real(*a, **kw)


h7.store.add_memory = flaky_add  # type: ignore[method-assign]
try:
    h7.consolidate(force=True)
    raised = ""
except Exception as exc:
    raised = f"{type(exc).__name__}: {exc}"
h7.store.add_memory = real  # type: ignore[method-assign]
mems_after = [m["summary"] for m in h7.store.active_memories(h7.instance_no, view=Store.ALL_SCOPES)]
open_after = len(h7.store.open_messages(h7.instance_no))
print(f"      add_memory 调用次数={calls['n']}，consolidate 抛出：{raised or '（无）'}")
print(f"      已写进记忆：{mems_after}；仍处于未整理的消息：{open_before} -> {open_after}")
# ★ 期望翻转（lead 授权，2026-10-10）：原来这条断言"半写：第一条记忆已经落库、消息却没标已整理
#   （没有事务）"——那是 P1-7 的缺陷。修后 consolidate 的"写记忆 + 标消息 + 推游标"在一个
#   事务里（team-store），中途失败整批回滚：一条记忆都不该留下，消息仍然是未整理状态，
#   异常照旧抛出来（fail-loud，调用方知情）。正向对照见 7b：重试后能正常落库且不重复。
check("7a ★ 事务回滚：失败时一条记忆都没落库、消息仍未整理、异常照旧抛出",
      not mems_after and open_after > 0 and bool(raised),
      f"raised={raised!r} mems={mems_after} open={open_after}")
added2 = h7.consolidate(force=True)
mems2 = [m["summary"] for m in h7.store.active_memories(h7.instance_no, view=Store.ALL_SCOPES)]
check("7b 重试后靠去重没有产生重复条目", len(mems2) == len(set(mems2)),
      f"added={added2} 条数={len(mems2)} 去重后={len(set(mems2))}")

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
