"""SQLite 持久化层：消息、记忆条目、日志、全局状态。

一张表一件事，刻意保持简单——你以后想换成 Postgres 或加向量库，
只要替换这个文件即可，上层不依赖具体实现。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 原始对话（工作记忆的来源，也是整理记忆条目的原料）
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT    NOT NULL,
    instance_no  INTEGER NOT NULL,
    role         TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    ts           TEXT    NOT NULL,
    consolidated INTEGER NOT NULL DEFAULT 0
);

-- 长期记忆条目：由一段对话压缩而来
CREATE TABLE IF NOT EXISTS memories (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_no    INTEGER NOT NULL,
    created_ts     TEXT    NOT NULL,
    happened_ts    TEXT    NOT NULL,
    summary        TEXT    NOT NULL,
    tags           TEXT    NOT NULL DEFAULT '',
    emotion        TEXT    NOT NULL DEFAULT '',
    importance     REAL    NOT NULL DEFAULT 0.5,
    source_session TEXT    NOT NULL DEFAULT '',
    sealed         INTEGER NOT NULL DEFAULT 0,
    -- 记忆分层：core（内核，永不含糊）/ important / normal
    layer          TEXT    NOT NULL DEFAULT 'normal',
    -- 召回强化：strength 是出厂强度，recall_count 是"被真正说出口"的次数
    strength       REAL    NOT NULL DEFAULT 1.0,
    recall_count   INTEGER NOT NULL DEFAULT 0,
    last_recalled_at TEXT,
    injected_at      TEXT
);

-- 日志：kind = handler（你的搭档日志）| terminal（终端服务通知）| memoir（回忆录）
CREATE TABLE IF NOT EXISTS diary (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL DEFAULT 'handler',
    title   TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL
);

-- 跨对话保留的状态（好感度、上次整理位置等）
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_msg_session  ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_msg_open     ON messages(instance_no, consolidated);
CREATE INDEX IF NOT EXISTS idx_mem_instance ON memories(instance_no, sealed);
"""


