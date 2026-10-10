import sys
from pathlib import Path

sys.path.insert(0, ".")
from giftia.store import Store

p = Path(".review/tmp/memory-man-consolidate/data/giftia.db")
print("exists", p.exists())
st = Store(p)
for r in st._rows(
    "SELECT id, session_id, instance_no, consolidated, recalled, substr(content,1,24) AS c "
    "FROM messages ORDER BY id"
):
    print(r)
print("meta:", st._rows("SELECT key, value FROM meta WHERE key LIKE 'last%'"))
print("first_open(sess1):", st.first_open_message_id(1, "i1-m-20261001"))
print("open_messages(sess1):", [m["id"] for m in st.open_messages(1, 0, session_id="i1-m-20261001")])
st.close()
