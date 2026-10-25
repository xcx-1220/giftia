"""Recoverable maintenance: worker does network/pure work, caller alone commits SQLite."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import queue
import threading
import time
from datetime import datetime

from .llm import DeepSeekLLM, MockLLM
from .memory import build_transcript, known_block, consolidate, reconcile, blur_text
from .dream import should_dream


class SnapshotStore:
    def __init__(self, memories):
        self.memories = memories

    def active_memories(self, *args, **kwargs):
        return self.memories


class CachedJudge:
    def __init__(self, llm=None, values=None):
        self.llm = llm
        self.values = values if values is not None else {}

    def find_duplicate_groups(self, text):
        return self.call("find_duplicate_groups", text)

    def confirm_duplicate(self, text):
        return self.call("confirm_duplicate", text)

    def call(self, method, text):
        key = method + ":" + text
        if key not in self.values:
            if self.llm is None:
                raise ValueError("stale reconciliation snapshot")
            self.values[key] = getattr(self.llm, method)(text)
        return self.values[key]


class Maintenance:
    def __init__(self, brain, llm=None):
        self.brain = brain
        self.settings = brain.settings
        self.store = brain.store
        cfg = dataclasses.replace(brain.settings.model, request_timeout=min(20, brain.settings.model.request_timeout))
        self.llm = llm or (MockLLM(cfg) if cfg.is_mock else DeepSeekLLM(cfg, retries=1))
        self.results = queue.Queue()
        self.running = False
        self.closed = False
        self.scanned_at = 0.0
        self.store.conn.executescript("""
          CREATE TABLE IF NOT EXISTS maintenance_tasks (
            id INTEGER PRIMARY KEY, job_key TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
            payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', elapsed_ms INTEGER NOT NULL DEFAULT 0);
        """)
        self.store.conn.execute("UPDATE maintenance_tasks SET status='queued' WHERE status='running'")
        self.store.conn.execute("DELETE FROM maintenance_tasks WHERE status IN ('done','stale','failed') AND id < (SELECT COALESCE(max(id),0)-200 FROM maintenance_tasks)")
        self.store.conn.commit()

    def enqueue(self, kind, payload, version):
        for row in self.store._rows("SELECT kind,payload FROM maintenance_tasks WHERE status IN ('queued','running')"):
            old = json.loads(row['payload'])
            if row['kind'] == kind and old['scope'] == payload['scope'] and old['subject'] == payload['subject'] and old['instance'] == self.brain.instance_no:
                return
        count = self.store.conn.execute("SELECT count(*) FROM maintenance_tasks WHERE status IN ('queued','running')").fetchone()[0]
        if count >= 20:
            return
        payload.update(instance=self.brain.instance_no, handler=self.brain.handler_id,
                       floor=self.brain._int_meta("handler_bound_msg_id"), bound_at=self.brain.handler_bound_at,
                       visibility_revision=self.brain._int_meta('memory_revision'))
        key = json.dumps([kind, payload['instance'], payload['scope'], payload['subject'],
                          payload['handler'], payload['floor'], payload['visibility_revision'], version], ensure_ascii=False)
        self.store.conn.execute("INSERT OR IGNORE INTO maintenance_tasks(job_key,kind,payload) VALUES(?,?,?)",
                                (key, kind, json.dumps(payload, ensure_ascii=False)))
        self.store.conn.commit()

    def schedule(self):
        brain = self.brain
        rows = self.store._rows("SELECT session_id,scope,subject_id,count(*) n,max(ts) last FROM messages "
                                "WHERE instance_no=? AND recalled=0 AND consolidated=0 "
                                "GROUP BY session_id,scope,subject_id ORDER BY max(id) ASC LIMIT 20", (brain.instance_no,))
        for row in rows:
            if row['scope'] == 'private' and not brain.handler_id:
                continue
            try:
                stale = (brain.clock.now() - datetime.fromisoformat(row['last'])).total_seconds() >= 1800
            except ValueError:
                stale = False
            if row['n'] < brain.settings.memory.consolidate_every and not stale:
                continue
            pending = self.store._rows("SELECT * FROM messages WHERE session_id=? AND scope=? AND subject_id=? "
                                       "AND instance_no=? AND recalled=0 AND consolidated=0 ORDER BY id LIMIT 80",
                                       (row['session_id'], row['scope'], row['subject_id'], brain.instance_no))
            if row['scope'] == 'private':
                floor = brain._int_meta('handler_bound_msg_id')
                pending = [m for m in pending if int(m['id']) > floor and
                           (not brain.handler_bound_at or m['ts'] > brain.handler_bound_at or floor)]
            if not pending:
                continue
            view = [(row['scope'], row['subject_id'])]
            existing = self.store.active_memories(brain.instance_no, view=view)
            state = brain.conversation_state(row['scope'], row['subject_id']).render(brain.clock.now(), stage=brain.status().stage, for_extraction=True) if brain.settings.memory.conversation_state_enabled else ''
            payload = dict(scope=row['scope'], subject=row['subject_id'], session=row['session_id'],
                           pending=pending, now=brain.clock.iso(),
                           known=known_block(existing, brain.settings.memory.known_hint_limit) + "\n" + state)
            self.enqueue('consolidate', payload, [pending[0]['id'], pending[-1]['id']])

        # Private dreams and reconciliation never use the last HTTP visitor's mutable view.
        private = self.store.active_memories(brain.instance_no, view=[('private', '')]) if brain.handler_id else []
        private = [m for m in private if brain._memory_visible(m)]
        signature = hashlib.sha256(json.dumps([(m['id'],m['summary']) for m in private],ensure_ascii=False).encode()).hexdigest()[:20]
        if len(private) >= brain.settings.dream.min_memories and should_dream(brain.settings, self.store, brain.clock, brain.instance_no, view=[('private', '')]):
            picked = private[-max(1, brain.settings.dream.fragments):]
            last = self.store._one('SELECT max(id) id FROM dreams WHERE instance_no=?', (brain.instance_no,))
            self.enqueue('dream', dict(scope='private', subject='', memories=picked,
                                      previous_dream=last['id'] if last else None), [last['id'] if last else 0, signature])
        cfg = brain.settings.memory
        last_reconcile = float(self.store.state_get(f'quality-reconciled:{brain.instance_no}', '0') or 0)
        if len(private) >= cfg.reconcile_min_memories and brain.clock.now().timestamp() - last_reconcile >= cfg.reconcile_hours * 3600:
            bucket = int(brain.clock.now().timestamp() / max(3600, cfg.reconcile_hours * 3600))
            self.enqueue('reconcile', dict(scope='private', subject='', memories=private[-40:]), [bucket,signature])

    def _work(self, task_id, kind, payload):
        start = time.monotonic()
        try:
            if kind == 'consolidate':
                value = self.llm.extract_memories(build_transcript(payload['pending'], self.settings.persona.name),
                    name=self.settings.persona.name, today=payload['now'][:10],
                    scene={'private': 'c2c', 'group': 'group', 'person': 'person'}[payload['scope']], known=payload['known'])
            elif kind == 'dream':
                fragments = [blur_text(m['summary'], self.settings.dream.blur, int(m['id']) * 31 + 7)[0]
                             for m in payload['memories']]
                value = self.llm.write_dream(fragments, name=self.settings.persona.name)
            else:
                judge = CachedJudge(self.llm)
                reconcile(SnapshotStore(payload['memories']), judge, self.settings,
                          instance_no=payload['instance'], scope=payload['scope'], subject_id=payload['subject'], dry_run=True)
                value = judge.values
            self.results.put((task_id, kind, payload, value, '', round((time.monotonic()-start)*1000)))
        except Exception as exc:
            self.results.put((task_id, kind, payload, None, type(exc).__name__ + ': ' + str(exc)[:200],
                              round((time.monotonic()-start)*1000)))

    def valid(self, payload):
        brain = self.brain
        if payload['instance'] != brain.instance_no:
            return False
        if payload.get('visibility_revision', 0) != brain._int_meta('memory_revision'):
            return False
        if payload['scope'] == 'private' and (payload['handler'] != brain.handler_id or
            payload['floor'] != brain._int_meta('handler_bound_msg_id') or payload['bound_at'] != brain.handler_bound_at):
            return False
        if 'pending' in payload:
            rows = self.store.messages_by_ids([m['id'] for m in payload['pending']])
            return (len(rows) == len(payload['pending']) and all(
                not m['recalled'] and not m['consolidated'] and m['content'] == old['content'] and
                m['scope'] == payload['scope'] and m['subject_id'] == payload['subject']
                for m, old in zip(rows, payload['pending'])))
        current = {m['id']: m for m in self.store.active_memories(brain.instance_no, view=[(payload['scope'], payload['subject'])])}
        if 'previous_dream' not in payload and list(current)[-40:] != [m['id'] for m in payload['memories']]:
            return False
        return all(m['id'] in current and m['summary'] == current[m['id']]['summary'] for m in payload['memories'])

    def poll(self, *, start=True):
        if self.closed:
            return
        while True:
            try:
                task_id, kind, payload, value, error, elapsed = self.results.get_nowait()
            except queue.Empty:
                break
            self.running = False
            status = 'done'
            try:
                if error:
                    raise RuntimeError(error)
                if not self.valid(payload):
                    status = 'stale'
                elif kind == 'consolidate':
                    brain = self.brain
                    consolidate(self.store, None, brain.settings, session_id=payload['session'],
                        now_iso=brain.clock.iso(), force=True, scope=payload['scope'], subject_id=payload['subject'],
                        prepared=(payload['pending'], value), conversation=brain.conversation_state(payload['scope'], payload['subject']) if brain.settings.memory.conversation_state_enabled else None,
                        on_draft=brain._on_draft if payload['scope'] == 'private' else None)
                elif kind == 'dream':
                    row = self.store._one('SELECT max(id) id FROM dreams WHERE instance_no=?', (payload['instance'],))
                    if (row['id'] if row else None) == payload['previous_dream'] and str(value).strip():
                        self.store.add_dream(payload['instance'], self.brain.clock.iso(), str(value)[:1200],
                                             [m['id'] for m in payload['memories']])
                else:
                    self.store.snapshot_memories(tag='quality-reconcile')
                    reconcile(self.store, CachedJudge(values=value), self.settings,
                              instance_no=payload['instance'], scope=payload['scope'], subject_id=payload['subject'])
                    self.store.state_set(f"quality-reconciled:{payload['instance']}", str(self.brain.clock.now().timestamp()))
            except Exception as exc:
                error = type(exc).__name__ + ': ' + str(exc)[:200]
                attempts = self.store._one('SELECT attempts FROM maintenance_tasks WHERE id=?', (task_id,))['attempts']
                status = 'queued' if attempts < 3 else 'failed'
            self.store.conn.execute('UPDATE maintenance_tasks SET status=?,error=?,elapsed_ms=?,retry_at=? WHERE id=?',
                                    (status, error, elapsed, time.time()+30, task_id))
            if status in ('done', 'stale'):
                self.store.conn.execute("UPDATE maintenance_tasks SET payload='{}' WHERE id=?", (task_id,))
            elif status == 'failed':
                print(f'[维护] {kind} 任务 #{task_id} 重试后仍失败，原始消息保留，详情见 maintenance_tasks')
            self.store.conn.commit()
        if not start:
            return
        if time.monotonic() - self.scanned_at > 5:
            self.schedule()
            self.scanned_at = time.monotonic()
        if self.running:
            return
        row = self.store._one("SELECT * FROM maintenance_tasks WHERE status='queued' AND retry_at<=? "
                              "ORDER BY CASE kind WHEN 'consolidate' THEN 0 ELSE 1 END,id LIMIT 1", (time.time(),))
        if row:
            payload = json.loads(row['payload'])
            if not self.valid(payload):
                self.store.conn.execute("UPDATE maintenance_tasks SET status='stale' WHERE id=?", (row['id'],))
                self.store.conn.commit()
                return
            self.store.conn.execute("UPDATE maintenance_tasks SET status='running',attempts=attempts+1 WHERE id=?", (row['id'],))
            self.store.conn.commit()
            self.running = True
            threading.Thread(target=self._work, args=(row['id'], row['kind'], payload), daemon=True).start()

    def close(self):
        self.poll(start=False)
        self.closed = True
        # A daemon worker may finish later, but it has no database handle. Its durable job is recovered on restart.
