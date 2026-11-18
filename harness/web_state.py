"""Durable jobs and view-scoped transcripts for the isolated web gateway."""

from contextlib import closing, contextmanager
import json
import sqlite3
from time import time

RETENTION = 24 * 60 * 60
ACTIVE = ("queued", "running", "committing")
REVOCATIONS_FILE = "web-revocations.db"
REVOCATION_SCHEMA_VERSION = 1
REVOKED = """EXISTS (SELECT 1 FROM rev.attachments r WHERE r.conversation=j.conversation
    AND r.attachment_id=CASE WHEN j.kind='attachment' THEN j.id ELSE json_extract(j.input,'$.attachmentId') END)"""
MISSING_SOURCE = """(j.kind='turn' AND json_extract(j.input,'$.attachmentId') IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM jobs a WHERE a.conversation=j.conversation AND a.kind='attachment'
    AND a.id=json_extract(j.input,'$.attachmentId') AND a.expires>?))"""
VISIBLE = f"(j.expires IS NULL OR j.expires>?) AND NOT {REVOKED} AND NOT {MISSING_SOURCE}"


class WebState:
    def __init__(self, path):
        self.path = path
        self.revocations = path.with_name(REVOCATIONS_FILE)
        if not self.revocations.exists() and path.is_file():
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                if db.execute("PRAGMA user_version").fetchone()[0] >= REVOCATION_SCHEMA_VERSION:
                    raise ValueError("Current attachment revocation database is missing")
        with closing(sqlite3.connect(self.revocations)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS attachments (
                conversation TEXT NOT NULL, attachment_id TEXT NOT NULL,
                PRIMARY KEY (conversation, attachment_id)
            )""")
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT NOT NULL, conversation TEXT NOT NULL, kind TEXT NOT NULL,
                    status TEXT NOT NULL, input TEXT NOT NULL, payload BLOB, receipt TEXT,
                    events TEXT NOT NULL DEFAULT '[]', error TEXT, created REAL NOT NULL, expires REAL,
                    PRIMARY KEY (conversation, kind, id)
                );
                CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(status, created);
                CREATE INDEX IF NOT EXISTS jobs_expiry ON jobs(expires);
                UPDATE jobs SET status='queued', events='[]' WHERE status='running';
            """)
            self.purge_unavailable(db, time())
            db.execute(f"PRAGMA user_version={REVOCATION_SCHEMA_VERSION}")

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("ATTACH DATABASE ? AS rev", (str(self.revocations),))
            yield db

    @staticmethod
    def purge_unavailable(db, now):
        db.execute("BEGIN IMMEDIATE")
        db.execute(f"DELETE FROM jobs AS j WHERE j.expires<=? OR {REVOKED} OR {MISSING_SOURCE}", (now, now))
        db.commit()

    def put(self, view, kind, job_id, value, payload=None):
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
        now = time()
        expires = now + RETENTION if kind == "attachment" else None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            key = (view, kind, job_id)
            if kind == "turn" and (attachment_id := value["attachmentId"]):
                if not db.execute(f"""SELECT 1 FROM jobs j WHERE conversation=? AND kind='attachment'
                                    AND id=? AND status='ready' AND {VISIBLE}""",
                                  (view, attachment_id, now, now)).fetchone():
                    raise LookupError("附件不存在或已不可用。")
            previous = db.execute("SELECT input FROM jobs WHERE conversation=? AND kind=? AND id=?", key).fetchone()
            if previous:
                return previous["input"] == raw
            if kind == "attachment":
                count = db.execute("""SELECT count(*) FROM jobs
                    WHERE conversation=? AND kind='attachment' AND expires>? AND status<>'cancelled'""",
                                   (view, now)).fetchone()[0]
                if count >= 12:
                    raise ValueError("当前会话最多保留 12 份附件。")
            db.execute("""INSERT INTO jobs (conversation, kind, id, status, input, payload, created, expires)
                          VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)""", (*key, raw, payload, now, expires))
            db.commit()
            return True

    def get(self, view, kind, job_id):
        now = time()
        with self.connect() as db:
            row = db.execute(f"SELECT * FROM jobs j WHERE conversation=? AND kind=? AND id=? AND {VISIBLE}",
                             (view, kind, job_id, now, now)).fetchone()
            return dict(row) if row else None

    def next(self):
        with self.connect() as db:
            now = time()
            if db.execute("SELECT 1 FROM jobs WHERE expires<=? LIMIT 1", (now,)).fetchone():
                self.purge_unavailable(db, now)
            row = db.execute(f"""UPDATE jobs SET status=CASE status WHEN 'queued' THEN 'running' ELSE status END
                WHERE rowid=(SELECT j.rowid FROM jobs j WHERE status IN ('queued','committing') AND {VISIBLE}
                             ORDER BY status='committing' DESC, created, rowid LIMIT 1) RETURNING *""", (now, now)).fetchone()
            return dict(row) if row else None

    def delete_attachment(self, view, attachment_id):
        if self.get(view, "attachment", attachment_id) is None:
            return False
        # Commit the revocation first. Every read honors it even if job cleanup is interrupted.
        with closing(sqlite3.connect(self.revocations)) as db, db:
            db.execute("INSERT OR IGNORE INTO attachments VALUES (?, ?)", (view, attachment_id))
        with self.connect() as db:
            self.purge_unavailable(db, time())
        return True

    def status(self, job):
        now = time()
        with self.connect() as db:
            row = db.execute(f"SELECT status FROM jobs j WHERE conversation=? AND kind=? AND id=? AND {VISIBLE}",
                             (job["conversation"], job["kind"], job["id"], now, now)).fetchone()
            return row["status"] if row else None

    def transition(self, job, previous, status, *, error=None, receipt=None, drop_payload=False):
        with self.connect() as db:
            marks = ",".join("?" for _ in previous)
            return db.execute(f"""UPDATE jobs SET status=?, error=?, receipt=?,
                    payload=CASE WHEN ? THEN NULL ELSE payload END,
                    events=CASE WHEN ?='queued' THEN '[]' ELSE events END
                    WHERE conversation=? AND kind=? AND id=? AND status IN ({marks})""",
                    (status, error, json.dumps(receipt, ensure_ascii=False) if receipt is not None else None,
                     drop_payload, status, job["conversation"], job["kind"], job["id"], *previous)).rowcount == 1

    def events(self, job, events):
        with self.connect() as db:
            db.execute("""UPDATE jobs SET events=? WHERE conversation=? AND kind=? AND id=? AND status='running'""",
                       (json.dumps(events, ensure_ascii=False), job["conversation"], job["kind"], job["id"]))

    def snapshot(self, view):
        now = time()
        with self.connect() as db:
            return [dict(row) for row in db.execute(f"""SELECT id, conversation, kind, status, input, receipt, events,
                error, created FROM (
                    SELECT j.id, j.conversation, j.kind, j.status, j.input, j.receipt, j.events, j.error, j.created, j.rowid
                    FROM jobs j WHERE j.conversation=? AND {VISIBLE}
                    ORDER BY created DESC, rowid DESC LIMIT 120
                ) ORDER BY created, rowid""", (view, now, now))]

    def document_history(self, job, attachment_id):
        history = []
        now = time()
        with self.connect() as db:
            rows = db.execute(f"""SELECT input, receipt FROM jobs j WHERE conversation=? AND kind='turn'
                AND status='ready' AND json_extract(input, '$.attachmentId')=? AND created<? AND {VISIBLE}
                ORDER BY created DESC, rowid DESC LIMIT 6""",
                (job["conversation"], attachment_id, job["created"], now, now)).fetchall()
        for row in reversed(rows):
            history.extend(({"role": "user", "content": json.loads(row["input"])["text"]},
                            {"role": "assistant", "content": json.loads(row["receipt"])["reply"]}))
        return history
