#!/usr/bin/env bash
set -euo pipefail

base=$(realpath -e "${1:?R2 candidate directory required}")
case "$base" in
  /opt/giftia-harness-test/stage5-*-r2) ;;
  *) echo "Expected an isolated R2 candidate directory" >&2; exit 2 ;;
esac
state="$base/state"
[[ "$(realpath -e "$state")" == "$state" ]]
source_dir="$base/source/harness"
nodepath=/opt/giftia-harness-test/toolchain/node-v24.11.1-linux-x64/bin:/usr/bin:/bin
pypath="$base/python:$base/source"
gateway=18151
model=18152
common=(--root "$state/benchmark" --gateway-port "$gateway" --model-port "$model")
unit=giftia-harness-r2-web
guard=(--uid=giftia-harness-test --gid=giftia-harness-test
  -p "WorkingDirectory=$state" -p UMask=0077 -p KillMode=mixed -p TimeoutStopSec=35s
  -p NoNewPrivileges=yes -p PrivateTmp=yes -p PrivateDevices=yes -p ProtectSystem=strict -p ProtectHome=yes
  -p InaccessiblePaths=/opt/giftia -p "ReadWritePaths=$state" -p 'RestrictAddressFamilies=AF_UNIX AF_INET'
  -p IPAddressDeny=any -p IPAddressAllow=localhost -p MemoryMax=512M
  --setenv="PATH=$nodepath" --setenv="PYTHONPATH=$pypath")

run_test() {
  sudo -n -u giftia-harness-test env PATH="$nodepath" PYTHONPATH="$pypath" /usr/bin/python3 "$@"
}
case "${2:?Action required}" in
  native-todo-test)
    sudo -n systemd-run --wait --pipe --collect --unit=giftia-harness-native-todo-check "${guard[@]}" \
      /usr/bin/python3 "$source_dir/verify_native_todo.py" --root "$state/native-todo-checks" \
      --gateway-port "$gateway" --model-port "$model" --pdf "$base/synthetic-reference.pdf"
    ;;
  chat-tools-test)
    sudo -n systemd-run --wait --pipe --collect --unit=giftia-harness-chat-tools-check "${guard[@]}" \
      /usr/bin/python3 "$source_dir/verify_chat_tools.py" --root "$state/chat-tools-checks" \
      --gateway-port "$gateway" --model-port "$model"
    ;;
  rollback-test)
    sudo -n systemd-run --wait --pipe --collect --unit=giftia-harness-r2-rollback-check "${guard[@]}" \
      /usr/bin/python3 "$source_dir/verify_r2_handoff.py" --root "$state/rollback-checks" \
      --gateway-port "$gateway" --model-port "$model"
    ;;
  https-test|https-assets)
    options=()
    if [[ "$2" == https-assets ]]; then options+=(--assets-only); fi
    sudo -n systemd-run --wait --pipe --collect --unit=giftia-harness-r2-https-check "${guard[@]}" \
      /usr/bin/python3 "$source_dir/verify_r2_release.py" --root "$state/https-checks" \
      --gateway-port "$gateway" --model-port "$model" "${options[@]}"
    ;;
  history-test)
    sudo -n systemd-run --wait --pipe --collect --unit=giftia-harness-r2-history-check "${guard[@]}" \
      /usr/bin/python3 "$source_dir/verify_r2_history.py" --root "$state/history-checks" \
      --gateway-port "$gateway" --model-port "$model"
    ;;
  restore-test)
    sudo -n systemd-run --wait --pipe --collect --unit=giftia-harness-r2-restore-check "${guard[@]}" \
      /usr/bin/python3 "$source_dir/verify_r2.py" --root "$state/recovery-checks" \
      --gateway-port "$gateway" --model-port "$model" --pdf "$base/synthetic-reference.pdf"
    ;;
  measure)
    run_test "$source_dir/stage5_probe.py" "${common[@]}" prepare
    sudo -n systemd-run --unit="$unit" --service-type=exec "${guard[@]}" \
      /usr/bin/python3 "$source_dir/stage1.py" "${common[@]}" serve-web
    trap 'sudo -n systemctl stop "$unit"' EXIT
    for _ in $(seq 1 60); do
      if curl -fsS --max-time 1 "http://127.0.0.1:$gateway/api/health" >/dev/null 2>&1; then
        cgroup=/sys/fs/cgroup$(systemctl show "$unit" -p ControlGroup --value)
        run_test "$source_dir/stage5_probe.py" "${common[@]}" --pdf "$base/synthetic-reference.pdf" --cgroup "$cgroup" measure
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
