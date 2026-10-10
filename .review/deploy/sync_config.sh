#!/usr/bin/env bash
# 用仓库版 config 覆盖服务器 config（值已证明等价），并逐字节校验
set -u
APP=/opt/giftia
STAMP=$(date +%Y%m%d-%H%M%S)

echo "=== 1. 先备份当前（含新键的）config ==="
sudo cp -a "$APP/config/settings.toml" "/opt/giftia-config-settings-$STAMP.bak"
sudo cp -a "$APP/config/persona.toml"  "/opt/giftia-config-persona-$STAMP.bak"
sudo ls -la /opt/giftia-config-*.bak | tail -4

echo
echo "=== 2. 覆盖为仓库版（保留 600 权限与 giftia 属主）==="
sudo install -o giftia -g giftia -m 600 /tmp/newconfig-settings.toml "$APP/config/settings.toml"
sudo install -o giftia -g giftia -m 600 /tmp/newconfig-persona.toml  "$APP/config/persona.toml"
sudo ls -la "$APP/config/"

echo
echo "=== 3. 逐字节校验：服务器 == 本机上传的那份 ==="
for pair in "settings:/tmp/newconfig-settings.toml" "persona:/tmp/newconfig-persona.toml"; do
  name=${pair%%:*}; src=${pair#*:}
  a=$(sudo sha256sum "$APP/config/$name.toml" | cut -d' ' -f1)
  b=$(sha256sum "$src" | cut -d' ' -f1)
  if [ "$a" = "$b" ]; then echo "  $name.toml  ✓ 逐字节一致  ${a:0:16}"; else echo "  $name.toml  !! 不一致  server=$a  src=$b"; fi
done

echo
echo "=== 4. 值仍然是同一套（防"覆盖把她的设定改了"）==="
cd "$APP" && sudo -n -u giftia /usr/bin/python3 - <<'PY'
from giftia.config import load_settings
s = load_settings()
m, o, l = s.memory, s.outreach, s.lifespan
print(f"  lifespan.total_hours={l.total_hours}  stages={len(s.stages)}")
print(f"  memory.retention_unit_hours={m.retention_unit_hours} consolidate_every={m.consolidate_every}")
print(f"  memory.core_always_inject={m.core_always_inject} min_raw_score={m.min_raw_score} prefilter={m.recall_prefilter}")
print(f"  outreach.enabled={o.enabled} target={o.target} max_per_day={o.max_per_day} max_per_30_days={o.max_per_30_days} gap={o.min_gap_hours}")
print(f"  outreach.mood_share_min_idle_hours={o.mood_share_min_idle_hours} wakeup_max_per_30_days={o.wakeup_max_per_30_days}")
print(f"  runtime.response_tier={s.runtime.response_tier} auto_bind={s.runtime.auto_bind} keywords={s.runtime.keywords}")
print(f"  persona.name={s.persona.name} model_code={s.persona.model_code}")
PY

echo
echo "=== 5. 重启并确认她上线 ==="
sudo systemctl restart giftia-qq
sleep 6
echo "服务：$(systemctl is-active giftia-qq) / 重启次数=$(systemctl show giftia-qq -p NRestarts --value) / 结果=$(systemctl show giftia-qq -p Result --value)"
sudo journalctl -u giftia-qq -n 8 --no-pager | tail -8

echo
echo "=== 6. 记忆完好 ==="
sudo -n -u giftia /usr/bin/python3 /tmp/dbcount.py "$APP/data/giftia.db"
sudo -n -u giftia /usr/bin/python3 -c "
import sqlite3
c = sqlite3.connect('/opt/giftia/data/giftia.db')
print('  integrity_check:', c.execute('PRAGMA integrity_check').fetchone()[0])
"
echo "CONFIG_SYNC_DONE"
