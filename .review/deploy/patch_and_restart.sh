#!/usr/bin/env bash
# 给服务器 config 补上新键（先备份），然后重启服务并验证
set -u
APP=/opt/giftia

echo "=== 1. 备份 config ==="
sudo cp -a "$APP/config/settings.toml" "/opt/giftia-settings-$STAMP.toml.bak" 2>/dev/null
sudo -n -u giftia cp "$APP/config/settings.toml" "/opt/giftia-settings-servercopy-$(date +%Y%m%d-%H%M%S).bak"
echo "备份完成"

echo
echo "=== 2. 追加缺失的新键 ==="
sudo -n -u giftia /usr/bin/python3 /tmp/patch_config.py 2>&1

echo
echo "=== 3. 确认值真的被读到了（用服务同一个解释器）==="
cd "$APP" && sudo -n -u giftia /usr/bin/python3 - <<'PY'
from giftia.config import load_settings
s = load_settings()
m, o = s.memory, s.outreach
print(f"  core_always_inject={m.core_always_inject} max={m.core_always_inject_max} chars={m.core_max_chars}")
print(f"  min_raw_score={m.min_raw_score} recall_prefilter={m.recall_prefilter}")
print(f"  mood_share_min_idle_hours={o.mood_share_min_idle_hours} wakeup_max_per_30_days={o.wakeup_max_per_30_days}")
print(f"  原有值仍在：retention_unit_hours={m.retention_unit_hours} target={o.target} max_per_day={o.max_per_day} tier={s.runtime.response_tier}")
PY

echo
echo "=== 4. 重启服务使她按新 config 运行 ==="
sudo systemctl restart giftia-qq
sleep 6
echo "服务：$(systemctl is-active giftia-qq)"
sudo journalctl -u giftia-qq -n 10 --no-pager | tail -10

echo
echo "=== 5. 最终确认 ==="
sudo -n -u giftia /usr/bin/python3 /tmp/dbcount.py "$APP/data/giftia.db"
echo "代码特征：respond_safely=$(grep -c respond_safely $APP/adapters/qq_official.py)"
echo "PATCH_DONE"
