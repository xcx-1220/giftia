#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

base=/opt/giftia-harness
state=/var/lib/giftia-harness
profile="$state/dsh-home/profiles/isla-test"
stage=/opt/giftia-harness-test/stage5-20261118-chat-tools-r2/source/harness
live="$base/source/harness"
releases=/opt/giftia/data/releases
service=giftia-harness.service
files=(role_service.py web/chat.js web/sw.js)
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
    ongoing, = database.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running','committing')").fetchone()
print(f'Active web jobs: {ongoing}')
raise SystemExit(bool(ongoing))
PY
}

healthy() {
  local health
  for _ in {1..24}; do
    if health=$(curl -fs --max-time 4 https://giftia.icu/api/health) &&
        grep -q '"synthetic": false' <<< "$health"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

restore_profile() {
  local checkpoint=$1
  systemctl stop "$service"
  for file in "${files[@]}"; do
    install -m 0644 -o root -g root "$checkpoint/source/$file" "$live/$file"
  done
  mv -- "$profile" "$profile.failed-$(basename "$checkpoint")"
  cp -a -- "$checkpoint/profile" "$profile"
  systemctl start "$service"
  healthy
}

case "${1:?Expected apply or rollback}" in
  apply)
    systemctl is-active --quiet "$service"
    healthy
    check_queue
    checkpoint=$(mktemp -d "$releases/chat-tools-XXXXXXXX")
    echo "Rollback checkpoint $checkpoint"
    mkdir -p -- "$checkpoint/source/web"
    for file in "${files[@]}"; do
      cp -- "$live/$file" "$checkpoint/source/$file"
    done
    cp -a -- "$profile" "$checkpoint/profile"
    changed=false
    on_failure() {
      trap - ERR
      if $changed; then
        restore_profile "$checkpoint" || echo 'Automatic rollback requires operator attention' >&2
      else
        systemctl start "$service" || echo 'Service restart requires operator attention' >&2
      fi
      exit 1
    }
    trap on_failure ERR
    systemctl stop "$service"
    check_queue
    changed=true
    for file in "${files[@]}" chat_tools_profile.py; do
      install -m 0644 -o root -g root "$stage/$file" "$live/$file"
    done
    install -d -m 0755 "$live/chat-tool-bundle"
    for file in package.json index.js cordis.patch.yml; do
      install -m 0644 -o root -g root "$stage/chat-tool-bundle/$file" "$live/chat-tool-bundle/$file"
    done
    /usr/sbin/runuser -u giftia -- env "PATH=$PATH" "PYTHONPATH=$PYTHONPATH" \
      python3 "$live/chat_tools_profile.py" --root "$state"
    systemctl start "$service"
    healthy
    chat=$(curl -fsS --max-time 8 https://giftia.icu/chat.js)
    worker=$(curl -fsS --max-time 8 https://giftia.icu/sw.js)
    grep -q '我算一下' <<< "$chat"
    grep -q 'ella-shell-harness-v2' <<< "$worker"
    trap - ERR
    echo 'Chat tools published on the existing origin'
    ;;
  rollback)
    checkpoint=$(realpath -e "${2:?Expected a chat tools checkpoint}")
    [[ "$checkpoint" == "$releases/chat-tools-"* && -f "$checkpoint/profile/package.json" ]]
    check_queue
    restore_profile "$checkpoint"
    ;;
  *) exit 2 ;;
esac
