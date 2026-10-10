#!/usr/bin/env bash
# 装入仓库版 settings.toml，并做全套最终校验
set -u
APP=/opt/giftia
STAMP=$(date +%Y%m%d-%H%M%S)

echo "=== 1. 再备份一次当前 settings.toml ==="
sudo cp -a "$APP/config/settings.toml" "/opt/giftia-config-settings-$STAMP.bak"
echo "已备份 /opt/giftia-config-settings-$STAMP.bak"

echo
echo "=== 2. 装入仓库版（-o/-g/-m 一次性给对权限）==="
sudo install -o giftia -g giftia -m 644 /tmp/repo-settings.toml "$APP/config/settings.toml"
sudo ls -la "$APP/config/"

echo
echo "=== 3. 逐字节校验 ==="
a=$(sudo sha256sum "$APP/config/settings.toml" | cut -d' ' -f1)
b=$(sha256sum /tmp/repo-settings.toml | cut -d' ' -f1)
c=$(sudo sha256sum "$APP/config/persona.toml" | cut -d' ' -f1)
echo "  settings.toml server=$a"
echo "  settings.toml repo  =$b"
[ "$a" = "$b" ] && echo "  → ✓ settings.toml 与仓库逐字节一致" || echo "  → !! 仍不一致"
echo "  persona.toml  server=$c  （应为 e348b7556d584c678fc97f6b7e9c21d89535e12b5fc9a13e8d4c24556ba9543c）"

echo
echo "=== 4. 值不变（同一套设定）==="
cd "$APP" && sudo -n -u giftia /usr/bin/python3 - <<'PY'
from giftia.config import load_settings
s = load_settings()
m, o, l = s.memory, s.outreach, s.lifespan
print(f"  lifespan.total_hours={l.total_hours} stages={len(s.stages)}")
print(f"  retention_unit_hours={m.retention_unit_hours} consolidate_every={m.consolidate_every}")
print(f"  core_always_inject={m.core_always_inject} min_raw_score={m.min_raw_score} prefilter={m.recall_prefilter}")
print(f"  outreach enabled={o.enabled} target={o.target} per_day={o.max_per_day} per_30d={o.max_per_30_days} gap={o.min_gap_hours}")
print(f"  mood_share_min_idle_hours={o.mood_share_min_idle_hours} wakeup_max_per_30_days={o.wakeup_max_per_30_days}")
print(f"  response_tier={s.runtime.response_tier} persona={s.persona.name}")
PY

echo
echo "=== 5. 重启 + 上线确认 ==="
sudo systemctl restart giftia-qq
sleep 6
echo "服务：$(systemctl is-active giftia-qq) / NRestarts=$(systemctl show giftia-qq -p NRestarts --value) / Result=$(systemctl show giftia-qq -p Result --value)"
sudo journalctl -u giftia-qq -n 7 --no-pager | tail -7

echo
echo "=== 6. 记忆与库 ==="
sudo -n -u giftia /usr/bin/python3 /tmp/dbcount.py "$APP/data/giftia.db"
echo "  config 备份留档："; sudo ls /opt/giftia-config-*.bak
echo "SETTINGS_SYNC_DONE"
