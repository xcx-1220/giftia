"""Lead 独立复现：memory-man 报的两条 P0（不复用它的探针）。

P0-1：`giftia/recycle.py:102` 的 `store.active_memories(instance_no)` 没传 view
      → 别人的私聊记忆 / 所有群的事被写进给搭档看的回忆录（.md）、封存 JSON、以及 /memoir 的 diary。
      代码事实：store._view_clause(None) → ("", [])，即**不过滤**。

P0-2：`store.backfill_message_scopes()` 用全局 state['last_group_openid'] 给所有 `-g` 老消息回填归属
      → 群 A 的历史消息被标成群 B 的 openid，随后整理成"群 B 的记忆"。

复现方式：只用 giftia 的真实 API 构造数据（Store.add_memory / add_message + perform_recycle），
不直接改 SQL，避免"只有手工造数据才能触发"的质疑。
"""

from __future__ import annotations

import dataclasses
import json
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

from giftia.config import load_settings  # noqa: E402
from giftia.clock import Clock  # noqa: E402
from giftia.lifespan import Lifespan  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402
from giftia.recycle import perform_recycle  # noqa: E402
from giftia.store import Store  # noqa: E402

TMP = Path(r"D:\ai chatbot\.review\tmp\lead-verify-p0")
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

SECRET = "SECRET-BRAVO-4402"

# ── P0-1 ────────────────────────────────────────────────────────────
print("=" * 78)
print("P0-1 回收仪式是否过滤归属（release 前必须确认）")
print("=" * 78)
root = TMP / "recycle"
root.mkdir(parents=True, exist_ok=True)
settings = dataclasses.replace(load_settings(force_mock=True), root=root)
store = Store(settings.db_path)
clock = Clock(store)
now = clock.iso()
instance = 1

store.add_memory(
    instance_no=instance, created_ts=now, happened_ts=now,
    summary="搭档说过他下周三要考驾照。", importance=0.7, layer="important",
    scope="private", subject_id="",
)
store.add_memory(
    instance_no=instance, created_ts=now, happened_ts=now,
    summary=f"{SECRET} 小雨私聊说她跟家里吵架了。", importance=0.9, layer="important",
    scope="person", subject_id="USER-XIAOYU",
)
store.add_memory(
    instance_no=instance, created_ts=now, happened_ts=now,
    summary="阿伟在群里说他老婆怀孕了。", importance=0.6, layer="normal",
    scope="group", subject_id="GROUP-A",
)
print(f"库里有 3 条不同归属的记忆：private / person(小雨) / group(群A)")

# 让 lifespan 走一遍激活，然后回收
life = Lifespan(settings, store, clock)
life.ensure_activated(total_hours_override=1.0)
clock.advance(2)      # 强制到期
report = perform_recycle(settings, store, clock, MockLLM(settings.model), life, reason="寿命结束")

memoir_text = report.memoir_path.read_text(encoding="utf-8")
sealed_text = report.sealed_path.read_text(encoding="utf-8")
diary_rows = store.list_diary(kind="terminal", limit=10)
diary_text = "\n".join(str(r["content"]) for r in diary_rows)

checks = [
    ("回忆录 .md 不含小雨的私聊", SECRET not in memoir_text),
    ("封存 JSON 不含小雨的私聊", SECRET not in sealed_text),
    ("/memoir（diary 表）不含小雨的私聊", SECRET not in diary_text),
]
for label, ok in checks:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
print(f"\n  回忆录路径：{report.memoir_path}")
leaked = [ln for ln in memoir_text.splitlines() if SECRET in ln]
for ln in leaked:
    print(f"  泄漏行 → {ln}")
print(f"\n  active_memories() 不传 view 时返回 {len(store.active_memories(instance))} 条"
      f"（= 全量，含 person/group）")
print(f"  对照组：群 A 视角 active_memories(view=[('group','GROUP-A')]) = "
      f"{len(store.active_memories(instance, view=[('group', 'GROUP-A')]))} 条")
print(f"  对照：搭档视角 active_memories(view=[('private','')]) = "
      f"{len(store.active_memories(instance, view=[('private', '')]))} 条")
store.close()

# ── P0-2 ────────────────────────────────────────────────────────────
print()
print("=" * 78)
print("P0-2 老库 scope 回填：群 A 的历史消息会不会被标成群 B")
print("=" * 78)
root2 = TMP / "backfill"
root2.mkdir(parents=True, exist_ok=True)
old = dataclasses.replace(load_settings(force_mock=True), root=root2)
st2 = Store(old.db_path)
# 模拟升级前的老库：两条群消息，scope 还是列默认值
st2.add_message("i1-gGROUP-A-20261001", 1, "user", "阿伟说周末打球，三缺一。", "2026-10-01T10:00:00",
                speaker_id="USER-AWEI", speaker_name="阿伟")
st2.add_message("i1-gGROUP-B-20261002", 1, "user", "小雨说下周考试。", "2026-10-02T10:00:00",
                speaker_id="USER-XIAOYU", speaker_name="小雨")
# "最近活跃的群" = 群 B（她最后在 B 群说过话）
st2.state_set("last_group_openid", "GROUP-B")
st2.meta_set("msg_scope_backfilled", 0)

before = [(r["id"], r["session_id"], r["scope"], r["subject_id"])
          for r in st2._rows("SELECT id, session_id, scope, subject_id FROM messages ORDER BY id")]
print("回填前：")
for row in before:
    print(f"  #{row[0]} {row[1]:<24} scope={row[2]:<8} subject={row[3]!r}")
updated = st2.backfill_message_scopes()
after = [(r["id"], r["session_id"], r["scope"], r["subject_id"])
         for r in st2._rows("SELECT id, session_id, scope, subject_id FROM messages ORDER BY id")]
print(f"backfill 更新 {updated} 条：")
mismatch = 0
for row in after:
    sid_group = row[1].split("-g", 1)[1].rsplit("-", 1)[0]
    ok = row[3].startswith(sid_group) or sid_group.startswith(row[3])
    if not ok:
        mismatch += 1
    print(f"  #{row[0]} {row[1]:<24} scope={row[2]:<8} subject={row[3]!r} "
          f"{'OK' if ok else '← 归属错！session 里写的是 ' + sid_group}")
print(f"  [{'PASS' if mismatch == 0 else 'FAIL'}] 每条群消息的 subject 是否与 session_id 里的群标识一致"
      f"（错 {mismatch} 条）")
st2.close()

print("\n删除临时库：" f"{TMP}")
shutil.rmtree(TMP, ignore_errors=True)
