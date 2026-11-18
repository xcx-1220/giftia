#!/usr/bin/env bash
set -euo pipefail

base=$(realpath -e "${1:?Candidate directory required}")
case "$base" in
  /opt/giftia-harness-test/stage5-*) ;;
  *) echo "Expected a Stage 5 candidate under the existing test directory" >&2; exit 2 ;;
esac

case "${2:?State name required}" in
  state) state="$base/state"; unit=giftia-harness-web-stage5 ;;
  restored) state="$base/restored"; unit=giftia-harness-web-stage5-restore ;;
  *) exit 2 ;;
esac
[[ "$(realpath -e "$state")" == "$state" ]]

nodepath=/opt/giftia-harness-test/toolchain/node-v24.11.1-linux-x64/bin:/usr/bin:/bin
pypath="$base/python:$base/source"
source_dir="$base/source/harness"
case "${3:?Action required}" in
  prepare)
    sudo -n -u giftia-harness-test env PATH="$nodepath" PYTHONPATH="$pypath" \
      /usr/bin/python3 "$source_dir/stage5_probe.py" --root "$state" prepare
    ;;
  start)
    sudo -n systemd-run --unit="$unit" --service-type=exec \
      --uid=giftia-harness-test --gid=giftia-harness-test \
      -p "WorkingDirectory=$state" -p UMask=0077 -p KillMode=mixed -p TimeoutStopSec=35s \
      -p NoNewPrivileges=yes -p PrivateTmp=yes -p PrivateDevices=yes -p ProtectSystem=strict -p ProtectHome=yes \
      -p InaccessiblePaths=/opt/giftia -p "ReadWritePaths=$state" -p 'RestrictAddressFamilies=AF_UNIX AF_INET' \
      -p IPAddressDeny=any -p IPAddressAllow=localhost -p MemoryMax=512M \
      --setenv="PATH=$nodepath" --setenv="PYTHONPATH=$pypath" \
      /usr/bin/python3 "$source_dir/stage1.py" --root "$state" --gateway-port 18121 --model-port 18122 serve-web
    for _ in $(seq 1 60); do
      if curl -fsS --max-time 1 http://127.0.0.1:18121/api/health 2>/dev/null; then
        printf '\n'
        exit 0
      fi
      systemctl is-active --quiet "$unit" || break
      sleep 1
    done
    sudo -n journalctl -u "$unit" -n 22 --no-pager
    exit 1
    ;;
  *) exit 2 ;;
esac
