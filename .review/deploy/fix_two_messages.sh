#!/usr/bin/env bash
# 精确修掉那 2 条"群会话里却标成 private"的消息（它们是旧代码留下的边界），
# 并顺手看那 4 条未整理消息是什么。
set -u
DB=/opt/giftia/data/giftia.db
STAMP=$(date +%Y%m%d-%H%M%S)

echo "=== 1. 改动前备份整个库（含 WAL，用 sqlite backup 保证一致）==="
sudo -n -u giftia python3 - <<'PY'
import sqlite3, shutil, time
src = sqlite3.connect("/opt/giftia/data/giftia.db")
dst_path = f"/opt/giftia/data/giftia-pre-scopefix-{time.strftime('%Y%m%d-%H%M%S')}.db"
dst = sqlite3.connect(dst_path)
with dst:
    src.backup(dst)
src.close(); dst.close()
print("  备份：", dst_path)
PY

echo
echo "=== 2. 看那 4 条未整理消息 ==="
sudo -n -u giftia python3 - <<'PY'
import sqlite3
c = sqlite3.connect("/opt/giftia/data/giftia.db")
c.row_factory = sqlite3.Row
for r in c.execute("SELECT id, session_id, role, ts, scope, subject_id, substr(content,1,36) h "
                   "FROM messages WHERE consolidated=0 ORDER BY id"):
    print(f"  #{r['id']} {r['session_id']:<26} {r['role']:<9} scope={r['scope']:<8} "
          f"ts={r['ts'][:16]} {r['h']!r}")
PY

echo
echo "=== 3. 修复：群会话里的消息归属到它自己的群 ==="
sudo -n -u giftia python3 - <<'PY'
import re, sqlite3
c = sqlite3.connect("/opt/giftia/data/giftia.db")
c.row_factory = sqlite3.Row
# 用"该群会话里已有消息的 subject"（或 last_group_openid 前缀匹配）确定正确 subject
rows = c.execute("SELECT id, session_id, scope, subject_id FROM messages WHERE scope != 'group'").fetchall()
last_group = c.execute("SELECT value FROM state WHERE key='last_group_openid'").fetchone()
last_group = last_group[0] if last_group else ""
fixed = 0
for r in rows:
    sid = str(r["session_id"])
    m = re.match(r"^i\d+-g(.+?)-\d{8}$", sid)
    if not m:
        continue
    prefix = m.group(1)
    # 该会话里其他消息用的 subject
    peer = c.execute(
        "SELECT subject_id FROM messages WHERE session_id=? AND scope='group' AND subject_id != '' LIMIT 1",
        (sid,)).fetchone()
    subject = str(peer[0]) if peer else (last_group if last_group.startswith(prefix) else "")
    if not subject:
        print(f"  #{r['id']} 无法确定群标识，跳过（保持原样，不会被任何群看到）")
        continue
    c.execute("UPDATE messages SET scope='group', subject_id=? WHERE id=?", (subject, r["id"]))
    fixed += 1
    print(f"  #{r['id']} {sid} → scope=group subject={subject[:16]}…")
c.commit()
print(f"  修好 {fixed} 条")
PY

echo
echo "=== 4. 复验 ==="
sudo -n -u giftia python3 - <<'PY'
import re, sqlite3
c = sqlite3.connect("/opt/giftia/data/giftia.db")
bad = []
for r in c.execute("SELECT id, session_id, scope, subject_id FROM messages"):
    if "-g" in str(r[1]) and (r[2] != "group" or not r[3]):
        bad.append(tuple(r))
print("  群会话里归属不正确的消息：", len(bad), bad[:5])
print("  integrity_check:", c.execute("PRAGMA integrity_check").fetchone()[0])
for t in ("messages", "memories", "dreams"):
    print(f"  {t}: {c.execute(f'select count(*) from {t}').fetchone()[0]}")
PY

echo
echo "=== 5. 服务仍在跑、日志干净 ==="
systemctl is-active giftia-qq
sudo journalctl -u giftia-qq -n 3 --no-pager | tail -3
echo "SCOPE_FIX_DONE"
