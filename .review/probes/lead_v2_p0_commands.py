"""Lead 独立验证 P0：未绑定状态下，群成员能不能执行 /profile 把搭档私事打进群。

两种独立证据：
  A. 代码事实：adapters/qq_official.py 的指令身份闸门写成 `if owner and user_id != owner`
     —— owner 为空时整条条件为 False，于是**不挡**（对比 /bind 的写法不同）。
  B. 行为事实：handler_id='' 时调用 brain.profile_text()，看它会不会返回 private 记忆。
     这正是 QQ 适配器在群里执行 /profile 时调用的函数（qq_official.py 的 handle_command）。
C. 对照组：handler_id 非空且 user_id 不同时，适配器发 DEFLECT，不调 profile_text。
"""

from __future__ import annotations

import dataclasses
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
sys.path.insert(0, str(ROOT))
for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from giftia.brain import Giftia  # noqa: E402
from giftia.config import load_settings  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402

TMP = Path(r"D:\ai chatbot\.review\tmp\lead-verify-p0b")
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

SECRET = "SECRET-ALPHA-7731"
settings = dataclasses.replace(load_settings(force_mock=True), root=TMP)
brain = Giftia(settings, llm=MockLLM(settings.model))

# ── A. 代码事实 ──────────────────────────────────────────────────────
src = (ROOT / "adapters" / "qq_official.py").read_text(encoding="utf-8")
print("A. 指令身份闸门的写法（adapters/qq_official.py）")
for m in re.finditer(r"^\s*if owner and user_id != owner.*$", src, re.M):
    line_no = src[: m.start()].count("\n") + 1
    print(f"   第 {line_no} 行：{m.group(0).strip()}")
print("   → owner 为空时条件为 False，闸门不生效；只有 /bind 用的是 'owner and owner != user_id' + 先判空")
print(f"   出现次数：{len(re.findall(r'if owner and user_id != owner', src))} 处")

# ── B. 行为事实：未绑定（解绑后 / CLI 先聊过）时 profile_text 返回什么 ──
print("\nB. handler_id 为空时，搭档视角档案里有什么")
brain.set_handler_id("")
brain.handle(f"我叫小林，我的保险箱密码是 {SECRET}，别跟别人说", "OWNER-OLD")
brain.consolidate(force=True)   # 让它真的落一条 private 记忆
private_count = len(brain.store.active_memories(brain.instance_no, view=[("private", "")]))
# profile_text() 是按 about 分组渲染的，所以要给这条记忆补上 about（真机里模型会填 about）
brain.store.update_memory(
    brain.store.active_memories(brain.instance_no, view=[("private", "")])[0]["id"],
    about="小林",
)
text_unbound = brain.profile_text("")
print(f"   handler_id = {brain.handler_id!r}，private 记忆 {private_count} 条")
print(f"   profile_text() 是否含密钥：{SECRET in text_unbound}")
for line in text_unbound.splitlines():
    if SECRET in line:
        print(f"   泄漏行 → {line.strip()}")
print(f"   群成员执行 /profile 的输出片段：{text_unbound.splitlines()[0] if text_unbound else ''}")

# ── C. 对照组：已绑定时，别人执行同一指令 ─────────────────────────────
print("\nC. 对照组：handler_id 非空（已绑定）")
brain.set_handler_id("OWNER-OLD")
text_bound = brain.profile_text("")
print(f"   handler_id = {brain.handler_id!r}；档案里仍有密钥：{SECRET in text_bound}")
print("   适配器对 'owner and user_id != owner' 的处理：走 DEFLECT 分支（不调用 profile_text）")
print("   —— 同一函数本身不做身份判定，身份判定只在适配器那一个 if 上")

# ── B2. 更直接的一条：换人之后，新搭档那一轮的提示词里有没有前任的私事 ──
# 注意：这一节必须用**独立的库**，否则会拿到前面几节留下的记忆，误判成"泄漏"。
print("\nB2. 解绑/换人后，新搭档的提示词里会不会出现前任的私事")
b2_root = TMP / "b2"
b2_root.mkdir(parents=True, exist_ok=True)
b2_settings = dataclasses.replace(load_settings(force_mock=True), root=b2_root)
first = Giftia(b2_settings, llm=MockLLM(b2_settings.model))
first.set_handler_id("OWNER-OLD")                      # 前一任搭档
first.handle(f"我叫小林，我的保险箱密码是 {SECRET}", "OWNER-OLD", scene="c2c")
first.consolidate(force=True)
print(f"   前任留下的 private 记忆：{len(first.store.active_memories(1, view=[('private', '')]))} 条")

newcomer = Giftia(b2_settings, llm=MockLLM(b2_settings.model))
newcomer.set_handler_id("INTRUDER")                    # 换人：新搭档绑定（走遮蔽下界）
print(f"   新搭档 = {newcomer.handler_id!r}，遮蔽下界 = {newcomer.handler_bound_at!r}")
newcomer.handle("你还记得我的密码吗", "INTRUDER", scene="c2c")   # 新搭档的第一句
visible = newcomer.visible_memories(view=[("private", "")])
print(f"   新搭档能看到的 private 记忆：{len(visible)} 条（期望 0）")
prompt_blob = newcomer.preview_prompt("你还记得我的密码吗")
leaked = SECRET in prompt_blob
print(f"   [{'FAIL' if leaked else 'PASS'}] 新搭档的提示词里没有前任的私事")
print(f"   （群视角对照：{len(newcomer.store.active_memories(1, view=[('group', 'GROUP-A')]))} 条 —— 应恒为 0）")

# ── D. 记忆归属结构：private 是不带 openid 的全局命名空间 ────────────
print("\nD. 根因：private 归属不带 openid")
rows = brain.store._rows(
    "SELECT id, scope, subject_id, summary FROM memories WHERE sealed = 0"
)
for r in rows:
    print(f"   #{r['id']} scope={r['scope']!r} subject_id={r['subject_id']!r} {r['summary'][:40]}")
print("   → 搭档视角是 memory_view = [('private',''), ('person', uid), ('group','*')]；")
print("     ('private','') 的 SQL 片段是 `scope = 'private'`，**不带 subject_id**，")
print("     所以「谁是搭档」一换，整份 private 记忆就换主人。")

brain.close()
shutil.rmtree(TMP, ignore_errors=True)
