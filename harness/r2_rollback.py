"""Prepare an old-gateway receipt ledger while both chat writers are stopped."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from time import time

from legacy_web import LegacyWeb
from r2_preflight import reader
from release_config import WebRelease
from web_history import datum


def message_pair(db, turn, instance, scope, subject, prefix, floor, bound):
    rows = db.execute("""SELECT id, role, content FROM messages WHERE id IN (?,?) AND instance_no=?
        AND scope=? AND subject_id=? AND id>? AND ts>? AND substr(session_id,1,?)=? AND recalled=0""",
                      (turn[0], turn[1], instance, scope, subject, floor, bound, len(prefix), prefix)).fetchall()
    by_id = {row[0]: row[1:] for row in rows}
    user, assistant = by_id.get(turn[0]), by_id.get(turn[1])
    if (user and assistant and user[0] == "user" and assistant[0] == "assistant" and
            sha256(user[1].encode()).hexdigest() == turn[2]):
        return user[1], assistant[1]
    return None


def prepare_rollback(database, web_database, release, since, now=None):
    """Return the merged ledger and counts. Both source databases are read-only."""
    now = time() if now is None else now
    if not 0 < since <= now or not release.legacy:
        raise ValueError("Provide the cutover time and verified legacy identity mapping")
    legacy = LegacyWeb(release.legacy, release.owner, release.accounts, database)
    old = json.loads(Path(release.legacy["ledger"]).read_text(encoding="utf-8"))
    rows = {key: row for key, row in old.items() if row["created"] > now - 86400}
    if any(row["status"] in {"queued", "running"} for row in rows.values()):
        raise ValueError("Reconcile active old requests before preparing rollback")
    totals = {"added": 0, "alreadyPresent": 0, "unavailable": 0}
    with reader(database) as db, reader(web_database) as web:
        db.execute("BEGIN")
        web.execute("BEGIN")
        if not legacy.current_epoch(db):
            raise ValueError("The verified rollback binding has changed")
        instance, owner, bound, watermark = legacy.epoch
        for principal, old_principal in legacy.principals.items():
            is_owner = principal == owner
            scope, subject = ("private", "") if is_owner else ("person", principal)
            prefix = f"i{instance}-m-" if is_owner else f"i{instance}-p{principal[:12]}-"
            binding = json.dumps([instance, "ordinary-chat", principal, [bound, watermark] if is_owner else None])
            view = sha256(binding.encode()).hexdigest()[:32]
            cut_key = "harness_history_cut:" + json.dumps([instance, scope, subject])
            floor = max(watermark if is_owner else 0, int(datum(db, "state", cut_key, 0) or 0))
            jobs = web.execute("""SELECT id, created, json_extract(input,'$.text') FROM jobs
                WHERE conversation=? AND kind='turn' AND status IN ('ready','committing') AND created BETWEEN ? AND ?
                AND json_extract(input,'$.attachmentId') IS NULL ORDER BY created, id""",
                               (view, max(since, now - 86400), now)).fetchall()
            for request_id, created, job_text in jobs:
                if not re.fullmatch(r"[a-zA-Z0-9_-]{16,80}", request_id):
                    totals["unavailable"] += 1
                    continue
                turn = db.execute("""SELECT input_id, reply_id, input_hash FROM harness_turns
                    WHERE principal=? AND purpose='ordinary-chat' AND binding=? AND request_id=?""",
                                  (principal, binding, request_id)).fetchone()
                pair = turn and message_pair(db, turn, instance, scope, subject, prefix, floor,
                                             bound if is_owner and not watermark else "")
                if not pair or pair[0] != job_text:
                    totals["unavailable"] += 1
                    continue
                text, reply = pair
                key = old_principal + ":" + request_id
                receipt = {"principal": old_principal, "role": "owner" if is_owner else "friend", "user_id": subject,
                           "request_id": request_id, "text": text, "reply": reply, "error": "",
                           "created": created, "status": "completed"}
                previous = rows.get(key)
                if previous:
                    if any(previous.get(field) != receipt[field] for field in (
                            "principal", "role", "user_id", "request_id", "text", "reply", "status")):
                        raise ValueError("Old and new ledgers disagree on an existing receipt")
                    totals["alreadyPresent"] += 1
                else:
                    rows[key] = receipt
                    totals["added"] += 1
    return rows, totals


def write_new_ledger(output, rows):
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as file:
        json.dump(rows, file, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--web-database", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--since", required=True, type=float, help="Cutover timestamp in Unix seconds")
    parser.add_argument("--output", type=Path, help="Write an exclusively created ledger file. Omit for counts only.")
    args = parser.parse_args()
    rows, totals = prepare_rollback(args.database.resolve(), args.web_database, WebRelease.load(args.manifest), args.since)
    if args.output:
        write_new_ledger(args.output, rows)
    print(json.dumps(totals))


if __name__ == "__main__":
    main()
