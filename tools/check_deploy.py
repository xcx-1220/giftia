"""部署后核对：迁移是否安全、她的记忆还在不在。"""
import sys
from collections import Counter

sys.path.insert(0, "/opt/giftia")
from giftia.config import load_settings  # noqa: E402
from giftia.store import Store  # noqa: E402

settings = load_settings()
store = Store(settings.db_path)

mems = store.all_memories()
print(f"记忆        {len(mems)} 条")
print(f"归属分布    {dict(Counter(str(m.get('scope')) for m in mems))}")
print(f"有归属字段  {sum(1 for m in mems if m.get('scope'))}/{len(mems)}")
print(f"封存(前任)  {sum(1 for m in mems if int(m.get('sealed') or 0))} 条")
print(f"消息        {store.count_messages()} 条")
print(f"梦          {len(store.recent_dreams(1, 5))} 场")
print(f"认得的人    {store.state_get('known_people', '') or '（还没有）'}")
print(f"绑定        {store.state_get('handler_id', '') or '（还没认人）'}")
print(f"称呼        {store.state_get('handler_name', '') or '（还不知道）'}")
print()
print("会话分布（新的分区会话）：")
for sid, n in Counter(
    str(r["session_id"]) for r in store.all_messages(limit=500)
).most_common():
    print(f"  {sid}  {n} 条")
store.close()
