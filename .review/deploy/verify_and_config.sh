#!/usr/bin/env bash
# 只读：确认服务在跑 + 找出服务器 config 缺哪些新键
set -u
APP=/opt/giftia

echo "=== 1. 服务与心跳（应 active）==="
systemctl is-active giftia-qq
sudo journalctl -u giftia-qq --since "-90s" --no-pager | tail -6

echo
echo "=== 2. 服务器 settings.toml 里有没有本次新增的键 ==="
for k in core_always_inject core_always_inject_max core_max_chars min_raw_score recall_prefilter \
         mood_share_min_idle_hours wakeup_max_per_30_days cite_min_chars dedupe_ratio; do
  if grep -qE "^[[:space:]]*$k[[:space:]]*=" "$APP/config/settings.toml"; then
    echo "  有   $k"
  else
    echo "  缺   $k"
  fi
done

echo
echo "=== 3. 用服务同一个解释器，确认新代码真的按默认值生效（不崩）==="
cd "$APP" && sudo -n -u giftia /usr/bin/python3 - <<'PY'
from giftia.config import load_settings
s = load_settings()
m = s.memory
print(f"  memory.core_always_inject   = {m.core_always_inject}")
print(f"  memory.core_always_inject_max = {m.core_always_inject_max}")
print(f"  memory.core_max_chars       = {m.core_max_chars}")
print(f"  memory.min_raw_score        = {m.min_raw_score}")
print(f"  memory.recall_prefilter     = {m.recall_prefilter}")
o = s.outreach
print(f"  outreach.mood_share_min_idle_hours = {o.mood_share_min_idle_hours}")
print(f"  outreach.wakeup_max_per_30_days    = {o.wakeup_max_per_30_days}")
print(f"  outreach.enabled={o.enabled} target={o.target} max_per_day={o.max_per_day} max_per_30_days={o.max_per_30_days}")
print(f"  lifespan.total_hours={s.lifespan.total_hours}  stages={len(s.stages)}")
print(f"  runtime.response_tier={s.runtime.response_tier} auto_bind={s.runtime.auto_bind}")
PY

echo
echo "=== 4. 数据库完整性 + 她的实例 ==="
sudo -n -u giftia /usr/bin/python3 /tmp/dbcount.py "$APP/data/giftia.db"
sudo -n -u giftia /usr/bin/python3 -c "
import sqlite3
c = sqlite3.connect('/opt/giftia/data/giftia.db')
print('  integrity_check:', c.execute('PRAGMA integrity_check').fetchone()[0])
print('  journal_mode:', c.execute('PRAGMA journal_mode').fetchone()[0])
"
echo "=== DONE ==="
