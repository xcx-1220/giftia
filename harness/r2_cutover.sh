#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

base=/opt/giftia-harness
state=/var/lib/giftia-harness
data=/opt/giftia/data
old=giftia-qq.service
new=giftia-harness.service
hostname=giftia.icu
source_dir="$base/source/harness"
export PATH="$base/node/bin:/usr/bin:/bin"
export PYTHONPATH="$base/python:$base/source:$source_dir"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONSAFEPATH=1

[[ $EUID == 0 ]] || exit 2
[[ "$(realpath -e "$base")" == "$base" && "$(realpath -e "$state")" == "$state" ]]
[[ "$(realpath -e "$data")" == "$data" && -f "$state/release.json" ]]
cd -- "$base/source"
exec 9>"$state/release.lock"
flock -n 9

old_health() {
  curl -fsS --max-time 5 "https://$hostname/api/health" | python3 -c \
    'import json,sys; assert json.load(sys.stdin).get("ok") is True'
}

new_health() {
  python3 -c 'import json,sys; j=json.load(sys.stdin); assert j["harness"]=="0.2.2-rc.3" and j["synthetic"] is False'
}

wait_health() {
  for _ in {1..25}; do
    if "$@"; then return 0; fi
    sleep 1
  done
  return 1
}

local_new() {
  curl -fsS --max-time 3 -H "Host: $hostname" -H 'X-Forwarded-Proto: https' \
    'http://127.0.0.1:8111/api/health' | new_health
}

public_new() {
  curl -fsS --max-time 5 -H 'Cache-Control: no-cache' "https://$hostname/api/health" | new_health
}

backup_giftia() {
  python3 -c 'from pathlib import Path; import sys; from stage5_recovery import sqlite_backup; sqlite_backup(Path(sys.argv[1]), Path(sys.argv[2]))' \
    "$data/giftia.db" "$1"
}

queue() {
  python3 -c 'import json,sys; from pathlib import Path; from time import time; from r2_preflight import inspect_legacy; r=inspect_legacy(Path(sys.argv[1]),Path(sys.argv[2]),time()); print(json.dumps(r)); sys.exit(bool(r["active"]))' \
    "$1" "$2"
}

revert() {
  local checkpoint=$1
  systemctl stop "$new"
  [[ ! -e "$checkpoint/giftia-after.db" ]] || return 1
  backup_giftia "$checkpoint/giftia-after.db"
  if [[ -f "$state/web.db" ]]; then
    if python3 "$source_dir/r2_rollback.py" --database "$checkpoint/giftia-after.db" \
        --web-database "$state/web.db" --manifest "$state/release.json" \
        --since "$(cat "$checkpoint/cutover-time")" --output "$checkpoint/web_requests.prepared.json"; then
      install -m 0600 -o giftia -g giftia "$checkpoint/web_requests.prepared.json" "$data/web_requests.r2.json"
      mv -f -- "$data/web_requests.r2.json" "$data/web_requests.json"
    else
      echo 'Receipt preparation needs reconciliation; the new database and receipts were retained.' >&2
    fi
  fi
  systemctl disable "$new"
  systemctl enable --now "$old"
  wait_health old_health
}

preflight() {
  systemctl is-active --quiet "$old"
  if systemctl is-active --quiet "$new"; then return 1; fi
  old_health
  queue "$data/web_requests.json" "$data/giftia.db"
  systemd-analyze verify "$base/source/deploy/$new"
  echo 'Same-origin preflight passed'
}

case "${1:?Expected preflight, apply or rollback}" in
  preflight)
    preflight
    ;;
  apply)
    preflight
    mkdir -p -- "$data/releases"
    checkpoint=$(mktemp -d "$data/releases/harness-r2-XXXXXXXX")
    echo "Rollback checkpoint $checkpoint"
    halted=false
    launched=false
    on_failure() {
      trap - ERR
      if $launched; then
        revert "$checkpoint" || echo 'Automatic rollback requires operator attention' >&2
      elif $halted; then
        systemctl start "$old" && wait_health old_health || echo 'Old service restart requires operator attention' >&2
      fi
      exit 1
    }
    trap on_failure ERR
    halted=true
    systemctl stop "$old"
    backup_giftia "$checkpoint/giftia-before.db"
    install -m 0600 "$data/web_requests.json" "$checkpoint/web_requests.before.json"
    queue "$checkpoint/web_requests.before.json" "$checkpoint/giftia-before.db"
    install -m 0600 -o giftia -g giftia "$checkpoint/web_requests.before.json" "$state/legacy-web_requests.json"
    for file in push_subs.json push_last.json; do
      if [[ -f "$data/$file" ]]; then install -m 0600 "$data/$file" "$checkpoint/$file"; fi
    done
    date +%s > "$checkpoint/cutover-time"
    install -m 0644 "$base/source/deploy/$new" "/etc/systemd/system/$new"
    systemctl daemon-reload
    launched=true
    systemctl start "$new"
    wait_health local_new
    wait_health public_new
    systemctl disable "$old"
    systemctl enable "$new"
    trap - ERR
    echo 'Harness serves the existing HTTPS origin'
    ;;
  rollback)
    checkpoint=$(realpath -e "${2:?Expected the release checkpoint directory}")
    [[ "$checkpoint" == "$data/releases/harness-r2-"* && -f "$checkpoint/cutover-time" ]]
    revert "$checkpoint"
    ;;
  *) exit 2 ;;
esac
