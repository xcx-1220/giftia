"""Read the stopped old gateway's receipts under an explicit account mapping."""

from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
from time import time

from web_history import datum, history_identity


class LegacyWeb:
    def __init__(self, config, owner, accounts, database):
        self.database = database
        self.owner = owner
        self.epoch = config["epoch"]
        self.principals = config["principals"]
        if (len(self.epoch) != 4 or self.epoch[1] != owner or not self.principals.keys() <= accounts.keys()
                or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in self.principals.values())
                or len({value[:20] for value in self.principals.values()}) != len(self.principals)):
            raise ValueError("Invalid old web identity mapping")
        ledger = Path(config["ledger"])
        if not ledger.is_absolute() or (os.name != "nt" and ledger.stat().st_mode & 0o077):
            raise ValueError("Use an absolute path to a protected, stopped legacy ledger snapshot")
        rows = json.loads(ledger.read_text(encoding="utf-8"))
        prefixes = {value + ":" for value in self.principals.values()}
        self.rows = {key: row for key, row in rows.items() if key.partition(":")[0] + ":" in prefixes}
        with self.reader() as db:
            if not self.current_epoch(db):
                raise ValueError("Old web mapping differs from the current Giftia view")

    def reader(self):
        return closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True))

    def current_epoch(self, db):
        instance, owner, bound, watermark = history_identity(db, self.owner)
        return owner and self.epoch == [instance, self.owner, bound, watermark]

    def view(self, principal):
        old = self.principals.get(principal)
        if old:
            with self.reader() as db:
                if self.current_epoch(db):
                    return old[:20]
        return None

    def has_request(self, principal, request_id):
        return f"{self.principals.get(principal)}:{request_id}" in self.rows

    def result(self, principal, request_id):
        old = self.principals.get(principal)
        key = f"{old}:{request_id}"
        row = self.rows.get(key)
        if (not row or row.get("principal") != old or row.get("request_id") != request_id or
                not isinstance(row.get("created"), (int, float)) or time() - row["created"] >= 86400):
            return None
        owner = principal == self.owner
        if (row.get("role"), row.get("user_id")) != (("owner", "") if owner else ("friend", principal)):
            return None
        if row.get("status") not in {"completed", "running"}:
            return None
        mid = "web:" + key
        with self.reader() as db:
            db.execute("BEGIN")
            if not self.current_epoch(db):
                return None
            instance, _, bound, watermark = self.epoch
            scope, subject = ("private", "") if owner else ("person", principal)
            prefix = f"i{instance}-m-" if owner else f"i{instance}-p{principal[:12]}-"
            cut = "harness_history_cut:" + json.dumps([instance, scope, subject])
            floor = max(watermark if owner else 0, int(datum(db, "state", cut, 0) or 0))
            messages = db.execute("""SELECT mid, role, content
                FROM messages WHERE mid IN (?,?) AND instance_no=? AND scope=? AND subject_id=?
                AND recalled=0 AND id>? AND ts>? AND substr(session_id,1,?)=?""",
                (mid, mid + ":reply", instance, scope, subject, floor, bound if owner and not watermark else "",
                 len(prefix), prefix)).fetchall()
        if len(messages) != 2:
            return None
        pair = {message[0]: message[1:] for message in messages}
        original, reply = pair.get(mid), pair.get(mid + ":reply")
        if (original != ("user", row.get("text")) or not reply or reply[0] != "assistant" or
                (row["status"] == "completed" and reply[1] != row.get("reply"))):
            return None
        return {"text": original[1], "reply": reply[1]}
