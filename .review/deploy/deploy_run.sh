#!/usr/bin/env bash
# Giftia 部署（由 lead 从本机执行；服务器侧脚本）
# 顺序：备份 data/ → 停服（SIGTERM 让未整理的对话落库）→ 取新代码 → setup.sh → 验证
# 用法：bash /tmp/deploy_run.sh <期望的 commit 短 SHA>
# 注意：**不用 set -e** —— 每一步都显式检查退出码并打印原因，
# 免得像第一次那样"静默退出、只留一个 exit 1"让人不知道停在哪。
set -uo pipefail

die() { echo; echo "!! 停在第 $1 步：$2"; exit "${3:-1}"; }

WANT_SHA="${1:-}"
REPO="https://github.com/xcx-1220/giftia.git"
SRC=/tmp/giftia-src
APP=/opt/giftia
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="/opt/giftia-backup-$STAMP.tgz"

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

say "0. 前置检查"
command -v git >/dev/null || die 0 "服务器没有 git"
[ -d "$APP/data" ] || die 0 "找不到 $APP/data —— 不继续，避免装出一个空档案"
[ -f "$APP/data/giftia.db" ] || die 0 "找不到 $APP/data/giftia.db"
echo "server: $(hostname)  disk free: $(df -h /opt | awk 'NR==2{print $4}')"
echo "部署前条数："; sudo -n -u giftia python3 /tmp/dbcount.py /opt/giftia/data/giftia.db 2>&1

say "1. 先备份她的记忆（服务还在跑，整目录 tar；WAL 一起进包）"
sudo tar czf "$BACKUP" -C "$APP" data || die 1 "tar 备份失败（磁盘满？）"
sudo chmod 600 "$BACKUP"
echo "备份：$BACKUP  ($(sudo du -h "$BACKUP" | cut -f1))"
echo "包内容：$(sudo tar tzf "$BACKUP" | wc -l) 个条目"

say "2. 停服（systemd 发 SIGTERM，她借这段时间把未整理的对话存成记忆）"
sudo systemctl stop giftia-qq || die 2 "systemctl stop 失败"
sleep 2
systemctl is-active giftia-qq >/dev/null 2>&1 && die 2 "服务没有停下来，不继续" || echo "已停止 ✓"

say "3. 停服后再备一份 data/（这次库是干净关闭的）"
sudo tar czf "/opt/giftia-backup-clean-$STAMP.tgz" -C "$APP" data || die 3 "第二次备份失败"
sudo chmod 600 "/opt/giftia-backup-clean-$STAMP.tgz"
echo "干净备份：/opt/giftia-backup-clean-$STAMP.tgz ($(sudo du -h "/opt/giftia-backup-clean-$STAMP.tgz" | cut -f1))"
echo "停服后条数（应与部署前一致或更多）："; sudo -n -u giftia python3 /tmp/dbcount.py /opt/giftia/data/giftia.db 2>&1

say "4. 取新代码"
rm -rf "$SRC"
git clone --quiet --depth 1 "$REPO" "$SRC" || die 4 "服务器无法 clone GitHub"
cd "$SRC" || die 4 "进不去 $SRC"
GOT_SHA=$(git rev-parse --short HEAD)
echo "克隆到 $SRC，HEAD=$GOT_SHA"
if [ -n "$WANT_SHA" ] && [ "$GOT_SHA" != "$WANT_SHA" ]; then
  die 4 "服务器拿到的提交（$GOT_SHA）与本地期望（$WANT_SHA）不一致" 3
fi
echo "新版本特征自检：respond_safely=$(grep -c respond_safely adapters/qq_official.py) ALL_SCOPES=$(grep -c ALL_SCOPES giftia/store.py)"
[ "$(grep -c respond_safely adapters/qq_official.py)" -gt 0 ] || die 4 "克隆到的代码不是修复版"
git log --oneline -3

say "5. 跑 setup.sh（会保留已有 data/ 与已改过的 config/）"
sudo bash deploy/setup.sh > /tmp/setup.log 2>&1
SETUP_RC=$?
tail -30 /tmp/setup.log
if [ $SETUP_RC -ne 0 ]; then
  echo
  echo "!! setup.sh 返回 $SETUP_RC。她的记忆没动（data/ 与备份都在），下面是收尾检查："
fi

say "6. 验证"
sleep 4
echo "服务：$(systemctl is-active giftia-qq 2>/dev/null) / enabled=$(systemctl is-enabled giftia-qq 2>/dev/null)"
echo "部署后条数："; sudo -n -u giftia python3 /tmp/dbcount.py /opt/giftia/data/giftia.db 2>&1
echo "代码版本特征：respond_safely=$(grep -c respond_safely $APP/adapters/qq_official.py) ALL_SCOPES=$(grep -c ALL_SCOPES $APP/giftia/store.py)"
echo "config 保留情况："
diff -q "$SRC/config/settings.toml" "$APP/config/settings.toml" >/dev/null && echo "  settings.toml 与仓库一致（说明服务器没改过）" || echo "  settings.toml 是服务器自己的版本（已保留）"
echo "文件清单："; ls -la "$APP/data" | head -8

say "7. 最近日志"
sudo journalctl -u giftia-qq -n 14 --no-pager | tail -14
echo
echo "备份留档：$BACKUP"
echo "DEPLOY_DONE"
