#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

base=/opt/giftia-harness
live="$base/source/harness"
state=/var/lib/giftia-harness
profile="$state/dsh-home/profiles/isla-test"
releases=/opt/giftia/data/releases
stage=/opt/giftia-harness-test/stage5-20261118-native-ui-final-r2
service=giftia-harness.service
bundle=giftia-native-todo-bundle-0.1.0.tgz
files=(stage1.py web_gateway.py web/index.html web/chat.css web/chat.js web/sw.js)
export PYTHONSAFEPATH=1 PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$base/python:$base/source:$live"
export PATH="$base/node/bin:/usr/bin:/bin"

[[ $EUID == 0 && $(realpath -e "$live") == "$live" && $(realpath -e "$profile") == "$profile" ]]
cd -- "$live"
exec 9>"$state/release.lock"
flock -n 9

check_queue() {
  python3 - "$state/web.db" <<'PY'
from pathlib import Path
import sqlite3, sys

with sqlite3.connect(Path(sys.argv[1]).as_uri() + '?mode=ro', uri=True) as database:
    active, = database.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running','committing')").fetchone()
print(f'Active web jobs: {active}')
raise SystemExit(bool(active))
PY
}

healthy() {
  local health
  for _ in {1..42}; do
    if systemctl is-active --quiet "$service" &&
        health=$(curl -fs --max-time 3 -H 'Host: giftia.icu' -H 'X-Forwarded-Proto: https' http://127.0.0.1:8111/api/health) &&
        grep -q '"synthetic": false' <<< "$health"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

install_files() {
  local directory=$1 file
  for file in "${files[@]}"; do
    install -m 0644 -o root -g root "$directory/$file" "$live/$file" || return
  done
}

restore() {
  local checkpoint=$1
  systemctl stop "$service" || return
  install_files "$checkpoint/source" || return
  mv -- "$profile" "$(mktemp -d "$checkpoint/failed-profile-XXXXXXXX")/profile" || return
  cp -a -- "$checkpoint/profile" "$profile" || return
  systemctl start "$service" && healthy
}

verify_public() {
  local expected=$1 path file actual
  for path in / /chat.css /chat.js /sw.js; do
    case "$path" in /) file=index.html ;; *) file=${path#/} ;; esac
    actual=$(curl -fsS --max-time 8 "https://giftia.icu$path" | sha256sum | cut -d ' ' -f 1) || return
    [[ "$actual" == "$(sha256sum "$expected/web/$file" | cut -d ' ' -f 1)" ]] || return 1
  done
  curl -fsS --max-time 8 https://giftia.icu/api/health | grep -q '"synthetic": false'
}

case "${1:?Expected apply or rollback}" in
  apply)
    archive="$stage/release/$bundle"
    checksum=${2:?Expected tested bundle SHA-256}
    [[ $checksum =~ ^[a-f0-9]{64}$ && -f "$archive" ]]
    printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check --status
    healthy
    check_queue
    checkpoint=$(mktemp -d "$releases/native-ui-XXXXXXXX")
    echo "Rollback checkpoint $checkpoint"
    mkdir -p -- "$checkpoint/source/web"
    for file in "${files[@]}"; do
      cp -- "$live/$file" "$checkpoint/source/$file"
    done
    cp -a -- "$profile" "$checkpoint/profile"
    (cd -- "$checkpoint" && sha256sum "${files[@]/#/source/}" profile/package.json > checksums.sha256)
    changed=false
    on_failure() {
      trap - ERR
      if "$changed"; then
        restore "$checkpoint" || echo 'Automatic rollback requires attention' >&2
      else
        systemctl start "$service" && healthy || echo 'Service restart requires attention' >&2
      fi
      exit 1
    }
    trap on_failure ERR
    systemctl stop "$service"
    check_queue
    changed=true
    install_files "$stage/source/harness"
    install -m 0600 -o giftia -g giftia "$archive" "$state/packages/$bundle"
    /usr/sbin/runuser -u giftia -- env "PATH=$PATH" "PYTHONPATH=$PYTHONPATH" \
      python3 - "$state" "$state/packages/$bundle" <<'PY'
import json
from pathlib import Path
import sys
from stage1 import Lab, PROFILE

lab = Lab(Path(sys.argv[1]))
lab.command('plugin', '--profile', PROFILE, 'add', sys.argv[2])
installed = json.loads((lab.profile / 'package.json').read_text(encoding='utf-8'))['dependencies']
required = {'giftia-isla-harness-bundle', 'giftia-sample-tool-bundle', 'giftia-chat-tool-bundle', 'giftia-native-todo-bundle'}
if not required <= installed.keys():
    raise RuntimeError('Profile is missing a required bundle')
PY
    systemctl start "$service"
    healthy
    verify_public "$stage/source/harness"
    trap - ERR
    echo 'Published native todo and updated chat assets on giftia.icu'
    ;;
  rollback)
    checkpoint=$(realpath -e "${2:?Expected a native UI checkpoint}")
    [[ "$checkpoint" == "$releases/native-ui-"* && -f "$checkpoint/profile/package.json" ]]
    (cd -- "$checkpoint" && sha256sum --check --status checksums.sha256)
    check_queue
    systemctl stop "$service"
    if ! check_queue; then systemctl start "$service"; exit 1; fi
    restore "$checkpoint"
    verify_public "$checkpoint/source"
    ;;
  *) exit 2 ;;
esac
