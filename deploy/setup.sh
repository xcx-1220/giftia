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
say "1/6 检查 Python"
command -v python3 >/dev/null 2>&1 || die "没有 python3，请先：apt-get update && apt-get install -y python3"
python3 -c 'import sys, tomllib; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || die \
"python3 版本过低（$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')）。
       本项目的配置加载器依赖 tomllib，需要 Python 3.11+：
         * 换 Ubuntu 24.04 LTS（3.12）或 Debian 12（3.11）镜像最省事；
         * Ubuntu 22.04 自带的是 3.10，会直接跑不起来。
       若你另装了 python3.11+，请把 deploy/${SERVICE}.service 里的 ExecStart 改成对应路径。"
echo "Python $(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])') ✓　零第三方依赖，不需要 pip install"

# ── 2. 系统用户 ────────────────────────────────────────────────
say "2/6 创建系统用户"
if id -u "$APP_USER" >/dev/null 2>&1; then
  echo "用户 $APP_USER 已存在，跳过"
else
  useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
  echo "已创建系统用户 $APP_USER"
fi

# ── 3. 安装代码 ────────────────────────────────────────────────
say "3/6 安装到 $APP_DIR"
mkdir -p "$APP_DIR"
if [ "$SRC_DIR" != "$APP_DIR" ]; then
  # 代码全量覆盖；config/ 单独处理，而且**分两层**：
  #   persona.toml / settings.toml        ← 仓库的东西（默认值 + 文档），跟部署走
  #   *.local.toml                        ← 你自己的调整，永不覆盖
  #
  # 以前是"整份 toml 都不覆盖"，看着安全，其实有个很难发现的坑：
  # toml 里写了某个键，它就**压过代码默认值** —— 于是代码里改好的默认值
  # 部署上去根本没生效，还没有任何提示（键存在就优先）。
  # 分层之后：默认值改动自动生效，你的调整也永远安全。
  tar -C "$SRC_DIR" \
      --exclude=./data \
      --exclude=./.git \
      --exclude=./.review \
      --exclude=./config \
      --exclude=./__pycache__ \
      --exclude='*/__pycache__' \
      -cf - . | tar -C "$APP_DIR" -xf -
  mkdir -p "$APP_DIR/config"
  for f in persona.toml settings.toml; do
    if [ -f "$APP_DIR/config/$f" ] && ! cmp -s "$SRC_DIR/config/$f" "$APP_DIR/config/$f"; then
      # 内容真的变了才备份；顺便把"你手写的值"留个后路（迁移期很有用）
      cp "$APP_DIR/config/$f" "$APP_DIR/config/$f.bak-$(date +%Y%m%d-%H%M%S)"
      echo "  旧的 config/$f 有改动，已备份为 .bak-…"
    fi
    cp "$SRC_DIR/config/$f" "$APP_DIR/config/$f"
  done
  for f in persona.local.toml settings.local.toml; do
    if [ -f "$APP_DIR/config/$f" ]; then
      echo "  保留你自己的 config/$f"
    elif [ -f "$SRC_DIR/config/$f.example" ]; then
      cp "$SRC_DIR/config/$f.example" "$APP_DIR/config/$f"
      echo "  种了一份 config/$f（模板；往里写覆盖就生效）"
    fi
    # 模板本身也留一份在服务器上：README 让你"照 *.example 复制"，
    # 不带上它就等于那句话没法照做（同步检查也会报"服务器少了这个文件"）。
    if [ -f "$SRC_DIR/config/$f.example" ]; then
      cp "$SRC_DIR/config/$f.example" "$APP_DIR/config/$f.example"
    fi
  done
  echo "已从 $SRC_DIR 复制（data/ 不动；config 的仓库部分已更新，你的 *.local.toml 原样保留）"
else
  echo "源目录就是 $APP_DIR，跳过复制"
fi

mkdir -p "$APP_DIR/data"
[ -f "$APP_DIR/.env" ] || die \
"没有找到 $APP_DIR/.env。
       请先把凭据填好再跑这个脚本（.env 在项目根目录，和本机那份一样）：
         DEEPSEEK_API_KEY=sk-...
         QQ_BOT_APPID=...      （还没申请可以先留空，见最后一步）
         QQ_BOT_SECRET=..."
chmod 600 "$APP_DIR/.env"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
echo "代码就位；.env 权限已收成 600（仅 $APP_USER 可读）"

# 提前看清凭据齐不齐 —— 缺 QQ 凭据就别启动，否则 systemd 会每 5 秒重启一次
HAS_LLM=0; HAS_QQ=0
grep -qE '^[[:space:]]*DEEPSEEK_API_KEY=[^[:space:]]' "$APP_DIR/.env" && HAS_LLM=1
grep -qE '^[[:space:]]*QQ_BOT_APPID=[^[:space:]]' "$APP_DIR/.env" && \
  grep -qE '^[[:space:]]*QQ_BOT_SECRET=[^[:space:]]' "$APP_DIR/.env" && HAS_QQ=1
echo "DeepSeek key：$([ $HAS_LLM -eq 1 ] && echo 已配置 || echo '缺失')　QQ 凭据：$([ $HAS_QQ -eq 1 ] && echo 已配置 || echo '还没申请')"

# ── 4. 环境自检（不需要联网、不需要 API key）───────────────────
say "4/6 环境自检（跑一遍项目自带的端到端测试）"
# 自检用的解释器必须与服务的 ExecStart 是同一个，否则会出现"自检通过、服务起不来"
# （例如 PATH 里是用户装的 3.12，而 ExecStart 写死 /usr/bin/python3 = 3.10，没有 tomllib）。
SERVICE_PY="$(sed -n 's|^ExecStart=\([^ ]*\).*|\1|p' "$SRC_DIR/deploy/${SERVICE}.service" | head -n1)"
[ -n "$SERVICE_PY" ] || SERVICE_PY="/usr/bin/python3"
if [ ! -x "$SERVICE_PY" ]; then
  die "服务里写的解释器不存在：$SERVICE_PY。请改 deploy/${SERVICE}.service 的 ExecStart。"
fi
echo "自检解释器：$SERVICE_PY（与 service 的 ExecStart 一致）"
if sudo -u "$APP_USER" "$SERVICE_PY" "$APP_DIR/tools/selftest.py" >/tmp/giftia-selftest.log 2>&1; then
  tail -n 3 /tmp/giftia-selftest.log | sed 's/^/  /'
  echo "自检通过 —— 这台机器上她能正常跑。"
else
  echo "!! 自检没通过，最后 15 行如下："
  tail -n 15 /tmp/giftia-selftest.log | sed 's/^/  /'
  die "环境自检失败。把上面这段发给我/贴到 issue 里就能定位。"
fi

# ── 5. systemd ────────────────────────────────────────────────
say "5/6 安装 systemd 服务"
install -m 644 "$SRC_DIR/deploy/${SERVICE}.service" "/etc/systemd/system/${SERVICE}.service"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
echo "已注册为开机自启"

# ── 6. 启动 ────────────────────────────────────────────────────
say "6/6 启动"
if [ $HAS_QQ -eq 1 ]; then
  systemctl restart "$SERVICE"
  sleep 3
  systemctl --no-pager --lines=20 status "$SERVICE" || true
else
  echo "QQ 凭据还没填 —— 先不启动（否则她会每 5 秒重启一次）。"
  echo "等你拿到 AppID/AppSecret 填进 $APP_DIR/.env，再执行："
  echo "    sudo systemctl start ${SERVICE}"
fi

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
