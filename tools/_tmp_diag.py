"""诊断：她的记忆到底怎么了（只读）。"""
import sys
from collections import Counter

sys.path.insert(0, "/opt/giftia")
from giftia.brain import Giftia  # noqa: E402
from giftia.config import load_settings  # noqa: E402
from giftia.memory import is_cited, recall, retention_of  # noqa: E402

settings = load_settings()
bot = Giftia(settings)
now = bot.clock.now()
cfg = settings.memory
store = bot.store
inst = bot.instance_no

print(f"现在 {now.strftime('%Y-%m-%d %H:%M')}　实例 #{inst}")
print(f"记忆参数：retention_unit_hours={cfg.retention_unit_hours}  "
      f"consolidate_every={cfg.consolidate_every}  min_importance={cfg.min_importance}")
print(f"          cite_min_shared={cfg.cite_min_shared}  session_window={cfg.session_window}")
print()

mems = store.active_memories(inst)
print(f"总记忆 {len(mems)} 条")
print(f"  按归属 {dict(Counter(str(m.get('scope')) for m in mems))}")
print(f"  按分层 {dict(Counter(str(m.get('layer')) for m in mems))}")
print(f"  平均摘要长度 {sum(len(str(m['summary'])) for m in mems) / max(1, len(mems)):.0f} 字")
print()

msgs = store.all_messages(inst, limit=5000)
print(f"消息 {len(msgs)} 条")
for sid, n in Counter(str(m["session_id"]) for m in msgs).most_common():
    print(f"  {sid}  {n} 条")
pending = store.open_messages(inst)
print(f"还没整理成记忆的 {len(pending)} 条")
print()

print("═" * 78)
print("全部记忆（保留率 = 她现在还记得多清楚）")
print("═" * 78)
for m in sorted(mems, key=lambda x: str(x["created_ts"])):
    ret = retention_of(m, cfg, now)
    flag = "" if ret > 0.5 else ("  ⚠已经模糊" if ret > 0.2 else "  ⚠⚠快没了")
    print(f"#{m['id']:<4}[{str(m.get('scope')):<7}][{str(m.get('layer')):<9}]"
          f"重要性{float(m['importance']):.2f} 保留{ret:.2f}{flag}")
    print(f"     {m['summary']}")
    print(f"     标签：{m['tags']}　情绪：{m['emotion']}　建于 {str(m['created_ts'])[:16]}")
print()

print("═" * 78)
print("召回实测：随便问几句，看她会想起什么（这是「记混」的现场）")
print("═" * 78)
stage = bot.status().stage
for q in ("我下周三要做什么", "上次说的那件事", "你还记得我吗", "阿伟说了什么"):
    hits = recall(q, mems, stage, cfg, now)
    print(f"\n问「{q}」→ 命中 {len(hits)} 条")
    for h in hits:
        print(f"   {h.score:.2f} [{h.confidence}] {h.text[:50]}")
    if not hits:
        print("   （什么都想不起来）")
bot.store.close()
