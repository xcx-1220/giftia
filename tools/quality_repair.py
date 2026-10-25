#!/usr/bin/env python3
"""Preview/apply/revert narrow memory injection repairs; never delete original memories."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from giftia.conversation import is_performance

REPAIR = 'quality-performance-v1'


def candidates(conn):
    return [dict(r) for r in conn.execute("SELECT id,summary,instance_no,scope FROM memories "
        "WHERE sealed=0 AND forgotten=0 AND merged_into=0 ORDER BY id") if is_performance(r['summary'])]


def repair(db: Path, *, apply=False, rollback=False):
    conn = sqlite3.connect(db.as_uri() + '?mode=' + ('rw' if apply or rollback else 'ro'), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if not apply and not rollback:
            conn.execute('PRAGMA query_only=ON')
            return {'repair_id': REPAIR, 'mode': 'preview', 'candidates': candidates(conn)}
        # Additive schema must already have been migrated by the new release.
        conn.execute('BEGIN IMMEDIATE')
        changed = []
        if rollback:
            for row in conn.execute('SELECT memory_id,previous FROM quality_repairs WHERE repair_id=?', (REPAIR,)).fetchall():
                current = conn.execute('SELECT revision FROM memory_annotations WHERE memory_id=?', (row['memory_id'],)).fetchone()
                if current and current['revision'] != REPAIR:
                    continue  # A later repair must not be overwritten.
                old = json.loads(row['previous'])
                conn.execute('DELETE FROM memory_annotations WHERE memory_id=?', (row['memory_id'],))
                if old:
                    conn.execute('INSERT INTO memory_annotations VALUES(?,?,?,?,?,?)', tuple(old[k] for k in
                                 ['memory_id','kind','behavior_key','policy','reason','revision']))
                conn.execute('DELETE FROM quality_repairs WHERE repair_id=? AND memory_id=?', (REPAIR,row['memory_id']))
                changed.append(row['memory_id'])
        else:
            for row in candidates(conn):
                old = conn.execute('SELECT * FROM memory_annotations WHERE memory_id=?', (row['id'],)).fetchone()
                if old and old['revision'] == REPAIR:
                    continue
                conn.execute('INSERT OR IGNORE INTO quality_repairs VALUES(?,?,?)',
                             (REPAIR, row['id'], json.dumps(dict(old) if old else None)))
                conn.execute('INSERT OR REPLACE INTO memory_annotations VALUES(?,?,?,?,?,?)',
                             (row['id'], 'assistant_statement','farewell','background',
                              '反复的助手告别表演保留为历史，不作为默认行为注入', REPAIR))
                changed.append(row['id'])
        conn.commit()
        return {'repair_id': REPAIR, 'mode': 'rollback' if rollback else 'apply', 'changed': changed}
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--apply', action='store_true')
    group.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    print(json.dumps(repair(args.db.resolve(), apply=args.apply, rollback=args.rollback), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
