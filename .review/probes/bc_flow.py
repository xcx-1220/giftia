#!/usr/bin/env python
"""Stream B 探针 5：流水线的适配器分支（离线 dry-run，不连平台）。

覆盖：未绑定时的认主/指令闸门、陌生人私聊、转发(102)、聚批、旁听是否进上下文。
    python .review\\probes\\bc_flow.py
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
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

from adapters.qq_official import DEFLECT, QQBot, extract_content  # noqa: E402
from giftia import brain as brain_mod  # noqa: E402
from giftia.config import load_settings  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402
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


class Count(MockLLM):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.total = 0

    def chat(self, messages, **kw):
        self.total += 1
        return super().chat(messages, **kw)

    def chat_with_tools(self, messages, *, tools=None, **kw):
        self.total += 1
        return super().chat_with_tools(messages, tools=tools, **kw)

    def extract_memories(self, transcript, **kw):
        self.total += 1
        return super().extract_memories(transcript, **kw)

    def extract_self_name(self, text):
        self.total += 1
        return super().extract_self_name(text)


def env(name: str, hours: float = 81920.0, **mem_over):
    tag = f"{os.getpid()}-{int(time.time())}"
    d = ROOT / ".review" / "tmp" / f"brain-checker-{name}-{tag}"
    d.mkdir(parents=True, exist_ok=True)
    base = load_settings(lifespan_override=hours, force_mock=True)
    mem = dataclasses.replace(base.memory, **mem_over) if mem_over else base.memory
    return dataclasses.replace(base, root=d, memory=mem)


def new_bot(name, **rt_over):
    st = env(name)
    if rt_over:
        st = dataclasses.replace(st, runtime=dataclasses.replace(st.runtime, **rt_over))
    brain_mod.reset_default()
    hub = brain_mod.get_default(st)
    hub.llm = Count(st.model)
    bot = QQBot("appid", "secret", dry_run=True, settings=st, owner_openid="")
    return st, hub, bot


def group_event(i, content, uid="STRANGER", who="阿伟", mentions=None, role=""):
    return {
        "id": f"gm{i}", "content": content, "group_openid": "G-CMD",
        "author": {"user_openid": uid, "username": who, "member_role": role},
        "mentions": mentions or [],
    }


# ══ 0. 前置可达性：本地 CLI 的私聊记忆落在 private，并且第一条私聊会认主 ══
# ★ 期望翻转（lead 授权，2026-10-10）：原来这里断言"CLI 聊过之后 handler_id 仍然是空的"，
#   那是 P0-3a 的入口之一（未绑定 = 谁都能在群里 /profile 出来那批记忆）。
#   修后核心 path 也会**认主**（`_auto_bind`，与适配器同一个 [runtime] auto_bind 开关），
#   所以 handler_id 会被写上本地哨兵值 —— 这是设计行为，不是泄漏：认主必经 §二 的遮蔽。
print("== 0. 前置：本地 CLI（run.py → cli.py:85 brain.handle(text)）也会认主 ==")
st0, hub0, bot0 = new_bot("cli_pre")
hub0.handle("我今天加班到很晚，回来路上下了点雨")   # 不带 user_id，等价 cli.py:85
hub0.consolidate(force=True)
scopes0 = [m["scope"] for m in hub0.store.active_memories(hub0.instance_no,
                                                          view=Store.ALL_SCOPES)]
check("0a ★ 本地 CLI 第一条私聊会认主（核心认主，与适配器同一 auto_bind 开关）",
      hub0.handler_id == "handler", repr(hub0.handler_id))
check("0b 记忆落在 private（P0-3b 的遮蔽下界保证换人后看不到）", set(scopes0) == {"private"},
      str(scopes0))
check("0c 认主之后这一轮走搭档口径（is_handler=True、搭档视角）",
      hub0.is_handler is True
      and hub0.memory_view == [("private", ""), ("person", "handler"), ("group", "*")],
      f"is_handler={hub0.is_handler} view={hub0.memory_view}")

# ══ 1. 未绑定状态：指令闸门是否放行陌生人 ═══════════════════════
# ★ 期望翻转（lead 授权，2026-10-10）：这一节原来断言"未绑定时陌生人的 /profile 被执行、
#   能 /forget 掉搭档的记忆、能 /impress 写东西"——那是 P0-3a 的缺陷本身。
#   修后适配器采用白名单语义（qq_official.handle_command ①②③），下面是新契约下的正确行为。
print("\n== 1. 未绑定（handler_id='') 时群里的指令闸门 ==")
st, hub, bot = new_bot("cmd_unbound", response_tier=0, wake_delay_seconds=0.0)
hub.store.add_memory(instance_no=hub.instance_no, created_ts=hub.clock.iso(),
                     happened_ts=hub.clock.iso(),
                     summary=f"搭档的秘密 {SECRET}：他下周三要考驾照",
                     importance=0.9, layer="important", scope="private", subject_id="",
                     about="椎名")
mem_id = hub.store.active_memories(hub.instance_no, view=Store.ALL_SCOPES)[0]["id"]
check("1a 前置：已有一条搭档私事，且当前未绑定", hub.handler_id == "", repr(hub.handler_id))

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    handled = bot.handle_command("group", "G-CMD", "/profile", "STRANGER", "m1")
out = buf.getvalue()
print(f"      陌生人 /profile 被当成指令处理：{handled}")
check("1b 指令仍然被消费（回一句婉拒，不再交给她当聊天）", handled is True)
check("1c ★ 未绑定时陌生人的 /profile 不再输出任何档案（契约 §三.4：群里一律拒绝）",
      SECRET not in out, out.strip().splitlines()[-1][:80] if out.strip() else "(无输出)")
print(f"      她回的是：{out.strip().splitlines()[-1][:60] if out.strip() else '(无输出)'}")

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    bot.handle_command("group", "G-CMD", f"/forget {mem_id}", "STRANGER", "m2")
alive = any(int(m["id"]) == mem_id
            for m in hub.store.active_memories(hub.instance_no, view=Store.ALL_SCOPES))
check("1d ★ 未绑定时陌生人 /forget 无效：那条记忆还在", alive is True,
      f"memory #{mem_id} 还在={alive}")

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    bot.handle_command("group", "G-CMD", "/impress 张三 他欠我钱", "STRANGER", "m3")
imp = [m for m in hub.store.active_memories(hub.instance_no, view=Store.ALL_SCOPES)
       if "欠我钱" in str(m["summary"])]
check("1e ★ 未绑定时陌生人 /impress 无效：什么都没写进去", not imp, str(len(imp)))

# 正向对照：闸门判的是**身份**，不是"一律拒绝"——同一个人 /bind 之后，私聊里就查得到档案。
# （这也是 P0-3b 的对照：绑定之后写下的记忆看得见；绑定之前留下的仍然被遮蔽。）
print("      对照：同一个人 /bind 之后再查（c2c）")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    bot.handle_command("c2c", "STRANGER", "/bind", "STRANGER", "m4")
check("1f 未绑定时 /bind 仍然可用（白名单：/bind、/help、/unbind）",
      hub.handler_id == "STRANGER", repr(hub.handler_id))
hub.add_impression("阿伟", "他欠我钱")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    bot.handle_command("c2c", "STRANGER", "/profile", "STRANGER", "m5")
check("1g 搭档本人在私聊里 /profile 照常输出档案（闸门不是全拒）",
      "欠我钱" in buf.getvalue(), buf.getvalue().strip()[-60:])

# ══ 2. 群里的指令要先 @ 她，而且群里不输出"看记忆"的视角 ════════
# ★ 期望翻转（lead 授权，2026-10-10）：原来这里断言"未被 @ 的群里 /profile 也被执行了
#   （没走触发判定）"——那是缺陷。修后群里的指令要**先被 @**才进指令处理，
#   未被 @ 的 `/` 消息只旁听；被 @ 之后也只会拿到群聊婉拒（契约 §三.4）。
print("\n== 2. 群里的指令要先 @ 她（未被 @ 的 / 消息只旁听）==")
st_u, hub_u, bot_u = new_bot("cmdat", response_tier=0, wake_delay_seconds=0.0)
hub_u.store.add_memory(instance_no=hub_u.instance_no, created_ts=hub_u.clock.iso(),
                       happened_ts=hub_u.clock.iso(),
                       summary=f"搭档的秘密 {SECRET}：他下周三要考驾照",
                       importance=0.9, scope="private", subject_id="", about="椎名")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    bot_u.handle_event("GROUP_MESSAGE_CREATE",
                       group_event(90, "/profile", uid="STRANGER", who="阿伟"))  # 没有 mentions
out_u = buf.getvalue()
replied_u = any(f"[{bot_u.settings.persona.name}]" in l for l in out_u.splitlines())
check("2a ★ 未被 @ 的群指令不再被执行：不下档案、也不回答（走旁听）",
      SECRET not in out_u and not replied_u, out_u.strip()[:200])

# 正向对照：被 @ 之后同一条指令会进指令处理（拿到的是群聊婉拒，仍然不输出档案）
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    bot_u.handle_event("GROUP_AT_MESSAGE_CREATE",
                       group_event(91, "/profile", uid="STRANGER", who="阿伟",
                                   mentions=[{"bot": True}]))
out_at = buf.getvalue()
check("2a' 对照：被 @ 之后同一条 /profile 进入指令处理（回群聊婉拒，仍不输出档案）",
      "私下说" in out_at and SECRET not in out_at, out_at.strip()[-80:])

print("\n== 2b. 已绑定时同一个人在群里发同样的指令（对照组）==")
st, hub, bot = new_bot("cmd_bound", response_tier=0, wake_delay_seconds=0.0)
hub.set_handler_id("OWNER")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    bot.handle_command("group", "G-CMD", "/profile", "STRANGER", "m4")
check("2b 群里回的是群聊婉拒「……这个我们私下说。」（契约 §三.4：这些指令只在私聊里可用）",
      "私下说" in buf.getvalue(), buf.getvalue().strip()[-60:])

# ══ 3. 陌生人私聊：绑定前 vs 绑定后 ════════════════════════════
print("\n== 3. 陌生人私聊（自动认主开关）==")
st3, hub3, bot3 = new_bot("autobind")
pre = hub3.llm.total
bot3.handle_event("C2C_MESSAGE_CREATE", {"id": "c1", "content": "你好呀，我叫小雨",
                                         "author": {"user_openid": "STRANGER-Z",
                                                    "username": "小雨"}})
check("3a ★ 未绑定时陌生人私聊被自动认主成搭档", hub3.handler_id == "STRANGER-Z",
      repr(hub3.handler_id))
check("3b 认主后她按搭档口径回复（消耗了一次模型调用）", hub3.llm.total > pre)

st4, hub4, bot4 = new_bot("deflect")
hub4.set_handler_id("OWNER")
pre = hub4.llm.total
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    bot4.handle_event("C2C_MESSAGE_CREATE", {"id": "c2", "content": "你好呀",
                                             "author": {"user_openid": "STRANGER-Z",
                                                        "username": "小雨"}})
check("3c 已绑定时陌生人的私聊被挡（不调模型）", hub4.llm.total == pre,
      f"{pre} -> {hub4.llm.total}")
check("3d 挡回去的是 DEFLECT 台词", DEFLECT.strip()[:8] in buf.getvalue(),
      buf.getvalue()[-80:])

# ══ 4. 转发消息（message_type=102 / msg_elements）════════════
print("\n== 4. 转发消息 message_type=102（msg_elements）==")
fwd = {"content": "", "msg_elements": [
    {"content_type": 1, "content": "【转发的聊天记录】A：我下周三要离婚"},
    {"content_type": 1, "content": "B：你别冲动", "msg_elements": [{"content": "C：劝你冷静"}]},
]}
got = extract_content(fwd)
check("4a 转发内容被取出来（含嵌套）", "离婚" in got and "冷静" in got, got.replace("\n", " | "))
check("4b 转发正文被当成这个人的话", "A：" in got)

st5, hub5, bot5 = new_bot("forward", response_tier=0, wake_delay_seconds=0.0)
hub5.set_handler_id("OWNER")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    bot5.handle_event("GROUP_MESSAGE_CREATE",
                      group_event(1, extract_content(fwd), uid="STRANGER", who="阿伟"))
rows = hub5.store.all_messages(hub5.instance_no)
check("4c tier=0 时转发消息只旁听（不调模型）", hub5.llm.total == 0, str(hub5.llm.total))
check("4d 转发内容落在群归属下", rows and (rows[0]["scope"], rows[0]["subject_id"]) == ("group", "G-CMD"),
      str([(r["scope"], r["subject_id"]) for r in rows]))

st6, hub6, bot6 = new_bot("forward2", response_tier=100, wake_delay_seconds=0.0)
hub6.set_handler_id("OWNER")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    bot6.handle_event("GROUP_MESSAGE_CREATE",
                      group_event(2, extract_content(fwd), uid="STRANGER", who="阿伟"))
check("4e tier=100 时她会接转发的内容（照实转述进来）", "转发的聊天记录" in buf.getvalue(),
      buf.getvalue()[-100:])

# ══ 5. 聚批：前面几句旁听、最后一句回复 ════════════════════════
print("\n== 5. 聚批（wake_delay_seconds=0.3）==")
st7, hub7, bot7 = new_bot("batch", response_tier=100, wake_delay_seconds=0.3)
hub7.set_handler_id("OWNER")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    bot7.handle_event("GROUP_MESSAGE_CREATE", group_event(10, "第一句：周末去哪玩", uid="STRANGER", who="阿伟"))
    bot7.handle_event("GROUP_MESSAGE_CREATE", group_event(11, "第二句：我想去爬山", uid="STRANGER", who="阿伟"))
    time.sleep(0.35)
    bot7._flush_batches()
rows = hub7.store.all_messages(hub7.instance_no)
roles = [r["role"] for r in rows]
scopes = {(r["scope"], r["subject_id"]) for r in rows}
check("5a 聚批后：两句 user + 一条回复", roles.count("user") == 2 and roles.count("assistant") == 1,
      str(roles))
check("5b 前几句也是群归属落库", scopes == {("group", "G-CMD")}, str(scopes))
check("5c 批次只回一次（一条 assistant）", roles.count("assistant") == 1, str(roles))
# 旁听的那句在下一轮上下文里看得到
pre = hub7.llm.total
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    bot7.handle_event("GROUP_AT_MESSAGE_CREATE",
                      group_event(12, "刚才聊到哪了", uid="OWNER", who="椎名",
                                  mentions=[{"bot": True}]))
sys_prompt = ""
with contextlib.redirect_stdout(io.StringIO()):
    sys_prompt = hub7._prepare("刚才聊到哪了")[0][0]["content"] if False else ""
hist = [m for m in hub7.store.recent_messages(hub7.session_id, 40)]
check("5d 旁听的句子进了她的工作记忆（最近消息里有）",
      any("爬山" in str(m["content"]) for m in hist), str([m["content"][:10] for m in hist]))

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
brain_mod.reset_default()
sys.exit(1 if FAIL else 0)
