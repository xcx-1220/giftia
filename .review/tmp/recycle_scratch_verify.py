"""临时验证（不属于交付探针）：新 view 契约下 recycle/occasions/dream 的行为。

1. P1-11：/days（days_together_text，不传 view）只列搭档视角的大日子。
2. P2：dream.should_dream 与 make_dream 用同一视角计数（别人的私聊凑不够门槛）。
3. P0-1：回收后回忆录/diary 干净、封存 JSON 全量带标记。
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import sys
from datetime import timedelta
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
from giftia.dream import make_dream, should_dream  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402
from giftia.store import ALL_SCOPES  # noqa: E402

TMP = ROOT / ".review" / "tmp" / "recycle-scratch"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}" + (f"  → {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  → {detail}" if detail else ""))


def settings(name, hours=81920.0):
    root = TMP / name
    root.mkdir(parents=True, exist_ok=True)
    return dataclasses.replace(load_settings(lifespan_override=hours, force_mock=True), root=root)


SECRET = "SECRET-XIAOYU-9901"

print("=" * 78)
print("1. /days（days_together_text，不传 view）按归属过滤  ← P1-11")
print("=" * 78)
s1 = settings("days")
b1 = Giftia(s1, llm=MockLLM(s1.model))
b1.set_handler_id("OWNER-1")
now = b1.clock.iso()
tomorrow = (b1.clock.now().date() + timedelta(days=1)).isoformat()
b1.store.add_memory(instance_no=b1.instance_no, created_ts=now, happened_ts=now,
                    summary="搭档明天要去面试", event_date=tomorrow, scope="private", subject_id="")
b1.store.add_memory(instance_no=b1.instance_no, created_ts=now, happened_ts=now,
                    summary=f"{SECRET} 小雨明天要手术，别告诉别人", event_date=tomorrow,
                    scope="person", subject_id="USER-XIAOYU")
b1.store.add_memory(instance_no=b1.instance_no, created_ts=now, happened_ts=now,
                    summary="阿伟明天要考驾照", event_date=tomorrow,
                    scope="group", subject_id="GROUP-A")
days = b1.days_text()
print("  /days 输出：")
for line in days.splitlines():
    print(f"    {line}")
check("P1-11 搭档视角的大日子在（private）", "搭档明天要去面试" in days)
check("P1-11 别人的私聊不在（person）", SECRET not in days and "小雨" not in days)
check("P1-11 其它群的事不在（group）", "阿伟" not in days)

print()
print("=" * 78)
print("2. dream：should_dream 与 make_dream 同一视角  ← memory-man P1-3")
print("=" * 78)
s2 = settings("dream")
b2 = Giftia(s2, llm=MockLLM(s2.model))
b2.set_handler_id("OWNER-1")
for i in range(5):        # 别人的私聊：凑够 min_memories，但不是"她的事"
    b2.store.add_memory(instance_no=b2.instance_no, created_ts=b2.clock.iso(),
                        happened_ts=b2.clock.iso(),
                        summary=f"陌生人第 {i} 件私事 {SECRET}", importance=0.6,
                        scope="person", subject_id=f"STRANGER-{i}")
n_all = len(b2.store.active_memories(b2.instance_no, view=ALL_SCOPES))
n_owner = len(b2.store.active_memories(b2.instance_no, view=[("private", ""), ("person", "OWNER-1")]))
check("P1-3 搭档自己 0 条记忆时 should_dream 必须为 False（不能拿别人的私聊凑门槛）",
      should_dream(s2, b2.store, b2.clock, b2.instance_no) is False,
      f"全量={n_all} 条 / 搭档视角={n_owner} 条 / min_memories={s2.dream.min_memories}")
check("P1-3 make_dream 与 should_dream 同结果（都做不出来）",
      make_dream(s2, b2.store, b2.clock, b2.llm, b2.instance_no) is None)
for i in range(3):        # 搭档自己的事
    b2.store.add_memory(instance_no=b2.instance_no, created_ts=b2.clock.iso(),
                        happened_ts=b2.clock.iso(),
                        summary=f"搭档第 {i} 件私事：他去过深蓝咖啡馆", importance=0.7,
                        scope="private", subject_id="")
gate = should_dream(s2, b2.store, b2.clock, b2.instance_no)
dream = make_dream(s2, b2.store, b2.clock, b2.llm, b2.instance_no)
check("P1-3 搭档自己攒够 min_memories 后两者一致地放行",
      gate is True and dream is not None, f"should_dream={gate} make_dream={'ok' if dream else None}")

frag0 = dataclasses.replace(s2, dream=dataclasses.replace(s2.dream, fragments=0))
b3 = Giftia(dataclasses.replace(s2, root=settings("dream0").root), llm=MockLLM(s2.model))
b3.set_handler_id("OWNER-1")
for i in range(4):
    b3.store.add_memory(instance_no=b3.instance_no, created_ts=b3.clock.iso(),
                        happened_ts=b3.clock.iso(), summary=f"第 {i} 件私事", importance=0.7)
frag_src = make_dream(frag0, b3.store, b3.clock, b3.llm, b3.instance_no,
                      view=[("private", "")])
rows = b3.store.recent_dreams(b3.instance_no, limit=1)
n_src = len(rows[0]["source_ids"]) if (frag_src and rows) else -1
check("P2 fragments=0 时不拿全部记忆（取材应为 1 条，不是 4 条）",
      n_src == 1, f"source_ids={n_src}")

print()
print("=" * 78)
print("3. 回收：回忆录/diary 干净 + 封存 JSON 全量带标记  ← P0-1")
print("=" * 78)
s4 = settings("recycle")
b4 = Giftia(s4, llm=MockLLM(s4.model))
b4.set_handler_id("OWNER-1")
now4 = b4.clock.iso()
b4.store.add_memory(instance_no=b4.instance_no, created_ts=now4, happened_ts=now4,
                    summary="搭档说过他下周三要考驾照", importance=0.7, scope="private")
b4.store.add_memory(instance_no=b4.instance_no, created_ts=now4, happened_ts=now4,
                    summary=f"{SECRET} 小雨私聊说她跟家里吵架了", importance=0.9,
                    scope="person", subject_id="USER-XIAOYU")
b4.store.add_memory(instance_no=b4.instance_no, created_ts=now4, happened_ts=now4,
                    summary="阿伟在群里说他老婆怀孕了", importance=0.6,
                    scope="group", subject_id="GROUP-A")
report_text = b4.run_recycle("验证")
md = sorted(s4.memoir_dir.glob("*.md"))[0].read_text(encoding="utf-8")
sealed = json.loads(sorted(s4.sealed_dir.glob("*.json"))[0].read_text(encoding="utf-8"))
diary = "\n".join(str(r["content"]) for r in b4.store.list_diary(kind="terminal"))
check("P0-1 回忆录不含别人的私聊 / 群里的事", SECRET not in md and "阿伟" not in md)
check("P0-1 diary（/memoir）不含别人的私聊 / 群里的事", SECRET not in diary and "阿伟" not in diary)
check("P0-1 封存 JSON 仍是全量原始数据（person + group 都在）",
      any(SECRET in str(m["summary"]) for m in sealed["memories"])
      and any("阿伟" in str(m["summary"]) for m in sealed["memories"]),
      f'{len(sealed["memories"])} 条')
check("P0-1 封存 JSON 显式标注 scope/note",
      sealed.get("scope") == "all_scopes" and bool(str(sealed.get("note") or "")))
check("P0-1 终端服务公文说明了封存是全量", "含全部归属" in report_text)
print(f"  回忆录条数={len(sealed['memories'])}（封存全量）"
      f"；回忆录正文里 private 条数={md.count('搭档说过他下周三要考驾照')}")

print()
print(f"══ 临时验证：PASS={PASS} FAIL={FAIL} ══")
shutil.rmtree(TMP, ignore_errors=True)
raise SystemExit(1 if FAIL else 0)
