#!/usr/bin/env bash
# 只读：她现在到底会不会在群里主动说话（配置 + 实际发生过的记录）
set -u
APP=/opt/giftia

echo "===== A. 配置：三个相关旋钮 ====="
grep -nE '^(response_tier|speak_max_per_day|keywords|wake_delay_seconds|wakeup_max_per_30_days|use_wakeup|target|enabled|min_gap_hours|max_per_day|max_per_30_days|idle_days)' \
  "$APP/config/settings.toml" | sed 's/^/  /'
echo
echo "  各段归属："
awk '/^\[/{sec=$0} /^(response_tier|speak_max_per_day|keywords|wake_delay_seconds)/{print "   " sec " -> " $0}' "$APP/config/settings.toml"
awk '/^\[/{sec=$0} /^(use_wakeup|target|enabled|min_gap_hours|max_per_day|max_per_30_days|idle_days)/{print "   " sec " -> " $0}' "$APP/config/settings.toml"

echo
echo "===== B. 她记得"最近活跃的群"是哪个（主动发群消息要靠它）====="
sudo -n -u giftia python3 - <<'PY'
import sqlite3
c = sqlite3.connect("/opt/giftia/data/giftia.db")
r = c.execute("SELECT value FROM state WHERE key='last_group_openid'").fetchone()
print("  last_group_openid =", r[0] if r else "（空 —— 没有可发消息的群）")
for k in ("handler_id", "handler_name"):
    v = c.execute("SELECT value FROM state WHERE key=?", (k,)).fetchone()
    print(f"  {k} =", (v[0][:16] + "…") if v and v[0] else "（空）")
PY

echo
echo "===== C. 实际发生过什么（从上线到今天）====="
echo "--- 今天她自己插嘴的次数（speak_up 记账）---"
sudo -n -u giftia python3 - <<'PY'
import sqlite3, time
c = sqlite3.connect("/opt/giftia/data/giftia.db")
rows = c.execute("SELECT key, value FROM state WHERE key LIKE 'speak_up:%'").fetchall()
if not rows:
    print("  （账本为空：今天她一次都没自己插嘴）")
for k, v in rows:
    print(f"  {k} = {v}")
PY
echo "--- 主动联系的额度账本（outreach:log）---"
sudo -n -u giftia python3 - <<'PY'
import json, sqlite3
c = sqlite3.connect("/opt/giftia/data/giftia.db")
r = c.execute("SELECT value FROM state WHERE key='outreach:log'").fetchone()
log = json.loads(r[0]) if r and r[0] else []
print(f"  共发出过 {len(log)} 次主动消息")
for t in log[-6:]:
    print("   ", t)
PY

echo
echo "===== D. 日志里她主动开口的记录 ====="
echo "--- 自己插嘴（[主动·群里] 是她发到群里的）---"
sudo journalctl -u giftia-qq --since "today" --no-pager 2>/dev/null | grep -cE '\[主动·群里\]' || true
echo "--- 主动发私聊 ---"
sudo journalctl -u giftia-qq --since "today" --no-pager 2>/dev/null | grep -cE '\[主动·私聊\]' || true
echo "--- 被 @ 后回复（群里正常应答）---"
sudo journalctl -u giftia-qq --since "today" --no-pager 2>/dev/null | grep -cE '\[收到·群\]' || true
echo "--- 旁听（听见但不说话）---"
sudo journalctl -u giftia-qq --since "today" --no-pager 2>/dev/null | grep -cE '\[旁听\]|\[闭嘴\]' || true
echo "--- 最近 8 条与"开口决策"有关的日志 ---"
sudo journalctl -u giftia-qq --since "today" --no-pager 2>/dev/null | grep -E '收到·群|待答|主动·|旁听|闭嘴|聚批' | tail -8

echo
echo "===== E. 服务与群事件是否在流 ====="
systemctl is-active giftia-qq
sudo journalctl -u giftia-qq --since "-30min" --no-pager 2>/dev/null | tail -5
echo "===== DONE ====="
