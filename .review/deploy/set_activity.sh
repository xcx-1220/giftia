#!/usr/bin/env bash
# 调高她在群里的活跃度：response_tier 45→70、speak_max_per_day 3→6
set -u
APP=/opt/giftia
CFG="$APP/config/settings.toml"
STAMP=$(date +%Y%m%d-%H%M%S)

echo "===== 1. 备份 ====="
sudo cp -a "$CFG" "/opt/giftia-config-settings-$STAMP.bak"
echo "  已备份 /opt/giftia-config-settings-$STAMP.bak"

echo
echo "===== 2. 改前 ====="
sudo grep -nE '^(response_tier|speak_max_per_day)' "$CFG" | sed 's/^/  /'

echo
echo "===== 3. 精确替换（只动这两行，其余一个字节不碰）====="
sudo python3 - "$CFG" <<'PY'
import re, sys
path = sys.argv[1]
src = open(path, encoding="utf-8").read()
before = src
# response_tier: 45 -> 70（保留行内注释里"36%→71%"的说明，重写注释使其与实际一致）
src = re.sub(
    r"^response_tier\s*=\s*\d+.*$",
    "response_tier = 70\n# 70 是第 4 档：群里没被 @ 时她按概率接话，概率 = (70-20)/70 ≈ 71%。\n"
    "# 调大 = 更活跃（也更吵）；想让她安静就调回 0~19（只在被 @ 时开口）。",
    src, count=1, flags=re.M)
src = re.sub(
    r"^speak_max_per_day\s*=\s*\d+.*$",
    "speak_max_per_day = 6",
    src, count=1, flags=re.M)
if src == before:
    print("  !! 没有匹配到要改的行，中止")
    sys.exit(3)
open(path, "w", encoding="utf-8").write(src)
print("  已写入")
PY
[ $? -eq 0 ] || { echo "替换失败"; exit 3; }

echo
echo "===== 4. 改后 ====="
sudo grep -nE '^(response_tier|speak_max_per_day)' "$CFG" | sed 's/^/  /'

echo
echo "===== 5. 配置能正常解析吗（用服务同一个解释器）====="
cd "$APP" && sudo -n -u giftia /usr/bin/python3 - <<'PY'
from giftia.config import load_settings
s = load_settings()
tier = s.runtime.response_tier
cap = s.runtime.speak_max_per_day
prob = 0 if tier < 20 else (0 if tier < 45 else min(1.0, (tier - 20) / 70.0))
print(f"  response_tier = {tier}")
print(f"  speak_max_per_day = {cap}")
print(f"  推导：没被 @ 时接话概率 ≈ {prob:.0%}；每个群每天最多自己插嘴 {cap} 次")
print(f"  被 @ 时不受这两个限制（一定回）")
PY

echo
echo "===== 6. 重启生效 ====="
sudo systemctl restart giftia-qq
sleep 6
echo "  服务：$(systemctl is-active giftia-qq) / NRestarts=$(systemctl show giftia-qq -p NRestarts --value) / Result=$(systemctl show giftia-qq -p Result --value)"
sudo journalctl -u giftia-qq -n 7 --no-pager | tail -7

echo
echo "===== 7. 记忆完好 ====="
sudo -n -u giftia /usr/bin/python3 - <<'PY'
import sqlite3
c = sqlite3.connect("/opt/giftia/data/giftia.db")
print("  messages/memories/dreams:",
      c.execute("select count(*) from messages").fetchone()[0],
      c.execute("select count(*) from memories").fetchone()[0],
      c.execute("select count(*) from dreams").fetchone()[0])
print("  integrity_check:", c.execute("PRAGMA integrity_check").fetchone()[0])
PY
echo "TIER_DONE"
