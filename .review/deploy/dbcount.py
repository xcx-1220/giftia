import sqlite3
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "/opt/giftia/data/giftia.db"
c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
out = []
for t in ("messages", "memories", "dreams", "diary", "state", "meta"):
    try:
        out.append(f"{t}={c.execute(f'select count(*) from {t}').fetchone()[0]}")
    except Exception as exc:  # noqa: BLE001
        out.append(f"{t}=ERR({exc})")
try:
    inst = c.execute("select value from meta where key='instance_no'").fetchone()
    out.append(f"instance_no={inst[0] if inst else '?'}")
except Exception:
    pass
print("  " + "  ".join(out))
