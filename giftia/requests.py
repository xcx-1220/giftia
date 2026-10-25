"""Bounded, durable HTTP request ledger, independent of Giftia's SQLite connection."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


class RequestLedger:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
            self.rows = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            self.rows = {}
        self.prune()

    def prune(self):
        now = time.time()
        self.rows = {k: v for k, v in self.rows.items() if isinstance(v, dict) and now - v.get('created', 0) < 86400}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        with tmp.open('w', encoding='utf-8') as out:
            json.dump(self.rows, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, self.path)

    def get(self, key):
        with self.lock:
            self.prune()
            value = self.rows.get(key)
            return dict(value) if value else None

    def reserve(self, key, row):
        with self.lock:
            self.prune()
            existing = self.rows.get(key)
            if existing:
                if existing['text'] != row['text']:
                    raise ValueError('同一请求编号不能用于不同消息')
                return False
            if len(self.rows) >= 256:
                completed = [k for k, v in self.rows.items() if v['status'] in ('completed', 'failed')]
                if not completed:
                    raise OverflowError('请求记录已满')
                self.rows.pop(completed[0])
            self.rows[key] = dict(row, created=time.time(), status='queued')
            try:
                self.save()
            except BaseException:
                self.rows.pop(key, None)
                raise
            return True

    def update(self, key, **values):
        with self.lock:
            self.rows[key].update(values)
            self.save()
