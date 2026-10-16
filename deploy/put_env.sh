#!/usr/bin/env bash
# 安全地改服务器上的 .env（凭据文件）。
#
# 为什么要这个东西：踩过两次。
#   ① `printf "A=x\ny\nB=z"` —— 转义在不同 shell 里会被吃掉，`\n` 变成字面量 n，
#      结果那行的值变成 "…ailanB=z"，口令直接失效。
#   ② `sudo sh -c "cat >> /opt/giftia/.env"` —— 重定向由**外层的非 root shell**
#      执行，报 Permission denied，而前一步的删除已经生效 → 键全没了、服务起不来。
#
# 所以规矩是：**写文件（LF）→ 传上去 → 用这个脚本合并 → sudo tee 落盘**。
# 全程不碰引号和转义。
#
# 用法（在服务器上，root）：
#   sudo bash /opt/giftia/deploy/put_env.sh /tmp/keys.env [--restart]
#
# /tmp/keys.env 里每行一个 KEY=VALUE，# 开头是注释，空行忽略。
# 已存在的同名键会被**替换**，其它键原样保留。

set -euo pipefail

ENVF="${ENVF:-/opt/giftia/.env}"
SERVICE="${SERVICE:-giftia-qq}"

if [ $# -lt 1 ]; then
  echo "用法: $0 <KEY=VALUE 的文件> [--restart]" >&2
  exit 2
fi

SRC="$1"
RESTART="${2:-}"

[ -f "$SRC" ] || { echo "找不到 $SRC" >&2; exit 2; }

# 收集要设置的键名（去掉注释和空行）
KEYS="$(grep -v '^[[:space:]]*#' "$SRC" | grep -v '^[[:space:]]*$' | cut -d= -f1 || true)"
if [ -z "$KEYS" ]; then
  echo "文件里没有任何 KEY=VALUE" >&2
  exit 2
fi

echo "要设置的键："
echo "$KEYS" | sed 's/^/  /'

# 1) 逐键删旧行（只删这几个键，别的键一个字都不动）
for k in $KEYS; do
  sudo sed -i "/^${k}=/d" "$ENVF"
done

# 2) 追加新行 —— **sudo tee** 是关键：重定向由 tee 以 root 身份做，
#    不受外层 shell 权限影响，也不经过任何转义
sudo tee -a "$ENVF" < "$SRC" > /dev/null

# 3) 权限：服务以 giftia 跑，文件必须它可读
sudo chown giftia:giftia "$ENVF"
sudo chmod 600 "$ENVF"

echo "已写入 $ENVF"
echo -n "现在这些键在不在："
for k in $KEYS; do
  if sudo grep -q "^${k}=" "$ENVF"; then echo -n " $k✓"; else echo -n " $k✗"; fi
done
echo

if [ "$RESTART" = "--restart" ]; then
  sudo systemctl restart "$SERVICE"
  sleep 6
  echo -n "$SERVICE: "
  systemctl is-active "$SERVICE"
else
  echo "（没重启。要生效：sudo systemctl restart $SERVICE）"
fi
