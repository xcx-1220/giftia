"""Apply the R2 history projection to a stopped backup copy and report counts only."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from web_history import datum, history_identity, history_page


def visible_ids(database, principal, identity):
    instance, owner, bound, bound_id = identity
    binding = json.dumps([instance, "ordinary-chat", principal, [bound, bound_id] if owner else None])
    before = None
    ids = []
    while True:
        page = history_page(database, principal, binding, before)
        ids.extend(row["id"] for row in page["messages"])
        before = page["before"]
        if before is None:
            break
    if len(ids) != len(set(ids)):
        raise ValueError("History pagination repeated a message")
    return set(ids)


def audit(database):
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("Database consistency check failed")
        owner = datum(db, "state", "handler_id")
        if not owner:
            raise ValueError("Backup has no bound owner")
        owner_identity = history_identity(db, owner)
        instance = owner_identity[0]
        subjects = [row[0] for row in db.execute("""SELECT DISTINCT subject_id FROM messages WHERE
                    instance_no=? AND scope='person' AND subject_id NOT IN ('','web-guest',?)""", (instance, owner))]
        total = dict(db.execute("SELECT scope, count(*) FROM messages GROUP BY scope"))
        allowed_owner = visible_ids(database, owner, owner_identity)
        seen = set(allowed_owner)
        guest_counts = []
        for subject in subjects:
            allowed = visible_ids(database, subject, history_identity(db, subject))
            if seen.intersection(allowed):
                raise ValueError("Per-principal history ranges overlap")
            seen.update(allowed)
            guest_counts.append(len(allowed))
        shared_guest_rows = db.execute("""SELECT count(*) FROM messages
            WHERE instance_no=? AND scope='person' AND subject_id IN ('','web-guest')""", (instance,)).fetchone()[0]
    return {"instance": instance, "rowsByRecordedScope": total, "boundOwnerRows": len(allowed_owner),
            "perSubjectRows": sorted(guest_counts), "sharedGuestRowsHeld": shared_guest_rows,
            "rowsOutsideProjectedViews": sum(total.values()) - len(seen), "overlap": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup_copy", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.backup_copy.resolve()), indent=2))
