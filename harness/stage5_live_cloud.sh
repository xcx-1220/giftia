#!/usr/bin/env bash
set -euo pipefail

base=$(realpath -e "${1:?Candidate directory required}")
case "$base" in
  /opt/giftia-harness-test/stage5-*) ;;
  *) exit 2 ;;
esac
case "${2:?Lane required}" in
  smoke) web_port=18131; relay_port=18132; budget=3; days=1; extra=(--pdf-only) ;;
  r1) web_port=18141; relay_port=18142; budget=60; days=7; extra=() ;;
  *) exit 2 ;;
esac

lane=$2
state="$base/$lane"
source_dir="$base/source/harness"
nodepath=/opt/giftia-harness-test/toolchain/node-v24.11.1-linux-x64/bin:/usr/bin:/bin
pypath="$base/python:$base/source"
key="$base/credentials/$lane"
provider="$base/credentials/provider"
web="giftia-harness-$lane-web"
relay="giftia-harness-$lane-relay"

case "${3:?Action required}" in
  prepare)
    [[ ! -e "$state" && ! -e "$key" ]]
    install -d -m 0700 -o giftia-harness-test -g giftia-harness-test "$state"
    install -d -m 0700 "$base/credentials"
    (umask 077; /usr/bin/python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$key")
    sudo -n -u giftia-harness-test env PATH="$nodepath" PYTHONPATH="$pypath" \
      /usr/bin/python3 "$source_dir/stage5_probe.py" --root "$state" \
      --gateway-port "$web_port" --model-port "$relay_port" prepare
    sed -i 's/streamIdleTimeoutMs: 5000/streamIdleTimeoutMs: 40000/' \
      "$state/dsh-home/profiles/isla-test/cordis.patch.yml"
    ;;
  start)
    [[ "$(realpath -e "$state")" == "$state" && -s "$key" && -s "$provider" ]]
    systemd-run --unit="$relay" --service-type=exec \
      -p DynamicUser=yes -p "StateDirectory=$relay" -p StateDirectoryMode=0700 \
      -p "LoadCredential=deepseek:$provider" -p "LoadCredential=relay:$key" \
      -p UMask=0077 -p NoNewPrivileges=yes -p PrivateTmp=yes -p ProtectSystem=strict \
      -p ProtectHome=yes -p InaccessiblePaths=/opt/giftia -p MemoryMax=96M \
      /usr/bin/python3 "$source_dir/stage5_relay.py" --port "$relay_port" --limit "$budget" --days "$days" "${extra[@]}"
    systemd-run --unit="$web" --service-type=exec --uid=giftia-harness-test --gid=giftia-harness-test \
      -p "WorkingDirectory=$state" -p "LoadCredential=relay:$key" -p UMask=0077 -p KillMode=mixed -p TimeoutStopSec=35s \
      -p NoNewPrivileges=yes -p PrivateTmp=yes -p PrivateDevices=yes -p ProtectSystem=strict -p ProtectHome=yes \
      -p InaccessiblePaths=/opt/giftia -p "ReadWritePaths=$state" -p 'RestrictAddressFamilies=AF_UNIX AF_INET' \
      -p IPAddressDeny=any -p IPAddressAllow=localhost -p MemoryMax=512M \
      --setenv="PATH=$nodepath" --setenv="PYTHONPATH=$pypath" \
      /usr/bin/python3 "$source_dir/stage1.py" --root "$state" --gateway-port "$web_port" --model-port "$relay_port" \
      --live-relay serve-web
    for _ in $(seq 1 60); do
      if systemctl is-active --quiet "$relay" && curl -fsS --max-time 1 "http://127.0.0.1:$web_port/api/health"; then
        printf '\n'
        exit 0
      fi
      systemctl is-active --quiet "$web" || break
      sleep 1
    done
    journalctl -u "$web" -u "$relay" -n 25 --no-pager
    exit 1
    ;;
  *) exit 2 ;;
esac
