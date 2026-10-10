import sys
sys.path.insert(0, ".")
sys.path.insert(0, ".review/probes")
from mm_common import fresh_dir, make_brain, settings_for
from giftia.memory import load_pending

tmp = fresh_dir("dbg_cursor")
settings = settings_for(tmp)
b3 = make_brain(tmp / "cursor", settings=settings)
s3, i3 = b3.store, b3.instance_no
for k in range(3):
    s3.add_message("i1-m-2026100%d" % (k + 1), i3, "user", f"会话{k}的一句话内容",
                   f"2026-10-0{k+1}T09:00:00")
print("rows:", s3._rows("SELECT id, session_id, consolidated FROM messages"))
print("meta before:", s3._rows("SELECT key, value FROM meta WHERE key LIKE 'last%'"))
s3.meta_set("last_consolidated_msg_id", 2)
s3.meta_set("last_consolidated:i1-m-20261003", 2)
p1 = load_pending(s3, i3, "i1-m-20261001")
p3 = load_pending(s3, i3, "i1-m-20261003")
print("p1", [m["id"] for m in p1], "p3", [m["id"] for m in p3])
print("meta after:", s3._rows("SELECT key, value FROM meta WHERE key LIKE 'last%'"))
print("first_open:", s3.first_open_message_id(i3, "i1-m-20261001"))
