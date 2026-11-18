"""Back up a stopped test candidate and rehearse additive migration on a private copy."""

import argparse
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

from giftia.store import Store
from role_service import ensure_turn_ledger
from web_state import REVOCATIONS_FILE, WebState

COMPONENTS = {
    "giftia": Path("fixtures/giftia-test.db"),
    "attachments": Path("web.db"),
    "sessions": Path("dsh-home/sessions"),
}
LEGACY_TABLES = ("messages", "memories", "diary", "meta", "state")
DOCUMENT_MARKER = "PDF 资料（JSON 数据）"
DOCUMENT_MARKERS = (DOCUMENT_MARKER.encode(), json.dumps(DOCUMENT_MARKER)[1:-1].encode())


def read_db(path):
    return closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True))


def integrity(db):
    if [tuple(row) for row in db.execute("PRAGMA quick_check")] != [("ok",)]:
        raise ValueError("SQLite quick_check failed")


def sqlite_backup(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(mode=0o600, exist_ok=False)
    with read_db(source) as old, closing(sqlite3.connect(target)) as new:
        old.backup(new)
        integrity(new)


def files(path):
    return sorted(child for child in path.rglob("*") if child.is_file()) if path.is_dir() else [path]


def fingerprints(path):
    return {str(file.relative_to(path) if path.is_dir() else Path(file.name)): sha256(file.read_bytes()).hexdigest()
            for file in files(path)}


def document_log(path):
    with path.open("rb") as lines:
        return any(marker in line for line in lines for marker in DOCUMENT_MARKERS)


def copy_sessions(source, target):
    if not source.exists():
        target.mkdir(parents=True)
        return fingerprints(target)
    excluded = {log.parent for log in source.rglob("session.v4.jsonl") if document_log(log)}

    def ignore(directory, entries):
        return [entry for entry in entries if Path(directory) / entry in excluded]

    expected = {name: digest for name, digest in fingerprints(source).items()
                if not any(source / name == path or path in (source / name).parents for path in excluded)}
    shutil.copytree(source, target, ignore=ignore)
    if fingerprints(target) != expected:
        raise ValueError("Session archive differs after excluding PDF conversations")
    return expected


def snapshot(root, output):
    output.mkdir(mode=0o700)
    manifest = {}
    for kind, relative in COMPONENTS.items():
        source, target = root / relative, output / relative
        if kind == "sessions":
            manifest[kind] = copy_sessions(source, target)
        else:
            sqlite_backup(source, target)
            manifest[kind] = fingerprints(target)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {kind: len(items) for kind, items in manifest.items()}


def restore(backup, output, kind, *, revocations=None):
    relative = COMPONENTS[kind]
    source, target = backup / relative, output / relative
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    if fingerprints(source) != manifest[kind]:
        raise ValueError(f"{kind} backup differs from its manifest")
    if kind == "attachments":
        if (revocations is None or revocations.is_relative_to(backup)
                or revocations.is_relative_to(output) or not revocations.is_file()):
            raise ValueError("Attachment restore requires the current revocation database outside the snapshot and restore roots")
        with read_db(revocations) as db:
            integrity(db)
            db.execute("SELECT conversation, attachment_id FROM attachments LIMIT 1").fetchall()
    target.parent.mkdir(parents=True, exist_ok=True)
    if kind == "sessions":
        copied = copy_sessions(source, target)
        return {kind: len(copied)}
    else:
        if kind == "attachments":
            if target.exists():
                raise FileExistsError(target)
            sqlite_backup(revocations, output / REVOCATIONS_FILE)
        target.touch(mode=0o600, exist_ok=False)
        shutil.copyfile(source, target)
        with read_db(target) as db:
            integrity(db)
    if fingerprints(target) != manifest[kind]:
        raise ValueError(f"{kind} restore differs from its manifest")
    if kind == "attachments":
        WebState(target)
        with read_db(target) as db:
            integrity(db)
    return {kind: len(manifest[kind])}


def legacy_signature(db, columns):
    result = {}
    for table, fields in columns.items():
        identifiers = ', '.join('"' + field.replace('"', '""') + '"' for field in fields)
        order = ', '.join(str(index) for index in range(1, len(fields) + 1))
        rows = [tuple(row) for row in db.execute(f"SELECT {identifiers} FROM {table} ORDER BY {order}")]
        result[table] = (len(rows), sha256(repr(rows).encode()).hexdigest())
    return result


def legacy_reader(source, database, request_id=""):
    # Use the actual old code on an isolated destination. Emit only facts about visibility.
    probe = """
import json
from pathlib import Path
import sys
import giftia.store
from giftia.store import Store

assert Path(giftia.store.__file__).resolve().is_relative_to(Path.cwd())
store = Store(Path(sys.argv[1]))
try:
    request_id = sys.argv[2]
    answer = {"quickCheck": store.conn.execute("PRAGMA quick_check").fetchone()[0]}
    if request_id:
        row = store.conn.execute("SELECT instance, input_id, reply_id FROM harness_turns WHERE request_id=?",
                                 (request_id,)).fetchone()
        assert row is not None
        instance, in_id, out_id = row
        private = store.context_messages(instance, f"i{instance}-m-", "private", "", limit=200)
        guest = store.context_messages(instance, f"i{instance}-m-", "person", "synthetic-guest", limit=200)
        assert in_id in {m["id"] for m in private} and out_id in {m["id"] for m in private}
        assert not {in_id, out_id} & {m["id"] for m in guest}
        assert [m["role"] for m in private if m["id"] in {in_id, out_id}] == ["user", "assistant"]
        answer["newOrdinaryMessagesVisible"] = 2
    print(json.dumps(answer))
finally:
    store.close()
"""
    output = subprocess.check_output([sys.executable, "-c", probe, str(database), request_id], cwd=source, text=True)
    result = json.loads(output)
    if result["quickCheck"] != "ok":
        raise ValueError("Legacy reader integrity check failed")
    return result


def migrate(backup, output, legacy_source):
    output.mkdir(mode=0o700)
    target = output / "giftia.db"
    sqlite_backup(backup, target)
    old_check = legacy_reader(legacy_source, target)
    with read_db(backup) as original:
        integrity(original)
        columns = {table: [row[1] for row in original.execute(f"PRAGMA table_info({table})")]
                   for table in LEGACY_TABLES}
        before = legacy_signature(original, columns)
    store = Store(target)
    try:
        ensure_turn_ledger(store.conn)
        integrity(store.conn)
        if legacy_signature(store.conn, columns) != before:
            raise ValueError("Legacy data changed during additive migration")
        if store.conn.execute("SELECT count(*) FROM harness_turns").fetchone()[0]:
            raise ValueError("Legacy backup unexpectedly contains Harness receipts")
    finally:
        store.close()
    rollback_check = legacy_reader(legacy_source, target)
    return {"sourceCounts": {table: facts[0] for table, facts in before.items()},
            "originalRowsUnchanged": True, "legacyRead": old_check, "rollbackRead": rollback_check}


def main():
    command = argparse.ArgumentParser(description=__doc__)
    sub = command.add_subparsers(dest="action", required=True)
    copy = sub.add_parser("snapshot", help="Create a checked snapshot after stopping the isolated writer")
    copy.add_argument("state", type=Path)
    copy.add_argument("output", type=Path)
    recover = sub.add_parser("restore")
    recover.add_argument("snapshot", type=Path)
    recover.add_argument("output", type=Path)
    recover.add_argument("component", choices=COMPONENTS)
    recover.add_argument("--revocations", type=Path, help="Current revocation database, required for attachments")
    upgrade = sub.add_parser("migrate")
    upgrade.add_argument("backup", type=Path)
    upgrade.add_argument("output", type=Path)
    upgrade.add_argument("legacy_source", type=Path)
    rollback = sub.add_parser("rollback-read")
    rollback.add_argument("database", type=Path)
    rollback.add_argument("legacy_source", type=Path)
    rollback.add_argument("request_id")
    args = command.parse_args()
    if args.action == "snapshot":
        result = snapshot(args.state.resolve(), args.output.resolve())
    elif args.action == "restore":
        result = restore(args.snapshot.resolve(), args.output.resolve(), args.component,
                         revocations=args.revocations.resolve() if args.revocations else None)
    elif args.action == "migrate":
        result = migrate(args.backup.resolve(), args.output.resolve(), args.legacy_source.resolve())
    else:
        result = legacy_reader(args.legacy_source.resolve(), args.database.resolve(), args.request_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