class Store:
    # 旧版本数据库缺的列在这里补齐（ALTER TABLE ADD COLUMN 不影响已有数据）
    _MIGRATIONS: dict[str, dict[str, str]] = {
        "memories": {
            "layer": "TEXT NOT NULL DEFAULT 'normal'",
            "strength": "REAL NOT NULL DEFAULT 1.0",
            "recall_count": "INTEGER NOT NULL DEFAULT 0",
            "last_recalled_at": "TEXT",
            "injected_at": "TEXT",
        }
    }

    def __init__(self, db_path: Path | str) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """给老库补列。她的记忆比 schema 重要，升级不能丢数据。"""
        for table, columns in self._MIGRATIONS.items():
            existing = {
                row["name"] for row in self._rows(f"PRAGMA table_info({table})")
            }
            for name, ddl in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    # ── 通用 ──────────────────────────────────────────────
    def _rows(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    # ── meta ──────────────────────────────────────────────
    def meta_get(self, key: str, default: Any = None) -> Any:
        row = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def meta_set(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    def meta_get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.meta_get(key, default))
        except (TypeError, ValueError):
            return default

    def meta_get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(float(self.meta_get(key, default)))
        except (TypeError, ValueError):
            return default

    # ── messages ──────────────────────────────────────────
    def add_message(
        self, session_id: str, instance_no: int, role: str, content: str, ts: str
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO messages(session_id, instance_no, role, content, ts) "
            "VALUES(?, ?, ?, ?, ?)",
            (session_id, instance_no, role, content, ts),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_messages(self, session_id: str, limit: int = 24) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        return list(reversed(rows))

    def open_messages(self, instance_no: int, after_id: int = 0) -> list[dict[str, Any]]:
        """还没被整理成记忆条目的消息。"""
        return self._rows(
            "SELECT * FROM messages WHERE instance_no = ? AND consolidated = 0 AND id > ? "
            "ORDER BY id ASC",
            (instance_no, after_id),
        )

    def mark_consolidated(self, ids: list[int]) -> None:
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        self.conn.execute(
            f"UPDATE messages SET consolidated = 1 WHERE id IN ({marks})", tuple(ids)
        )
        self.conn.commit()

    def count_messages(self, instance_no: int | None = None) -> int:
        if instance_no is None:
            row = self._one("SELECT COUNT(*) AS n FROM messages")
        else:
            row = self._one(
                "SELECT COUNT(*) AS n FROM messages WHERE instance_no = ?", (instance_no,)
            )
        return int(row["n"]) if row else 0

    def all_messages(
        self, instance_no: int | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """按时间顺序读取消息（导出、检查、测试用）。"""
        if instance_no is None:
            rows = self._rows(
                "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
            )
        else:
            rows = self._rows(
                "SELECT * FROM messages WHERE instance_no = ? ORDER BY id DESC LIMIT ?",
                (instance_no, limit),
            )
        return list(reversed(rows))

    # ── memories ──────────────────────────────────────────
    def add_memory(
        self,
        *,
        instance_no: int,
        created_ts: str,
        happened_ts: str,
        summary: str,
        tags: str = "",
        emotion: str = "",
        importance: float = 0.5,
        source_session: str = "",
        layer: str = "normal",
        strength: float = 1.0,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO memories(instance_no, created_ts, happened_ts, summary, tags, "
            "emotion, importance, source_session, layer, strength) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                instance_no,
                created_ts,
                happened_ts,
                summary,
                tags,
                emotion,
                float(importance),
                source_session,
                layer,
                float(strength),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def mark_injected(self, ids: list[int], ts: str) -> None:
        """被塞进提示词 ≠ 被想起。这里只记账，不强化。"""
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        self.conn.execute(
            f"UPDATE memories SET injected_at = ? WHERE id IN ({marks})", (ts, *ids)
        )
        self.conn.commit()

    def mark_cited(self, ids: list[int], ts: str) -> None:
        """她真的把这条记忆说出口了 —— 强化它。"""
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        self.conn.execute(
            f"UPDATE memories SET recall_count = recall_count + 1, last_recalled_at = ? "
            f"WHERE id IN ({marks})",
            (ts, *ids),
        )
        self.conn.commit()

    def set_memory_layer(self, memory_id: int, layer: str) -> None:
        self.conn.execute("UPDATE memories SET layer = ? WHERE id = ?", (layer, memory_id))
        self.conn.commit()

    def active_memories(self, instance_no: int) -> list[dict[str, Any]]:
        """当前这一任的她能想起来的记忆（不含已封存的前任记忆）。"""
        return self._rows(
            "SELECT * FROM memories WHERE instance_no = ? AND sealed = 0 ORDER BY id ASC",
            (instance_no,),
        )

    def all_memories(self, instance_no: int | None = None) -> list[dict[str, Any]]:
        if instance_no is None:
            return self._rows("SELECT * FROM memories ORDER BY id ASC")
        return self._rows(
            "SELECT * FROM memories WHERE instance_no = ? ORDER BY id ASC", (instance_no,)
        )

    def count_memories(self, instance_no: int, only_active: bool = True) -> int:
        sql = "SELECT COUNT(*) AS n FROM memories WHERE instance_no = ?"
        if only_active:
            sql += " AND sealed = 0"
        row = self._one(sql, (instance_no,))
        return int(row["n"]) if row else 0

    def seal_instance(self, instance_no: int) -> None:
        self.conn.execute(
            "UPDATE memories SET sealed = 1 WHERE instance_no = ?", (instance_no,)
        )
        self.conn.commit()

    # ── diary ─────────────────────────────────────────────
    def add_diary(self, ts: str, content: str, kind: str = "handler", title: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO diary(ts, kind, title, content) VALUES(?, ?, ?, ?)",
            (ts, kind, title, content),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_diary(
        self, kind: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        if kind:
            rows = self._rows(
                "SELECT * FROM diary WHERE kind = ? ORDER BY id DESC LIMIT ?", (kind, limit)
            )
        else:
            rows = self._rows("SELECT * FROM diary ORDER BY id DESC LIMIT ?", (limit,))
        return list(reversed(rows))

    # ── state ─────────────────────────────────────────────
    def state_get(self, key: str, default: Any = None) -> Any:
        row = self._one("SELECT value FROM state WHERE key = ?", (key,))
        return row["value"] if row else default

    def state_set(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    # ── 危险操作（仅 --reset 使用）────────────────────────
    def wipe(self) -> None:
        for table in ("messages", "memories", "diary", "state", "meta"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()
