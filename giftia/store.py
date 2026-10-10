"""SQLite 持久化层：消息、记忆条目、日志、全局状态。

一张表一件事，刻意保持简单——你以后想换成 Postgres 或加向量库，
只要替换这个文件即可，上层不依赖具体实现。
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

# 显式的"我要看全量"视角（见 active_memories）。
# **只允许导出 / 自检 / 迁移使用**，调用点必须写一句注释说明为什么可以看全量——
# 默认值方向错误的 API（view=None = 不过滤）正是 P0-1 / P0-4 的共同根因。
ALL_SCOPES = "all"

# 孤立代理字符（\ud800-\udfff）：平台 JSON 里的 "\ud83d" 是合法 JSON，
# json.loads 会产出它，而 SQLite 编码不了 → UnicodeEncodeError（P0-5）。
_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def sanitize_text(value: Any) -> Any:
    """写库前的文本归一化：孤立代理字符 → U+FFFD，其余原样返回。

    为什么要在这里做：解码层拦不住它（它根本不是非法 UTF-8 字节），
    只有落到 SQLite 绑定参数的那一刻才会炸。炸的后果是一条消息丢掉 +
    适配器把异常当断线、整条连接重来（P0-5）。归一化是**幂等**的：
    正常文本一个字节都不会变。
    """
    if not isinstance(value, str):
        return value
    if _LONE_SURROGATE_RE.search(value):
        return _LONE_SURROGATE_RE.sub("\ufffd", value)
    return value


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
    consolidated INTEGER NOT NULL DEFAULT 0,
    -- 平台消息 id：撤回要按它找，调试和引用回复也用得上
    mid          TEXT    NOT NULL DEFAULT '',
    recalled     INTEGER NOT NULL DEFAULT 0
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
    injected_at      TEXT,
    -- 「他的大日子」：他说过的将来某一天（考试/面试/生日/约定），到那天她会记得
    event_date       TEXT,
    -- 这条记忆属于谁：
    --   private = 搭档的私事（只有你和她知道）
    --   person  = 某个人的私聊（subject_id = 那个人的 openid）
    --   group   = 某个群里公开发生的事（subject_id = group_openid）
    scope            TEXT    NOT NULL DEFAULT 'private',
    subject_id       TEXT    NOT NULL DEFAULT '',
    -- 原话是谁说的（群里分清谁是谁）
    speaker_id       TEXT    NOT NULL DEFAULT '',
    -- 这条记忆是关于谁的（搭档 / 某人的名字 / 群里）。
    -- 决定她能不能分清"这是谁的事"——真机数据里她曾把 A 的课记成 B 的。
    about            TEXT    NOT NULL DEFAULT '',
    -- 被合并进哪一条（>0 = 这条是重复的，已经并进那条了，不再参与检索）
    merged_into      INTEGER NOT NULL DEFAULT 0,
    -- 被明确要求忘掉的（1 = 不再参与检索，但数据还在、可回溯）
    forgotten        INTEGER NOT NULL DEFAULT 0
);

-- 她做过的梦。梦不是记忆：不进检索、不参与退化，只在第二天讲给你听。
CREATE TABLE IF NOT EXISTS dreams (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_no INTEGER NOT NULL,
    created_ts  TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    source_ids  TEXT    NOT NULL DEFAULT '',
    told        INTEGER NOT NULL DEFAULT 0
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
"""

