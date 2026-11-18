"""Seed a fresh Giftia SQLite database using the repository's Store API."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from giftia.store import Store


def seed(database):
    if database.exists():
        return
    database.parent.mkdir(parents=True, exist_ok=True)
    store = Store(database)
    try:
        ts = "2026-01-01T12:00:00+08:00"
        store.meta_set("harness_fixture", "stage1-v1")
        for identity, scope, text in (
            ("synthetic-owner", "private", "合成搭档把纸灯笼放在窗边。"),
            ("synthetic-guest", "person", "合成访客画了一只蓝风筝。"),
        ):
            store.add_message(identity, 1, "user", text, ts, speaker_id=identity,
                              scope=scope, subject_id=identity, mid=identity)
            store.add_memory(instance_no=1, created_ts=ts, happened_ts=ts,
                             summary=text, source_session=identity, scope=scope,
                             subject_id=identity, speaker_id=identity)
    finally:
        store.close()
