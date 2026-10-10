#!/usr/bin/env bash
# 部署前只读体检：不改服务器任何东西。由 lead 从本机 scp 过去执行。
set -u
echo "=== 1. sudo 是否免密 ==="
if sudo -n true 2>/dev/null; then echo "SUDO_NOPASS"; else echo "SUDO_NEEDS_PASSWORD"; fi

echo
echo "=== 2. 服务器上的 config 与仓库版本差多少 ==="
wc -c /opt/giftia/config/settings.toml /opt/giftia/config/persona.toml 2>/dev/null

echo
echo "=== 3. settings.toml 关键项（看她被怎么配的）==="
grep -nE '^(total_hours|demo_total_hours|enabled|target|response_tier|use_wakeup|max_per_day|max_per_30_days|retention_unit_hours|consolidate_every|handler_name|auto_bind|group_stranger_reply)' /opt/giftia/config/settings.toml 2>/dev/null

echo
echo "=== 4. persona 是谁 ==="
grep -nE '^(name|model_code)' /opt/giftia/config/persona.toml 2>/dev/null

echo
echo "=== 5. 服务器代码的版本特征（新旧对比锚点）==="
grep -c "respond_safely" /opt/giftia/adapters/qq_official.py 2>/dev/null || echo "0（旧版，没有 respond_safely）"
grep -c "ALL_SCOPES" /opt/giftia/giftia/store.py 2>/dev/null || echo "0（旧版，没有 ALL_SCOPES）"
grep -c "core_always_inject" /opt/giftia/config/settings.toml 2>/dev/null || echo "0（旧版 config）"

echo
echo "=== 6. 她的记忆规模（不打印内容）==="
if command -v sqlite3 >/dev/null 2>&1; then
  sudo -n -u giftia sqlite3 /opt/giftia/data/giftia.db \
    "select (select count(*) from messages)||' messages / '||(select count(*) from memories)||' memories / '||(select count(*) from dreams)||' dreams';" 2>&1
else
  echo "（服务器没装 sqlite3，用 python3 读）"
  sudo -n -u giftia python3 - <<'PY' 2>&1
import sqlite3
c = sqlite3.connect("/opt/giftia/data/giftia.db")
for t in ("messages", "memories", "dreams", "diary"):
    try:
        print(f"  {t}: {c.execute(f'select count(*) from {t}').fetchone()[0]}")
    except Exception as e:
        print(f"  {t}: ERR {e}")
PY
fi

echo
echo "=== 7. 磁盘与内存 ==="
df -h /opt | tail -1
free -m | head -2

echo
echo "=== 8. 服务 ==="
systemctl is-active giftia-qq 2>/dev/null
systemctl show giftia-qq -p ActiveEnterTimestamp --value 2>/dev/null
echo "上次重启原因: $(systemctl show giftia-qq -p Result --value 2>/dev/null) / NRestarts=$(systemctl show giftia-qq -p NRestarts --value 2>/dev/null)"
journalctl -u giftia-qq -n 5 --no-pager 2>/dev/null | tail -5

echo
echo "=== 9. 服务器能否访问 GitHub（决定用哪种搬运方式）==="
timeout 12 git ls-remote --heads https://github.com/xcx-1220/giftia.git 2>&1 | head -3 || echo "GITHUB_UNREACHABLE_FROM_SERVER"

echo
echo "=== 10. data 目录可写性 ==="
sudo -n -u giftia test -w /opt/giftia/data && echo DATA_WRITABLE || echo DATA_NOT_WRITABLE
echo "=== DONE ==="
