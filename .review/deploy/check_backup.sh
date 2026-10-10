#!/usr/bin/env bash
# 只读体检：备份是否完整 + 现在 live 数据的真实状态
set -u
BK=/opt/giftia-backup-20261010-121912.tgz

echo "=== A. 备份里的完整清单 ==="
sudo tar tzvf "$BK"
echo "备份大小: $(sudo du -h "$BK" | cut -f1)"

echo
echo "=== B. 现在 data/ 的真实状态 ==="
sudo ls -la /opt/giftia/data/

echo
echo "=== C. 服务是否还在跑 ==="
systemctl is-active giftia-qq 2>/dev/null || echo "(已停止)"

echo
echo "=== D. live 库的真实条数（从库里读，不是听备份说）==="
sudo -n -u giftia python3 /tmp/dbcount.py 2>&1

echo
echo "=== E. 把备份解到临时目录，验它真的能还原 ==="
sudo rm -rf /tmp/bkcheck && sudo mkdir -p /tmp/bkcheck
sudo tar xzf "$BK" -C /tmp/bkcheck
sudo chown -R ubuntu:ubuntu /tmp/bkcheck
ls -la /tmp/bkcheck/data/
echo "--- 从还原出来的库读条数 ---"
python3 /tmp/dbcount.py /tmp/bkcheck/data/giftia.db 2>&1

echo
echo "=== F. 两边比对（live vs 备份）==="
for f in giftia.db; do
  a=$(sudo sha256sum /opt/giftia/data/$f 2>/dev/null | cut -d' ' -f1)
  b=$(sha256sum /tmp/bkcheck/data/$f 2>/dev/null | cut -d' ' -f1)
  echo "$f  live=$a"
  echo "$f  bkup=$b"
  [ "$a" = "$b" ] && echo "  → 完全一致（备份时库是静止的）" || echo "  → 不一致（服务在写，属正常；以条数比对为准）"
done
echo "=== DONE ==="
