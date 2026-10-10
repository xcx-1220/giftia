#!/usr/bin/env python
"""team-brain 修复验收探针：编排与隐私闸门层（P0-3a/b、P0-4、P1-3/4/5/12/13/14）。

    python .review\\probes\\team_brain_fixes.py

断言写的是**修好之后应有的状态**（[PASS] = 这条修好了）。
自建 .review\\tmp\\team-brain-<name>-<pid>\\ 下的库，从不碰 data/giftia.db。
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from giftia import agent_tools  # noqa: E402
from giftia.brain import FAILURE_LINES, Giftia, LOCAL_USER_ID  # noqa: E402
from giftia.config import load_settings  # noqa: E402
from giftia.llm import ChatReply, MockLLM, ToolCall  # noqa: E402
from giftia.store import Store  # noqa: E402

PASS = FAIL = 0
TMPROOT = ROOT / ".review" / "tmp" / f"team-brain-fixes-{__import__('os').getpid()}"
BASE = load_settings(lifespan_override=81920.0, force_mock=True)
SECRET_A = "S3CR3T-ALPHA-7731"


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  → {detail}" if detail else ""))


def env(name: str, **rt):
    d = TMPROOT / name
    d.mkdir(parents=True, exist_ok=True)
    base = dataclasses.replace(BASE, root=d)
    if rt:
        base = dataclasses.replace(base, runtime=dataclasses.replace(base.runtime, **rt))
    return base


class Rec(MockLLM):
    """记下每一轮真正发出去的 system。"""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.systems: list[str] = []

    def chat(self, messages, **kw):
        self.systems.append(str(messages[0]["content"]))
        return super().chat(messages, **kw)

    def chat_with_tools(self, messages, *, tools=None, **kw):
        self.systems.append(str(messages[0]["content"]))
        return super().chat_with_tools(messages, tools=tools, **kw)

    @property
    def system(self) -> str:
        return self.systems[-1] if self.systems else ""


class Count(MockLLM):
    """数模型调用（P1-14 的"零 token"）。"""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.calls = {"chat": 0, "tools": 0, "extract": 0, "name": 0}

    def chat(self, messages, **kw):
        self.calls["chat"] += 1
        return super().chat(messages, **kw)

    def chat_with_tools(self, messages, *, tools=None, **kw):
        self.calls["tools"] += 1
        return super().chat_with_tools(messages, tools=tools, **kw)

    def extract_memories(self, transcript, **kw):
        self.calls["extract"] += 1
        return super().extract_memories(transcript, **kw)

    def extract_self_name(self, text):
        self.calls["name"] += 1
        return super().extract_self_name(text)


def blocks(system: str, head: str) -> str:
    i = system.find(head)
    if i < 0:
        return ""
    j = system.find("\n【", i + 1)
    return system[i : j if j > 0 else len(system)]


# ══ A. P0-3b：换人之后，新搭档看不到前任的私事 ══════════════════════
print("== A. P0-3b 绑定时刻遮蔽（换人 / 同一任）==")
a = Giftia(env("rebind"), llm=Rec(env("rebind").model))
a.set_handler_id("PARTNER-A")
print(f"      首次绑定（库里没有 private 记忆）→ handler_bound_at = {a.handler_bound_at!r}")
check("首次绑定不设遮蔽下界", a.handler_bound_at == "")
a.handle(f"我叫小林，我的保险箱密码是 {SECRET_A}，别跟别人说", "PARTNER-A")
a.consolidate(force=True)
check("A 的私事已经落成 private 记忆",
      any(SECRET_A in str(m["summary"])
          for m in a.store.active_memories(1, view=[("private", "")])))
check("★ 同一任搭档自己说的话照旧记得（遮蔽不误伤）",
      SECRET_A in a.preview_prompt("我的密码是什么"))

# 换人：/unbind → 陌生人私聊（中性内容，不含秘密）
a.set_handler_id("")
check("解绑后 handler_id 为空", a.handler_id == "")
a.handle("你好，我是新来的", "PARTNER-B", scene="c2c")
check("新搭档认主成功（owner 非空才走搭档路径）", a.handler_id == "PARTNER-B", a.handler_id)
print(f"      换人后 handler_bound_at = {a.handler_bound_at!r}")
check("换人时设了遮蔽下界", bool(a.handler_bound_at))
prompt_b = a.preview_prompt("你还记得我的密码吗")
check("★ 换人后新人看不到前任的私事（记忆检索不漏）", SECRET_A not in prompt_b)
check("★ 换人后新人看不到前任的对话窗口（工作记忆也不漏）", SECRET_A not in prompt_b)
check("★ /profile 也不吐前任私事（搭档视角同样过遮蔽）",
      SECRET_A not in a.profile_text(""))
a.handle("我叫小周，我下周要搬家", "PARTNER-B")
a.consolidate(force=True)
check("新人自己的事照旧记得", "搬家" in a.preview_prompt("我下周要做什么"))

# 大日子也是从记忆派生的：换人后不该被提醒前任的事
b = Giftia(env("rebind_occ"), llm=Rec(env("rebind_occ").model))
b.set_handler_id("PARTNER-A")
tomorrow = (b.clock.now().date() + timedelta(days=1)).isoformat()
b.store.add_memory(instance_no=1, created_ts=b.clock.iso(), happened_ts=b.clock.iso(),
                   summary="前任明天要去面试", importance=0.8, layer="important",
                   scope="private", subject_id="", event_date=tomorrow, about="搭档")
b.set_handler_id("")
b.handle("你好", "PARTNER-B", scene="c2c")
b._prepare("在吗", mark=False)
occ_titles = [o.title for o in b.last_occasions]
check("★ 换人后新搭档的大日子里没有前任的事", not any("面试" in t for t in occ_titles),
      str(occ_titles))

# 连续换人：每一任都只属于自己的那一段
c = Giftia(env("rebind2"), llm=Rec(env("rebind2").model))
c.set_handler_id("A")
c.handle(f"A 的秘密是 {SECRET_A}", "A")
c.consolidate(force=True)
c.set_handler_id("")                       # A 走了
c.set_handler_id("A")                      # 同一个人回来：他记得自己的过去
check("★ 同一个人 /unbind 之后回来，仍看得到自己说过的话",
      SECRET_A in c.preview_prompt("我的秘密是什么"))
c.set_handler_id("")
c.set_handler_id("B")                      # 换人：B 看不到 A 的
check("★ 第二任看不到第一任的私事", SECRET_A not in c.preview_prompt("我的秘密是什么"))
c.handle("B 的秘密是 S3CR3T-BRAVO-9", "B")
c.consolidate(force=True)
c.set_handler_id("")
c.set_handler_id("C")                      # 再换人：C 两任的都不该看到
prompt_c = c.preview_prompt("有什么秘密吗")
check("★ 第三任既看不到第一任、也看不到第二任的私事",
      SECRET_A not in prompt_c and "S3CR3T-BRAVO-9" not in prompt_c)

# ══ B. P0-3a / §三：未绑定（auto_bind=false）走未确认路径 ═══════════
print("\n== B. 未绑定 + auto_bind=false：私聊不是搭档 ==")
g = Giftia(env("unbound", auto_bind=False), llm=MockLLM(BASE.model))
g.handle(f"我叫小林，我的保险箱密码是 {SECRET_A}", "STRANGER-1", scene="c2c")
check("未绑定私聊不再默认是搭档（is_handler=False）", g.is_handler is False)
check("没有认主（handler_id 仍为空）", g.handler_id == "")
check("没有写 private 记忆",
      g.store.active_memories(1, view=[("private", "")]) == [])
check("记忆视角是「他自己」而不是搭档（person）", g.memory_view == [("person", "STRANGER-1")],
      str(g.memory_view))
check("★ 这种状态下工具不开放", agent_tools.available(g)[0] is False
      and agent_tools.specs(g) == [], str(agent_tools.available(g)))
check("★ 就算硬调也被拒", "不能用记忆工具" in agent_tools.execute(
    g, "remember", json.dumps({"content": "偷写一条"}, ensure_ascii=False)))
check("不推进关系（亲密度没动）", g.current_bond().affinity == 0.0,
      str(g.current_bond().affinity))
g.consolidate(force=True)
scopes = {str(m.get("scope")) for m in g.store.active_memories(1, view=Store.ALL_SCOPES)}
check("整理出来的记忆归属不是 private", "private" not in scopes, str(scopes))

# ══ C. P0-4：群发提示词不混别人的私聊大日子 ════════════════════════
print("\n== C. P0-4 群发（compose_outreach）视角一致 ==")
o = Giftia(env("outreach"), llm=Rec(env("outreach").model))
o.settings = dataclasses.replace(
    o.settings, outreach=dataclasses.replace(o.settings.outreach, enabled=True, target="group")
)
o.set_handler_id("OWNER")
o.set_last_group("GROUP-1")
for summary, scope, subject, about in (
    ("搭档明天要去面试", "private", "", "搭档"),
    ("SECRET-BRAVO 小雨明天要去做手术", "person", "USER-XIAOYU", "小雨"),
    ("阿伟明天要考驾照", "group", "GROUP-1", "阿伟"),
    ("小刚明天要搬家", "group", "GROUP-OTHER", "小刚"),
):
    o.store.add_memory(instance_no=1, created_ts=o.clock.iso(), happened_ts=o.clock.iso(),
                       summary=summary, importance=0.8, layer="important", scope=scope,
                       subject_id=subject, event_date=tomorrow, about=about)
plan_, text = o.compose_outreach(record=False, force=True, target="group")
leaks = [kw for kw in ("搭档明天要去面试", "小雨明天要去做手术", "小刚明天要搬家")
         if kw in o.llm.system]
check("★ 群发提示词只含本群公开的大日子", not leaks, str(leaks))
check("本群那条照旧在", "阿伟明天要考驾照" in o.llm.system)

# ══ D. P1-13：注入路径的印象块只带在场的人 ═════════════════════════
print("\n== D. P1-13 印象块在场闸门（注入路径）==")
p = Giftia(env("impress"), llm=Rec(env("impress").model))
p.set_handler_id("OWNER")
p.set_handler_name("椎名")
people = [f"成员{i}" for i in range(15)]
for who in people:
    for k in range(2):
        p.store.add_memory(instance_no=1, created_ts=p.clock.iso(), happened_ts=p.clock.iso(),
                           summary=f"{who}提过他{k}号要去外地出差", importance=0.6,
                           layer="normal", scope="group", subject_id="GK", about=who)
p.handle("大家好", user_id="OWNER", scene="group", group_id="GK", speaker_name="椎名",
         is_handler=True)
p.handle("我随便说一句，今天天气不错", user_id="U3", scene="group", group_id="GK",
         speaker_name="成员3", is_handler=False)
imp_group = blocks(p.llm.system, "【你对他们的印象】")
absent = [w for w in people if w != "成员3" and w in imp_group]
check("★ 群聊注入的印象块只带在场的人", not absent, str(absent[:5]))
check("在场的那位还在", "成员3" in imp_group)
p.handle("今天怎么样", user_id="OWNER")
imp_priv = blocks(p.llm.system, "【你对他们的印象】")
check("私聊里仍然带群成员的印象（你本来就有权知道）",
      any(w in imp_priv for w in people), imp_priv[:60].replace("\n", " / "))
check("直接调用（查看整份档案）仍然是按人封顶的全量",
      "成员0：" in p.impressions_block("你好", per_person=3, max_people=15))

# ══ E. P1-12：花名册按群隔离 + 硬上限 ════════════════════════════
print("\n== E. P1-12 花名册群隔离与上限 ==")
r = Giftia(env("roster"), llm=Rec(env("roster").model))
r.set_handler_id("OWNER")
r.set_handler_name("椎名")
r.handle("我是阿伟", user_id="U-A", scene="group", group_id="GB", speaker_name="阿伟")
r.handle("在吗", user_id="OWNER", scene="group", group_id="GA", speaker_name="椎名",
         is_handler=True)
roster = blocks(r.llm.system, "【群里你认得的人】")
check("★ 群 A 的花名册里没有只在群 B 出现过的人", "阿伟" not in roster,
      roster.replace("\n", " | ")[:100])
check("搭档仍然在第一行", "椎名" in roster and "（你的搭档）" in roster)
for i in range(60):
    r.clock.advance(1 / 3600.0)   # 每人隔一秒，"最近活跃"才有区分度
    r.handle(f"大家好，我叫甲{i}", user_id=f"U-{i}", scene="group", group_id="GA",
             speaker_name=f"甲{i}")
roster2 = blocks(r.llm.system, "【群里你认得的人】")
n_rows = len([ln for ln in roster2.splitlines() if ln.startswith("- ")])
check("★ 花名册有硬上限（≤ 30 行）", n_rows <= 30, f"{n_rows} 行")
check("按最近活跃排序（最后认识的在名单里）", "甲59" in roster2, roster2[-80:])
check("最先认识的人被挤出去了", "甲0\n" not in roster2 and "- 甲0" not in roster2,
      roster2[:120].replace("\n", " | "))
print(f"      60 人之后花名册 {n_rows} 行 / {len(roster2)} 字")

# ══ F. P1-14：旁听不花 token，攒着交给搭档那一轮 ══════════════════
print("\n== F. P1-14「零 token」与旁听整理的接续 ==")
f = Giftia(env("listen"), llm=Count(BASE.model))
f.set_handler_id("OWNER")
f.llm.calls = dict.fromkeys(f.llm.calls, 0)
f.handle("我是来看热闹的", "F-1", scene="group", group_id="G-9", speaker_name="路人",
         log_only=True)
check("★ 旁听不调模型（含名字判定的模型兜底）",
      all(v == 0 for v in f.llm.calls.values()), str(f.llm.calls))
check("旁听仍然记下了这句话", f.store.count_messages(1) == 1)
check("旁听留了待整理标记", bool(f._listen_pending()), str(f._listen_pending()))
for i in range(8):
    f.handle(f"群里第{i}句闲聊，说的是周六去打球的事", f"F-{i}", scene="group",
             group_id="G-9", speaker_name=f"群友{i}", log_only=True)
check("★ 攒够阈值也不当场整理（没有 extract 调用）", f.llm.calls["extract"] == 0,
      str(f.llm.calls))
before = len(f.store.active_memories(1, view=[("group", "G-9")]))
f.handle("在吗", "OWNER")           # 搭档那一轮
after = len(f.store.active_memories(1, view=[("group", "G-9")]))
check("★ 搭档那一轮把旁听补整理成记忆（群聊记忆不会永远不生成）", after > before,
      f"{before} → {after}；calls={f.llm.calls}")
check("整理完标记清掉了", not f._listen_pending())
check("这些记忆归属是群", all(str(m.get("scope")) == "group"
                              for m in f.store.active_memories(1, view=[("group", "G-9")])))

# ══ G. P1-3 / P1-4：异常兜底，一轮一定有回复 ══════════════════════
print("\n== G. P1-3 / P1-4 异常不再冒穿 ==")


class ToolRemember(MockLLM):
    is_mock = False

    def chat_with_tools(self, messages, *, tools=None, **kw):
        return ChatReply(tool_calls=[ToolCall(id="c1", name="remember",
                                             arguments=json.dumps({"content": "这条写不进去"},
                                                                  ensure_ascii=False))])

    def chat(self, messages, **kw):
        return "……嗯，我在听。"


class Boom(MockLLM):
    def chat(self, messages, **kw):
        raise ValueError("模型层冒出来的莫名异常")


class Empty(MockLLM):
    def chat(self, messages, **kw):
        return "   "


h = Giftia(env("dbfail"), llm=ToolRemember(BASE.model))
h.set_handler_id("OWNER")
real_add = h.store.add_memory


def boom_add(*args, **kwargs):
    raise sqlite3.OperationalError("database is locked")


h.store.add_memory = boom_add  # type: ignore[method-assign]
try:
    out = agent_tools.execute(h, "remember", json.dumps({"content": "这条写不进去"},
                                                       ensure_ascii=False))
    check("★ execute 在 DB 写失败时仍然返回文本（承诺成立）", bool(out), repr(out)[:50])
    reply = h.handle("我要记一笔", "OWNER")
    check("★ DB 写失败时 handle() 不抛异常且这一轮有回复", bool(reply.strip()), repr(reply)[:40])
    check("失败有留痕（notices 里有 [内部错误]）",
          any("[内部错误]" in n for n in h.notices), str(h.notices[:2]))
except Exception as exc:  # noqa: BLE001
    check("★ DB 写失败时 handle() 不抛异常且这一轮有回复", False,
          f"{type(exc).__name__}: {exc}")
h.store.add_memory = real_add  # type: ignore[method-assign]
check("失败记进了 diary",
      any("工具失败" in str(r.get("title") or "")
          for r in h.store.list_diary(kind="terminal", limit=5)),
      str([r.get("title") for r in h.store.list_diary(kind="terminal", limit=5)]))

hb = Giftia(env("boom"), llm=Boom(BASE.model))
hb.set_handler_id("OWNER")
try:
    r2 = hb.handle("在吗", "OWNER")
    check("★ 非 LLMError 也被兜住（不抛异常）", True)
    check("兜底回复是戏内台词", r2 in FAILURE_LINES, r2[:30])
    check("非 LLMError 有留痕", any("[内部错误]" in n for n in hb.notices), str(hb.notices[:2]))
except Exception as exc:  # noqa: BLE001
    check("★ 非 LLMError 也被兜住（不抛异常）", False, f"{type(exc).__name__}: {exc}")

he = Giftia(env("empty"), llm=Empty(BASE.model))
he.set_handler_id("OWNER")
r3 = he.handle("在吗", "OWNER")
rows = he.store.all_messages(1)
check("★ 空回复用 FAILURE_LINES 兜底（不写空的 assistant 记录）",
      r3 in FAILURE_LINES and rows and str(rows[-1]["content"]).strip() == r3,
      f"{r3[:20]!r} / {[(x['role'], x['content'][:8]) for x in rows]}")

# ══ H. P1-5：回填失败不再静默，也不留"已完成"标记 ════════════════
print("\n== H. P1-5 隐私迁移失败可观测、可重试 ==")
hs = env("backfill")
st = Store(hs.db_path)
st.meta_set("msg_scope_backfill_v2", 1)     # 假装上次已经标过"完成"
st.meta_set("msg_scope_backfilled", 1)
st.close()
orig_bf = Store.backfill_message_scopes


def boom_bf(self):
    raise sqlite3.OperationalError("database is locked")


Store.backfill_message_scopes = boom_bf  # type: ignore[method-assign]
try:
    hbf = Giftia(hs, llm=MockLLM(BASE.model))
finally:
    Store.backfill_message_scopes = orig_bf  # type: ignore[method-assign]
check("★ 回填失败有提示（不再 except: pass）",
      any("隐私迁移" in n for n in hbf.notices), str(hbf.notices[:2]))
check("回填失败记进了 diary",
      any("隐私迁移失败" in str(r.get("title") or "")
          for r in hbf.store.list_diary(kind="terminal", limit=5)),
      str([r.get("title") for r in hbf.store.list_diary(kind="terminal", limit=5)]))
check("★ 失败时没有留下「已完成」标记（下次启动会重试）",
      hbf.store.meta_get_int("msg_scope_backfill_v2", 0) == 0
      and hbf.store.meta_get_int("msg_scope_backfilled", 0) == 0)
# 恢复后下一次启动真的会重跑
st2 = Store(hs.db_path)
ran = {"n": 0}
orig2 = Store.backfill_message_scopes


def counting_bf(self):
    ran["n"] += 1
    return orig2(self)


Store.backfill_message_scopes = counting_bf  # type: ignore[method-assign]
try:
    Giftia(hs, llm=MockLLM(BASE.model))
finally:
    Store.backfill_message_scopes = orig2  # type: ignore[method-assign]
check("★ 下一次启动真的重跑了回填", ran["n"] == 1, str(ran))
st2.close()

# ══ I. 未绑定 + /profile 兜底；本地哨兵不误伤 ══════════════════════
print("\n== I. 未绑定态 /profile 与本地哨兵 ==")
i2 = Giftia(env("unbound2", auto_bind=False), llm=MockLLM(BASE.model))
i2.store.add_memory(instance_no=1, created_ts=i2.clock.iso(), happened_ts=i2.clock.iso(),
                    summary=f"搭档的秘密 {SECRET_A}", importance=0.9, layer="important",
                    scope="private", subject_id="", about="小林")
out = i2.profile_text("")
check("★ 未绑定时 /profile 一个字的档案都不输出", SECRET_A not in out and "还没" in out, out[:40])
check("本地哨兵常量没变（CLI / run.py 仍是不带 user_id 的那条路）", LOCAL_USER_ID == "handler")
cli = Giftia(env("cli"), llm=MockLLM(BASE.model))
cli.handle("我叫小林")            # 本地 CLI 就是这么调的
check("本地 CLI 第一条也算认主", cli.handler_id == LOCAL_USER_ID, cli.handler_id)
check("本地认主不设遮蔽下界（他看的是自己留下的记忆）", cli.handler_bound_at == "")

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
print(f"临时目录：{TMPROOT}")
shutil.rmtree(TMPROOT, ignore_errors=True)
sys.exit(1 if FAIL else 0)
