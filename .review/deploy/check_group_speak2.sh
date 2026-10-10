#!/usr/bin/env bash
set -u
echo "===== A. 三个旋钮（借 sudo 读 600 权限的 config）====="
sudo awk '/^\[/{sec=$0}
  /^(response_tier|speak_max_per_day|keywords|wake_delay_seconds)[[:space:]]*=/ {printf "  %-14s %s\n", sec, $0}
  /^(use_wakeup|target|enabled|min_gap_hours|max_per_day|max_per_30_days|idle_days|mood_share_min_idle_hours)[[:space:]]*=/ {printf "  %-14s %s\n", sec, $0}' \
  /opt/giftia/config/settings.toml

echo
echo "===== B. 今天的开口决策记录（逐条）====="
sudo journalctl -u giftia-qq --since "today" --no-pager 2>/dev/null | grep -E "旁听|闭嘴|待答|插嘴|主动" | tail -20

echo
echo "===== C. 计数：今天她都做了什么 ====="
L=$(sudo journalctl -u giftia-qq --since "today" --no-pager 2>/dev/null)
echo "  群里收到消息        : $(echo "$L" | grep -c '收到·群')"
echo "  其中被@点名         : $(echo "$L" | grep -c '收到·群·旁听' ) (旁听) / $(echo "$L" | grep -c '\[收到·群\]') (需应答)"
echo "  决定插嘴(待答)      : $(echo "$L" | grep -c '待答')"
echo "  因额度用完而闭嘴    : $(echo "$L" | grep -c '闭嘴')"
echo "  掷骰没过(旁听)      : $(echo "$L" | grep -c '旁听\]')"
echo "  真的发到群里        : $(echo "$L" | grep -c '主动·群里')"
echo "  主动发你私聊        : $(echo "$L" | grep -c '主动·私聊')"

echo
echo "===== D. 她现在还在收消息吗（最近 1 小时）====="
sudo journalctl -u giftia-qq --since "-1h" --no-pager 2>/dev/null | tail -6
echo "  （没有输出＝过去 1 小时群里和你都没说话，她安静等着）"

echo
echo "===== E. 服务 ====="
systemctl is-active giftia-qq
echo "  NRestarts=$(systemctl show giftia-qq -p NRestarts --value)"
echo "===== DONE ====="
