#!/usr/bin/env bash
set -u
echo "=== 1. 服务稳定性（重启后是否一直活着、有无报错）==="
systemctl is-active giftia-qq
echo "启动于: $(systemctl show giftia-qq -p ActiveEnterTimestamp --value)"
echo "重启次数: $(systemctl show giftia-qq -p NRestarts --value)  结果: $(systemctl show giftia-qq -p Result --value)"
echo "--- 最近 12 行日志（找 ERROR/Traceback/断开）---"
sudo journalctl -u giftia-qq -n 12 --no-pager | tail -12
echo "--- 有无异常关键字 ---"
sudo journalctl -u giftia-qq --since "-10min" --no-pager | grep -ciE "traceback|error|异常|失败" || echo 0

echo
echo "=== 2. 备份清单 ==="
sudo ls -la /opt/*.tgz 2>/dev/null
sudo ls -la /opt/giftia/config/*.bak* 2>/dev/null

echo
echo "=== 3. 服务器上的代码版本与本地是否一致 ==="
cd /opt/giftia
echo "  store.py        $(sha256sum giftia/store.py | cut -c1-16)"
echo "  qq_official.py  $(sha256sum adapters/qq_official.py | cut -c1-16)"
echo "  brain.py        $(sha256sum giftia/brain.py | cut -c1-16)"
echo "  settings.toml   $(sha256sum config/settings.toml | cut -c1-16)"

echo
echo "=== 4. 她的状态（不打印记忆内容）==="
sudo -n -u giftia /usr/bin/python3 /tmp/dbcount.py /opt/giftia/data/giftia.db
echo "配置文件语法自检："
cd /opt/giftia && sudo -n -u giftia /usr/bin/python3 -c "
from giftia.config import load_settings
s = load_settings()
print('  config 解析 OK，阶段数 =', len(s.stages), '，outreach =', s.outreach.enabled)
" 2>&1

echo
echo "=== 5. 清理临时文件 ==="
rm -f /tmp/giftia-src.tar.gz
rm -rf /tmp/giftia-src /tmp/bkcheck
echo "已清理 /tmp（部署脚本保留：/tmp/deploy_run.sh /tmp/install_from_tar.sh 等，可随时再跑）"
echo "=== ALL_CHECKS_DONE ==="
