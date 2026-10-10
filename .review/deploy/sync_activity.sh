#!/usr/bin/env bash
# 把本地仓库版 settings.toml（tier=70 / cap=6，注释已更新）覆盖到服务器，并核对
set -u
APP=/opt/giftia
CFG="$APP/config/settings.toml"
STAMP=$(date +%Y%m%d-%H%M%S)

echo "===== 1. 备份 ====="
sudo cp -a "$CFG" "/opt/giftia-config-settings-$STAMP.bak"
echo "  /opt/giftia-config-settings-$STAMP.bak"

echo
echo "===== 2. 装入（-o/-g/-m 一次到位）====="
sudo install -o giftia -g giftia -m 600 /tmp/repo-settings-activity.toml "$CFG"
sudo ls -la "$CFG"

echo
echo "===== 3. 逐字节校验 ====="
a=$(sudo sha256sum "$CFG" | cut -d' ' -f1)
b=$(sha256sum /tmp/repo-settings-activity.toml | cut -d' ' -f1)
echo "  server=$a"
echo "  repo  =$b"
[ "$a" = "$b" ] && echo "  ✓ 逐字节一致" || echo "  !! 不一致"

echo
echo "===== 4. 值确认（用服务同一个解释器）====="
cd "$APP" && sudo -n -u giftia /usr/bin/python3 - <<'PY'
from giftia.config import load_settings
s = load_settings()
t, c = s.runtime.response_tier, s.runtime.speak_max_per_day
prob = 0.0 if t < 45 else min(1.0, (t - 20) / 70.0)
print(f"  response_tier={t}  speak_max_per_day={c}")
print(f"  → 没被 @ 时接话概率 ≈ {prob:.0%}，每个群每天最多自己插嘴 {c} 次")
PY

echo
echo "===== 5. 重启 ====="
sudo systemctl restart giftia-qq
sleep 6
echo "  服务：$(systemctl is-active giftia-qq) / NRestarts=$(systemctl show giftia-qq -p NRestarts --value)"
sudo journalctl -u giftia-qq -n 4 --no-pager | tail -4

echo
echo "===== 6. 记忆完好 ====="
sudo -n -u giftia /usr/bin/python3 -c "
import sqlite3
c = sqlite3.connect('/opt/giftia/data/giftia.db')
print('  messages/memories/dreams:',
      c.execute('select count(*) from messages').fetchone()[0],
      c.execute('select count(*) from memories').fetchone()[0],
      c.execute('select count(*) from dreams').fetchone()[0])
print('  integrity_check:', c.execute('PRAGMA integrity_check').fetchone()[0])
"
echo "SYNC_DONE"
