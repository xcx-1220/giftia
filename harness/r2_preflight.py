"""Read-only R2 receipt and queue inventory. Outputs counts, never message bodies."""

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from time import time


def reader(path):
    return closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True))


def inspect_legacy(ledger, database, now):
    rows = json.loads(ledger.read_text(encoding="utf-8"))
    totals = Counter()
    live_states = Counter()
    with reader(database) as db:
        db.execute("BEGIN")
        for key, row in rows.items():
            if not isinstance(row, dict) or not isinstance(row.get("created"), (int, float)):
                totals["malformed"] += 1
                continue
            if now - row["created"] >= 86400:
                totals["expired"] += 1
                continue
            status = row.get("status", "unknown")
            live_states[status] += 1
            if key != f"{row.get('principal')}:{row.get('request_id')}":
                totals["malformed"] += 1
                continue
            mid = "web:" + key
            messages = db.execute("""SELECT mid, role, content, instance_no, scope, subject_id, recalled
                                   FROM messages WHERE mid IN (?, ?)""", (mid, mid + ":reply")).fetchall()
            pair = {message[0]: message for message in messages}
            original, reply = pair.get(mid), pair.get(mid + ":reply")
            intact = (len(messages) == 2 and original and reply and original[1] == "user"
                      and reply[1] == "assistant" and original[2] == row.get("text")
                      and original[3:6] == reply[3:6] and not (original[6] or reply[6]))
            if intact and status in {"completed", "running"} and (
                status == "running" or row.get("reply") == reply[2]
            ):
                totals["recoverable_db_pair"] += 1
            elif status == "queued" and not messages:
                totals["queued_without_commit"] += 1
            elif status == "failed" and not messages:
                totals["failed_without_commit"] += 1
            else:
                totals["needs_reconciliation"] += 1
    return {"unexpiredStates": dict(live_states), "classification": dict(totals),
            "active": live_states["queued"] + live_states["running"]}


def inspect_candidate(path, now):
    with reader(path) as db:
        db.execute("BEGIN")
        states = dict(db.execute("SELECT status,count(*) FROM jobs GROUP BY status"))
        orphans = db.execute("""SELECT count(*) FROM jobs j WHERE j.kind='turn'
            AND json_extract(j.input,'$.attachmentId') IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM jobs a WHERE a.conversation=j.conversation AND a.kind='attachment'
                AND a.id=json_extract(j.input,'$.attachmentId') AND a.expires>?)""", (now,)).fetchone()[0]
    return {"states": states, "active": sum(states.get(s, 0) for s in ("queued", "running", "committing")),
            "documentTurnsWithoutLiveAttachment": orphans}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-ledger", required=True, type=Path)
    parser.add_argument("--legacy-db", required=True, type=Path)
    parser.add_argument("--candidate-web", required=True, type=Path)
    args = parser.parse_args()
    now = time()
    print(json.dumps({"legacy": inspect_legacy(args.legacy_ledger, args.legacy_db, now),
                      "candidate": inspect_candidate(args.candidate_web, now)}, indent=2))


if __name__ == "__main__":
    main()
