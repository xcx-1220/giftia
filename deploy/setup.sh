#!/usr/bin/env bash
# Giftia 一键部署：Ubuntu 24.04 / Debian 12（需要 root 或 sudo）
#
#   sudo bash deploy/setup.sh
#
# 做五件事：检查 Python 版本 → 建系统用户 → 装到 /opt/giftia →
#           装 systemd 服务并开机自启 → 启动
#
# 可以重复运行：不会覆盖已有的 data/（她的记忆比部署脚本重要）。
set -euo pipefail

APP_USER="giftia"
APP_DIR="/opt/giftia"
SERVICE="giftia-qq"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n[错误] %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo bash deploy/setup.sh"

# ── 1. Python 版本（tomllib 需要 3.11+）────────────────────────
say "1/5 检查 Python"
command -v python3 >/dev/null 2>&1 || die "没有 python3，请先：apt-get update && apt-get install -y python3"
python3 -c 'import sys, tomllib; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || die \
"python3 版本过低（$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')）。
       本项目的配置加载器依赖 tomllib，需要 Python 3.11+：
         * 换 Ubuntu 24.04 LTS（3.12）或 Debian 12（3.11）镜像最省事；
         * Ubuntu 22.04 自带的是 3.10，会直接跑不起来。
       若你另装了 python3.11+，请把 deploy/${SERVICE}.service 里的 ExecStart 改成对应路径。"
echo "Python $(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])') ✓　零第三方依赖，不需要 pip install"

# ── 2. 系统用户 ────────────────────────────────────────────────
say "2/5 创建系统用户"
if id -u "$APP_USER" >/dev/null 2>&1; then
  echo "用户 $APP_USER 已存在，跳过"
else
  useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
  echo "已创建系统用户 $APP_USER"
fi

# ── 3. 安装代码 ────────────────────────────────────────────────
say "3/5 安装到 $APP_DIR"
mkdir -p "$APP_DIR"
if [ "$SRC_DIR" != "$APP_DIR" ]; then
  tar -C "$SRC_DIR" \
      --exclude=./data \
      --exclude=./.git \
      --exclude=./__pycache__ \
      --exclude='*/__pycache__' \
      -cf - . | tar -C "$APP_DIR" -xf -
  echo "已从 $SRC_DIR 复制（data/ 不动）"
else
  echo "源目录就是 $APP_DIR，跳过复制"
fi

mkdir -p "$APP_DIR/data"
[ -f "$APP_DIR/.env" ] || die \
"没有找到 $APP_DIR/.env。
       请先把凭据填好再跑这个脚本，至少要有：
         DEEPSEEK_API_KEY=...
         QQ_BOT_APPID=...
         QQ_BOT_SECRET=...
         QQ_OWNER_OPENID=...   （建议填，一个她只服务一个搭档）"
chmod 600 "$APP_DIR/.env"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
echo "代码就位；.env 权限已收成 600（仅 $APP_USER 可读）"

# ── 4. systemd ────────────────────────────────────────────────
say "4/5 安装 systemd 服务"
install -m 644 "$SRC_DIR/deploy/${SERVICE}.service" "/etc/systemd/system/${SERVICE}.service"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
echo "已注册为开机自启"

# ── 5. 启动 ────────────────────────────────────────────────────
say "5/5 启动"
systemctl restart "$SERVICE"
sleep 3
systemctl --no-pager --lines=20 status "$SERVICE" || true

cat <<EOF

部署完成。常用命令：
  journalctl -u ${SERVICE} -f      看她的日志（实时）
  systemctl restart ${SERVICE}     重启
  systemctl stop ${SERVICE}        停止（会先把未整理的对话存成记忆）
  systemctl status ${SERVICE}      状态
  tar czf ~/giftia-backup-\$(date +%F).tgz -C ${APP_DIR} data    备份她的记忆

三个容易踩的坑：
  1. 只在一台机器上跑。家里和服务器同时跑，会有两个她抢着回你的消息。
  2. 如果你在 QQ 开放平台开了 IP 白名单，要把本机公网 IP 加进去——
     否则她收得到消息，却发不出去。
  3. 不需要开放任何入站端口（除了 SSH）。她是主动连出去的，
     所以不需要公网 IP 映射、不需要域名、也不需要备案。

想改她的性格：编辑 ${APP_DIR}/config/persona.toml，然后 systemctl restart ${SERVICE}
EOF
