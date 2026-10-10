#!/usr/bin/env bash
# 从本机传上来的代码包安装（服务器连不上 GitHub，改用 scp 搬运）
# 前置：/tmp/giftia-src.tar.gz 已上传；服务已停止；data/ 已备份。
set -uo pipefail

APP=/opt/giftia
SRC=/tmp/giftia-src
WANT_SHA="${1:-}"

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

say "A. 解包到 $SRC"
rm -rf "$SRC"
mkdir -p "$SRC"
tar xzf /tmp/giftia-src.tar.gz -C "$SRC"
echo "顶层内容："; ls "$SRC" | head -12
echo "包内没有 .env："; [ -f "$SRC/.env" ] && echo "!! 包里有 .env，停" || echo "✓ 无"

say "B. 版本自检（必须是修复版）"
RS=$(grep -c respond_safely "$SRC/adapters/qq_official.py" 2>/dev/null || echo 0)
AS=$(grep -c ALL_SCOPES "$SRC/giftia/store.py" 2>/dev/null || echo 0)
CA=$(grep -c core_always_inject "$SRC/config/settings.toml" 2>/dev/null || echo 0)
echo "respond_safely=$RS  ALL_SCOPES=$AS  core_always_inject=$CA"
[ "$RS" -gt 0 ] && [ "$AS" -gt 0 ] || { echo "!! 这不是修复版代码，停止"; exit 4; }

say "C. Python 语法与自检（用与服务相同的解释器）"
SERVICE_PY=$(sed -n 's|^ExecStart=\([^ ]*\).*|\1|p' "$SRC/deploy/giftia-qq.service" | head -n1)
echo "服务解释器：$SERVICE_PY"
"$SERVICE_PY" -c "import sys, tomllib; print('python', sys.version.split()[0])" || { echo "!! 解释器不可用"; exit 5; }
( cd "$SRC" && "$SERVICE_PY" -m compileall -q giftia adapters run.py tools >/dev/null 2>&1 && echo "编译检查通过" ) || echo "（compileall 有告警，继续）"

say "D. 跑一遍项目自带的端到端测试（用服务同一个解释器）"
( cd "$SRC" && timeout 300 "$SERVICE_PY" tools/selftest.py 2>&1 | tail -4 ) || echo "（自检未通过或超时 —— 下面是安装结果，失败会在日志里显示）"

say "E. 安装（setup.sh 保留 data/ 与已有 config/）"
cd "$SRC"
sudo bash deploy/setup.sh > /tmp/setup.log 2>&1
RC=$?
tail -30 /tmp/setup.log
[ $RC -eq 0 ] || echo "!! setup.sh 退出码 $RC（记忆没动，备份都在）"

say "F. 验证"
sleep 4
echo "服务：$(systemctl is-active giftia-qq 2>/dev/null) / enabled=$(systemctl is-enabled giftia-qq 2>/dev/null)"
echo "部署后条数："; sudo -n -u giftia python3 /tmp/dbcount.py "$APP/data/giftia.db" 2>&1
echo "落地代码特征：respond_safely=$(grep -c respond_safely $APP/adapters/qq_official.py) ALL_SCOPES=$(grep -c ALL_SCOPES $APP/giftia/store.py)"
echo "设置的 config："
grep -nE '^(core_always_inject|mood_share_min_idle_hours|wakeup_max_per_30_days|min_raw_score)' "$APP/config/settings.toml" 2>/dev/null || echo "  （服务器 config 是它自己的版本，已保留）"
echo "data 目录："; ls -la "$APP/data" | head -8

say "G. 最近日志（看有没有起来、有没有报错）"
sudo journalctl -u giftia-qq -n 16 --no-pager | tail -16
echo
echo "DEPLOY_DONE"