# 索引单独一份、在 _migrate() **之后**建：
# 老库可能连索引用到的列都还没有（mid 是后加的），先建索引会直接
# `no such column` 打不开库。索引不是数据，晚一步没有任何代价。
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_msg_session  ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_msg_open     ON messages(instance_no, consolidated);
CREATE INDEX IF NOT EXISTS idx_msg_mid      ON messages(mid);
CREATE INDEX IF NOT EXISTS idx_mem_instance ON memories(instance_no, sealed);
"""


class Store:
    # 模块级常量的别名：`Store.ALL_SCOPES` 与 `giftia.store.ALL_SCOPES` 两种写法都成立
    ALL_SCOPES = ALL_SCOPES

    # 旧版本数据库缺的列在这里补齐（ALTER TABLE ADD COLUMN 不影响已有数据）
    _MIGRATIONS: dict[str, dict[str, str]] = {
        "memories": {
            "layer": "TEXT NOT NULL DEFAULT 'normal'",
            "sealed": "INTEGER NOT NULL DEFAULT 0",
            "strength": "REAL NOT NULL DEFAULT 1.0",
            "recall_count": "INTEGER NOT NULL DEFAULT 0",
            "last_recalled_at": "TEXT",
            "injected_at": "TEXT",
            "event_date": "TEXT",
            "scope": "TEXT NOT NULL DEFAULT 'private'",
            "subject_id": "TEXT NOT NULL DEFAULT ''",
            "speaker_id": "TEXT NOT NULL DEFAULT ''",
            "about": "TEXT NOT NULL DEFAULT ''",
            "merged_into": "INTEGER NOT NULL DEFAULT 0",
            "forgotten": "INTEGER NOT NULL DEFAULT 0",
        },
        "messages": {
            "speaker_id": "TEXT NOT NULL DEFAULT ''",
            "speaker_name": "TEXT NOT NULL DEFAULT ''",
            "scope": "TEXT NOT NULL DEFAULT 'private'",
            "subject_id": "TEXT NOT NULL DEFAULT ''",
            "recalled": "INTEGER NOT NULL DEFAULT 0",
            "mid": "TEXT NOT NULL DEFAULT ''",
        },
    }

    # ── 记忆快照：合并是半破坏性的，要能回滚 ────────────────
    def snapshot_memories(self, tag: str = "auto", keep: int = 5) -> Path:
        """把记忆表整表拍一份 JSON 快照，只保留最近 keep 份。

        为什么需要：`reconcile` 的合并会把她记忆里的重复条目藏起来
        （mark_merged 虽然不删数据，但一旦模型判错、把两件事合成一件，
        她是真的会忘掉一件事）。拍照是唯一能让人放心的兜底。
        """
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = self.path.parent / "snapshots"
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"memories-{stamp}-{tag}.json"
        payload = {
            "taken_at": datetime.now().isoformat(timespec="seconds"),
            "db": str(self.path),
            "memories": self._rows("SELECT * FROM memories ORDER BY id ASC"),
        }
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        # 只留最近 keep 份，别把磁盘塞满
        snaps = sorted(out_dir.glob("memories-*.json"))
        for old in snaps[:-keep] if keep > 0 else []:
            try:
                old.unlink()
            except OSError:
                pass
        return target

    def restore_memories(self, snapshot: Path | str) -> int:
        """从快照整表恢复记忆。返回恢复的条数。

        整表替换（不是"取消合并"）——语义简单、可预期，出事时能一键回到拍照那一刻。
        """
        data = json.loads(Path(snapshot).read_text(encoding="utf-8"))
        rows = data.get("memories") or []
        if not rows:
            return 0
        cols = [
            "id", "instance_no", "created_ts", "happened_ts", "summary", "tags",
            "emotion", "importance", "source_session", "layer", "strength",
            "recall_count", "last_recalled_at", "injected_at", "sealed", "event_date",
            "scope", "subject_id", "speaker_id", "about", "merged_into",
            # forgotten 漏了会怎样：快照恢复后被明确要求"别提了"的记忆**复活**，
            # 又回到检索里 —— 和 forget_memory 的语义正好相反（§5.2）。
            "forgotten",
        ]
        self.conn.execute("DELETE FROM memories")
        for row in rows:
            present = [c for c in cols if c in row]
            marks = ",".join("?" * len(present))
            self.conn.execute(
                f"INSERT INTO memories({','.join(present)}) VALUES({marks})",
                tuple(row[c] for c in present),
            )
        self.conn.commit()
        return len(rows)

    def list_snapshots(self) -> list[Path]:
        out_dir = self.path.parent / "snapshots"
        return sorted(out_dir.glob("memories-*.json")) if out_dir.exists() else []

    # ── 备注名：你给群友起的名字，展示时覆盖昵称 ─────────────
    # 归属主键仍是 openid（昵称会变、会重名，openid 不会）——
    # 真机数据里她曾把"椎名"和群昵称"习惯微笑i"当成两个人。
    def all_notes(self) -> dict[str, str]:
        raw = str(self.state_get("member_notes", "") or "")
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def person_note(self, user_id: str) -> str:
        return self.all_notes().get(str(user_id), "")

    def set_person_note(self, user_id: str, note: str) -> None:
        notes = self.all_notes()
        note = (note or "").strip()[:24]
        if note:
            notes[str(user_id)] = note
        else:
            notes.pop(str(user_id), None)
        self.state_set("member_notes", json.dumps(notes, ensure_ascii=False))

    def __init__(self, db_path: Path | str) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=True 是**现状语义的显式化**（也正好是 sqlite3 的默认值）：
        # 一个连接只在一个线程里用。适配器侧是多线程的（收消息 / 发消息 / 后台整理），
        # 跨线程复用同一个连接会 ProgrammingError —— 保持"谁开谁用"，
        # 但写明白，别让后来的人以为默认是 False 就随手跨线程传。
        self.conn = sqlite3.connect(str(self.path), check_same_thread=True)
        self.conn.row_factory = sqlite3.Row
        # 多个写入口（收消息、整理记忆、后台记账）撞在一起时不要立刻抛
        # "database is locked"：等最多 5 秒再失败。没有它，一次抖动就会顺着
        # P1-3 那条链冒穿到适配器、把整条连接重来。
        self.conn.execute("PRAGMA busy_timeout=5000")
        # WAL 是"读写不互相阻塞"的前提。**失败不能静默降级**：
        # 静默变成 delete 模式之后，表现是随机的 database is locked，最难查。
        mode = str(self.conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        if mode != "wal" and str(self.path) != ":memory:":
            raise RuntimeError(
                f"SQLite 无法为 {self.path} 启用 WAL（journal_mode={mode}）。"
                "WAL 是并发读写的前提，静默降级会让适配器写入随机失败。"
                "请把 data/ 放在本地磁盘（网络盘/部分只读挂载不支持 WAL）。"
            )
        self.conn.executescript(SCHEMA)
        self._migrate()
        # 索引在补列之后建（见 INDEXES 的注释：老库可能连 mid 列都还没有）
        self.conn.executescript(INDEXES)
        self.conn.commit()

    # ── 显式事务 ──────────────────────────────────────────
    # Python 3.14 的 sqlite3 默认 autocommit=False（legacy 事务控制）：
    # 每条 DML 自己开一个隐式事务，而本类的每个写方法又各自 commit ——
    # 于是"写记忆 + 标已整理 + 推游标"这种多步写入中途失败会留下撕裂状态（P1-7）。
    # 需要"要么全成、要么全不成"的地方用 begin()/commit()/rollback() 自己圈起来。
    def begin(self) -> None:
        """开一个显式写事务（BEGIN IMMEDIATE）。

        用 IMMEDIATE 而不是默认的 DEFERRED：立刻拿写锁，冲突当场失败并走
        busy_timeout 等待，而不是拖到 commit 时才报 "database is locked"。
        不支持嵌套——嵌套说明调用方理解错了事务边界。
        """
        if self.conn.in_transaction:
            raise RuntimeError("Store.begin()：已经在一个事务里了，不支持嵌套事务")
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def _migrate(self) -> None:
        """给老库补列。她的记忆比 schema 重要，升级不能丢数据。

        清单要覆盖"最老的 schema 也缺"的列：`sealed` 看着像原始字段，
        但最早的库里没有，而 idx_mem_instance 依赖它 —— 缺了它老库根本打不开。
        """
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

    def meta_set(self, key: str, value: Any, *, commit: bool = True) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (sanitize_text(key), sanitize_text(str(value))),
        )
        if commit:
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
        self,
        session_id: str,
        instance_no: int,
        role: str,
        content: str,
        ts: str,
        speaker_id: str = "",
        speaker_name: str = "",
        scope: str = "private",
        subject_id: str = "",
        mid: str = "",
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO messages(session_id, instance_no, role, content, ts, "
            "speaker_id, speaker_name, scope, subject_id, mid) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sanitize_text(session_id),
                instance_no,
                sanitize_text(role),
                sanitize_text(content),
                sanitize_text(ts),
                sanitize_text(speaker_id),
                sanitize_text(speaker_name),
                sanitize_text(scope),
                sanitize_text(subject_id),
                sanitize_text(str(mid or "")),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def delete_message(self, message_id: int) -> None:
        """删掉一条消息。

        用途：她的一句话**根本没送出去**时，不该留在她的记忆里——
        否则她以为自己说过了，而用户从来没看见。
        """
        self.conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        self.conn.commit()

    def update_message_content(self, message_id: int, content: str) -> None:
        """把一条消息改成实际送达的内容（分段发送只成功了一部分时用）。"""
        self.conn.execute(
            "UPDATE messages SET content = ? WHERE id = ?",
            (sanitize_text(content), message_id),
        )
        self.conn.commit()

    @staticmethod
    def _session_scope_key(session_id: str) -> tuple[str, str]:
        """从会话名解析归属：返回 (scope, token)。

        会话名形如 `i{实例号}-{scope_key}-{日期}`，scope_key 是
        `m`（搭档私聊）/ `g{group_id[:12]}` / `p{openid[:12]}`。
        token 是截断到 12 位的标识（私聊时为 ""）。

        注意：标识里**可能带 "-"**（合成数据、少数平台的 openid），所以不能
        简单按 "-" 切第一段——那样 `i1-gGROUP-A-20261001` 会被解析成 "GROUP"，
        再与 last_group 做前缀比对就会**误确认**成别的群。这里先按
        `i{n}-[gp]…` 取出整段，再去掉末尾的日期段（`-YYYYMMDD`）。
        """
        sid = str(session_id or "")
        m = re.match(r"^i\d+-([gp])(.+)$", sid)
        if not m:
            return "private", ""
        scope = "group" if m.group(1) == "g" else "person"
        token = re.sub(r"-\d{8}$", "", m.group(2))  # 去掉结尾的日期段
        if not token:
            return "private", ""
        return scope, token

    def backfill_message_scopes(self) -> int:
        """给历史消息补 scope/subject_id。返回改动的行数。

        scope 这一列是后加的，老数据全是默认值 'private' —— 于是**昨天群里的话
        会被当成"你的私事"存进私聊记忆**。这里按会话名把它们归位：
          i<n>-m-…  搭档私聊（不动）
          i<n>-g<群id前12位>-…  群聊 → subject 用 session_id 里那个群
          i<n>-p<openid前12位>-…  某人的私聊 → subject 用发言人 id

        **群标识只从 session_id 解析**，不再用全局的 state['last_group_openid']：
        她可以同时在多个群里，用"最近活跃的那个群"会把群 A 的历史标成群 B，
        接着整理成"群 B 的记忆"并在群 B 里当众说出来（P0-2）。
        session 里存的是截断值，所以要与 last_group_openid 做前缀比对**确认**；
        解析不出来或对不上的老行**保持 private**——宁可不给她看，也别看错人。

        跑两遍：v1（旧实现）可能已经把群 A 标成了群 B，那些行不是 private，
        只认 `msg_scope_backfilled` 就再也不会被纠正。所以新标记
        `msg_scope_backfill_v2` 独立判断，并把"能证明标错"的行撤回 private。
        """
        if self.meta_get_int("msg_scope_backfill_v2", 0):
            return 0
        last_group = str(self.state_get("last_group_openid", "") or "")
        rows = self._rows(
            "SELECT id, session_id, speaker_id, scope, subject_id FROM messages "
            "WHERE (scope = 'private' AND subject_id = '') "
            "   OR scope IN ('group', 'person')"
        )
        updated = 0
        for row in rows:
            scope, token = self._session_scope_key(str(row["session_id"]))
            cur_scope = str(row["scope"] or "")
            cur_subject = str(row["subject_id"] or "")
            target: tuple[str, str] | None = None
            if scope == "group" and token:
                if last_group and (last_group.startswith(token) or token.startswith(last_group)):
                    # 比对确认是同一个群：session 里只有 12 位截断值，存完整的 openid
                    target = ("group", last_group)
                elif cur_scope == "group" and not cur_subject.startswith(token):
                    # 已经标成 group 但它不属于这个会话里的群 → v1 标错了，撤回 private
                    target = ("private", "")
            elif scope == "person" and token:
                speaker = str(row["speaker_id"] or "")
                if speaker and (speaker.startswith(token) or token.startswith(speaker)):
                    target = ("person", speaker)
                elif cur_scope == "person" and not cur_subject.startswith(token):
                    target = ("private", "")
            if target is None or target == (cur_scope, cur_subject):
                continue
            self.conn.execute(
                "UPDATE messages SET scope = ?, subject_id = ? WHERE id = ?",
                (target[0], target[1], int(row["id"])),
            )
            updated += 1
        self.conn.commit()
        # 老标记保留（别的代码可能读它），新标记独立判断"要不要重跑一遍"
        self.meta_set("msg_scope_backfilled", 1)
        self.meta_set("msg_scope_backfill_v2", 1)
        return updated

    def sessions_with_pending(self, instance_no: int) -> list[dict[str, Any]]:
        """哪些会话还有没整理成记忆的消息（带上归属，补整理时要用）。

        为什么需要：整理是增量的（游标只往前走），而"攒够 N 条"是按会话算的。
        一段对话聊到一半停了、或者跨过零点，剩下的几条会永远卡在 pending 里。
        真机数据里"认识第一天"那段对话就是这样没变成记忆的。
        """
        return self._rows(
            "SELECT session_id, scope, subject_id, COUNT(*) AS n, "
            "MIN(ts) AS oldest, MAX(ts) AS newest, MAX(id) AS last_id "
            "FROM messages WHERE instance_no = ? AND consolidated = 0 "
            "GROUP BY session_id, scope, subject_id ORDER BY MIN(id) ASC",
            (instance_no,),
        )

    def recent_messages(self, session_id: str, limit: int = 24) -> list[dict[str, Any]]:
        """工作记忆。**被撤回的消息不算**——那是从没发生过的话。"""
        rows = self._rows(
            "SELECT * FROM messages WHERE session_id = ? AND recalled = 0 "
            "ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        return list(reversed(rows))

    def messages_by_prefix(self, prefix: str, limit: int = 40) -> list[dict[str, Any]]:
        """按会话前缀取历史（跨天）。

        session_id 形如 i1-gXXXX-20261007，前缀 i1-gXXXX- 就是"这个群的所有记录"——
        她"往上翻聊天记录"查的就是它，和她在 QQ 里看到的视角一致。
        """
        # LIKE 的通配符要**转义**而不是删掉（P3-4）：session_id 里的 `_` 是合法字符
        # （scope_key 是 `p<uid[:12]>` / `g<gid[:12]>`，不能保证永远没有 `_`）。
        # 以前直接删掉，含 `_` 的会话前缀会静默失配。
        safe = (
            str(prefix)
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        rows = self._rows(
            "SELECT * FROM messages WHERE session_id LIKE ? ESCAPE '\\' AND recalled = 0 "
            "ORDER BY id DESC LIMIT ?",
            (safe + "%", limit),
        )
        return list(reversed(rows))

    def open_messages(
        self, instance_no: int, after_id: int = 0, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        """还没被整理成记忆条目的消息（按会话分开整理，别把别人的对话混进来）。"""
        if session_id is None:
            return self._rows(
                "SELECT * FROM messages WHERE instance_no = ? AND consolidated = 0 "
                "AND recalled = 0 AND id > ? ORDER BY id ASC",
                (instance_no, after_id),
            )
        return self._rows(
            "SELECT * FROM messages WHERE instance_no = ? AND session_id = ? "
            "AND consolidated = 0 AND recalled = 0 AND id > ? ORDER BY id ASC",
            (instance_no, session_id, after_id),
        )

    def first_open_message_id(self, instance_no: int, session_id: str) -> int | None:
        """这个会话里最早一条还没整理的消息 id（没有则 None）。

        给"新会话第一次建整理游标"用：游标必须从这个会话自己的起点往回退，
        不能直接继承全局游标——否则这个会话更早的消息会永久落在窗口外（P1-10）。
        """
        row = self._one(
            "SELECT MIN(id) AS n FROM messages WHERE instance_no = ? AND session_id = ? "
            "AND consolidated = 0 AND recalled = 0",
            (instance_no, session_id),
        )
        return int(row["n"]) if row and row["n"] is not None else None

    def mark_recalled(self, msg_id: str, session_id: str | None = None) -> int:
        """标记一条消息被撤回了。返回标记了几条。

        撤回的消息**再也不发给模型**（工作记忆、群聊记录、记忆整理三条路都不带它）——
        否则她会记住一句从没发生过的话。

        传 `session_id` 就只在那个会话里找：平台 msg_id 理论上全局唯一，但真出现重复时，
        按 mid 全表更新会把**别人群里**同 id 的那条也标成撤回（P3-1）。不传 = 旧行为
        （所有会话里同 mid 的都标记），保持对既有调用方兼容。
        """
        if not msg_id:
            return 0
        if session_id:
            cur = self.conn.execute(
                "UPDATE messages SET recalled = 1, consolidated = 1 "
                "WHERE mid = ? AND session_id = ?",
                (str(msg_id), str(session_id)),
            )
        else:
            cur = self.conn.execute(
                "UPDATE messages SET recalled = 1, consolidated = 1 WHERE mid = ?",
                (str(msg_id),),
            )
        self.conn.commit()
        return int(cur.rowcount or 0)

    def mark_consolidated(self, ids: list[int], *, commit: bool = True) -> None:
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        self.conn.execute(
            f"UPDATE messages SET consolidated = 1 WHERE id IN ({marks})", tuple(ids)
        )
        if commit:
            self.conn.commit()

    def count_messages(self, instance_no: int | None = None) -> int:
        if instance_no is None:
            row = self._one("SELECT COUNT(*) AS n FROM messages")
        else:
            row = self._one(
                "SELECT COUNT(*) AS n FROM messages WHERE instance_no = ?", (instance_no,)
            )
        return int(row["n"]) if row else 0

    def last_message_ts(self, instance_no: int | None = None) -> str:
        """最后一条消息的时间（判断"多久没说话了"用）。"""
        if instance_no is None:
            row = self._one("SELECT ts FROM messages ORDER BY id DESC LIMIT 1")
        else:
            row = self._one(
                "SELECT ts FROM messages WHERE instance_no = ? ORDER BY id DESC LIMIT 1",
                (instance_no,),
            )
        return str(row["ts"]) if row else ""

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
        event_date: str = "",
        scope: str = "private",
        subject_id: str = "",
        speaker_id: str = "",
        about: str = "",
        commit: bool = True,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO memories(instance_no, created_ts, happened_ts, summary, tags, "
            "emotion, importance, source_session, layer, strength, event_date, "
            "scope, subject_id, speaker_id, about) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                instance_no,
                sanitize_text(created_ts),
                sanitize_text(happened_ts),
                sanitize_text(summary),
                sanitize_text(tags),
                sanitize_text(emotion),
                float(importance),
                sanitize_text(source_session),
                sanitize_text(layer),
                float(strength),
                sanitize_text(event_date) or None,
                sanitize_text(scope),
                sanitize_text(subject_id),
                sanitize_text(speaker_id),
                sanitize_text(about),
            ),
        )
        # commit=False：调用方已开显式事务（见 begin()），由它统一提交/回滚
        if commit:
            self.conn.commit()
        return int(cur.lastrowid)

    def update_memory(
        self,
        memory_id: int,
        *,
        summary: str | None = None,
        tags: str | None = None,
        importance: float | None = None,
        layer: str | None = None,
        about: str | None = None,
        commit: bool = True,
    ) -> None:
        """让已经记下的那条长大（记忆演进），而不是再记一条重复的。"""
        sets: list[str] = []
        args: list = []
        for col, val in (
            ("summary", summary),
            ("tags", tags),
            ("importance", importance),
            ("layer", layer),
            ("about", about),
        ):
            if val is not None:
                sets.append(f"{col} = ?")
                args.append(sanitize_text(val) if isinstance(val, str) else val)
        if not sets:
            return
        args.append(memory_id)
        self.conn.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", tuple(args))
        if commit:
            self.conn.commit()

    # ── dreams ────────────────────────────────────────────
    def add_dream(
        self, instance_no: int, created_ts: str, content: str, source_ids: list[int] | None = None
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO dreams(instance_no, created_ts, content, source_ids) "
            "VALUES(?, ?, ?, ?)",
            (
                instance_no,
                sanitize_text(created_ts),
                sanitize_text(content),
                ",".join(str(i) for i in (source_ids or [])),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_dreams(self, instance_no: int, limit: int = 5) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT * FROM dreams WHERE instance_no = ? ORDER BY id DESC LIMIT ?",
            (instance_no, limit),
        )
        return list(reversed(rows))

    def mark_dream_told(self, dream_id: int) -> None:
        self.conn.execute("UPDATE dreams SET told = 1 WHERE id = ?", (dream_id,))
        self.conn.commit()

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

    @staticmethod
    def _view_clause(view: list[tuple[str, str]] | str) -> tuple[str, list]:
        """"她此刻能想起哪些记忆" → SQL 片段。

        view 是 [(scope, subject), ...]：
          ("private", "")            搭档的私事
          ("person", "<openid>")     某个人的私聊
          ("group", "<group_openid>")某个群里公开发生的事（"*" = 所有群）
        view=ALL_SCOPES 是**显式**看全量（导出/自检/迁移专用）；
        view=[] 表示什么都不给看。

        view=None **不再是"不过滤"**：那个默认值是 P0-1/P0-4 以及 /days、
        should_dream 的共同根因（忘传 = 把所有人的事摊开）。现在它按编程错误
        处理，直接抛 TypeError —— fail-loud，不 fail-open。
        """
        if view is None:
            raise TypeError(
                "active_memories(instance_no, view=...) 的 view 不能是 None："
                "None 曾经表示'不过滤'，正是隐私越界的根因。"
                "要看全量请显式传 store.ALL_SCOPES 并注明用途（导出/自检/迁移）；"
                "要看某个视角传 [(scope, subject), ...]；什么都看不到传 []。"
            )
        if isinstance(view, str):
            if view != ALL_SCOPES:
                raise ValueError(f"未知的 view：{view!r}（字符串只接受 ALL_SCOPES）")
            return "", []
        if not view:
            return " AND 0", []
        parts: list[str] = []
        args: list = []
        for scope, subject in view:
            if not subject or subject == "*":
                parts.append("scope = ?")
                args.append(scope)
            else:
                parts.append("(scope = ? AND subject_id = ?)")
                args += [scope, subject]
        return " AND (" + " OR ".join(parts) + ")", args

    def active_memories(
        self, instance_no: int, view: list[tuple[str, str]] | str
    ) -> list[dict[str, Any]]:
        """当前这一任的她能想起来的记忆（不含已封存的前任记忆）。

        view 决定"此刻她能看到谁的事"——见 _view_clause。**必填**，没有默认值：
        这是隐私的闸门，忘传就等于把所有人的事摊开给她。view=ALL_SCOPES
        只给导出/自检/迁移用（调用点要写一句为什么可以看全量）。
        """
        extra, args = self._view_clause(view)
        return self._rows(
            "SELECT * FROM memories WHERE instance_no = ? AND sealed = 0 "
            "AND merged_into = 0 AND forgotten = 0"
            + extra
            + " ORDER BY id ASC",
            (instance_no, *args),
        )

    def memories_by_person(
        self, instance_no: int, view: list[tuple[str, str]] | str
    ) -> dict[str, list[dict[str, Any]]]:
        """按"关于谁"分组 —— **每人一份档案**（她对这个人的长期印象）。

        不用另立一张表：记忆本来就有 `about` 字段，分组就是档案。
        这样"她记得你什么"和"她记得你的事"是同一份数据，不会两处打架。
        view 与 active_memories 同语义（必填）。
        """
        out: dict[str, list[dict[str, Any]]] = {}
        for mem in self.active_memories(instance_no, view=view):
            about = str(mem.get("about") or "").strip()
            if about:
                out.setdefault(about, []).append(mem)
        return out

    def forget_memory(self, memory_id: int) -> bool:
        """让她忘掉一条（你发现她记错了，可以直接抹掉）。

        不是删除：打上 forgotten 标记就不再参与检索，但数据还在、可回溯。
        """
        cur = self.conn.execute(
            "UPDATE memories SET forgotten = 1 WHERE id = ?", (memory_id,)
        )
        self.conn.commit()
        return bool(cur.rowcount)

    def mark_merged(self, ids: list[int], into: int) -> None:
        """把这些重复条目并进 into（不删数据，只是不再参与检索——可回溯）。"""
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        self.conn.execute(
            f"UPDATE memories SET merged_into = ? WHERE id IN ({marks})", (into, *ids)
        )
        self.conn.commit()

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

    # ── 群聊记录：就是 messages 表本身 ──────────────────────
    # 群里每个人的话都以 role="user" 存进 session_id = "g:<group_openid>"，
    # 她自己的回复是 role="assistant" —— 所以"往上翻聊天记录"就是
    # recent_messages("g:<gid>")，和她在 QQ 里看到的视角一致（单一数据源，不另立一张表）。

    # ── diary ─────────────────────────────────────────────
    def add_diary(self, ts: str, content: str, kind: str = "handler", title: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO diary(ts, kind, title, content) VALUES(?, ?, ?, ?)",
            (sanitize_text(ts), sanitize_text(kind), sanitize_text(title), sanitize_text(content)),
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

    def state_set(self, key: str, value: Any, *, commit: bool = True) -> None:
        self.conn.execute(
            "INSERT INTO state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (sanitize_text(key), sanitize_text(str(value))),
        )
        if commit:
            self.conn.commit()

    # ── 危险操作（仅 --reset 使用）────────────────────────
    def wipe(self) -> None:
        for table in ("messages", "memories", "diary", "state", "meta"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()
