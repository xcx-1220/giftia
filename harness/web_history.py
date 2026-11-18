"""Read-only, view-bound pagination for ordinary chat history."""

from contextlib import closing
import json
import sqlite3

PAGE_SIZE = 40


class HistoryViewChanged(Exception):
    pass


def datum(db, table, key, default=""):
    query = {"meta": "SELECT value FROM meta WHERE key=?", "state": "SELECT value FROM state WHERE key=?"}[table]
    row = db.execute(query, (key,)).fetchone()
    return row[0] if row else default


def history_identity(db, principal):
    instance = int(datum(db, "meta", "instance_no", 1))
    if instance < 1:
        raise ValueError("Invalid history instance")
    owner = principal == datum(db, "state", "handler_id") and bool(principal)
    bound = datum(db, "meta", "handler_bound_at") if owner else ""
    bound_id = int(datum(db, "meta", "handler_bound_msg_id", 0) or 0) if owner else 0
    return instance, owner, bound, bound_id


def history_page(database, principal, expected_binding, before=None):
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        instance, owner, bound, bound_id = history_identity(db, principal)
        binding = json.dumps([instance, "ordinary-chat", principal, [bound, bound_id] if owner else None])
        if binding != expected_binding:
            raise HistoryViewChanged
        scope, subject = ("private", "") if owner else ("person", principal)
        prefix = f"i{instance}-m-" if owner else f"i{instance}-p{principal[:12]}-"
        cut_key = "harness_history_cut:" + json.dumps([instance, scope, subject])
        floor = max(bound_id, int(datum(db, "state", cut_key, 0) or 0))
        timestamp_floor = bound if not bound_id else ""
        limit = before if before is not None else 9223372036854775807
        rows = db.execute("""SELECT id, role, content AS text, ts FROM messages
            WHERE instance_no=? AND scope=? AND subject_id=? AND substr(session_id,1,?)=? AND recalled=0
            AND role IN ('user','assistant') AND id>? AND ts>? AND id<?
            ORDER BY id DESC LIMIT ?""", (instance, scope, subject, len(prefix), prefix, floor,
                                        timestamp_floor, limit, PAGE_SIZE + 1)).fetchall()
    return {"messages": [dict(row) for row in rows[:PAGE_SIZE]],
            "before": rows[PAGE_SIZE - 1]["id"] if len(rows) > PAGE_SIZE else None}
