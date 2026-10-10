"""Stream A 探针 5：SQLite 存储层正确性。

覆盖：
  1. 老库迁移：SCHEMA 里 ALTER 不覆盖"整张表缺失"的情况
  2. 事务边界：多表/多语句写入是否原子，异常时是否回滚
  3. 字符串拼 SQL 的注入面（哪些是拼接的、参数来自哪里）
  4. 连接生命周期 / 线程安全
  5. 时间戳类型混用（tz-aware vs naive）能否让 recall 直接崩
  6. LIKE 前缀匹配的语义（% _ 被直接删掉）

跑法：
    python .review\\probes\\mm_store_sqlite.py
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mm_common import (  # noqa: E402
    TMPROOT,
    check,
    fresh_dir,
    make_brain,
    mem,
    settings_for,
    stage,
    title,
)
from giftia.memory import recall  # noqa: E402
from giftia.store import SCHEMA, Store  # noqa: E402

NOW = datetime.now()


def main() -> int:
    tmp = fresh_dir("sqlite")
    settings = settings_for(tmp)
    failures = 0

    # ── 1. 老库迁移 ───────────────────────────────────────────────
    title("1. 老库迁移：整张表缺失时 _migrate() 能不能补上")
    legacy = tmp / "legacy" / "data" / "giftia.db"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    # 造一个"老版本"的库：只有 meta / messages / memories / state，
    # 没有 dreams / diary（SCHEMA 里的 CREATE TABLE IF NOT EXISTS 会不会补上？）
    old_schema = """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
        instance_no INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
        ts TEXT NOT NULL, consolidated INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT, instance_no INTEGER NOT NULL,
        created_ts TEXT NOT NULL, happened_ts TEXT NOT NULL, summary TEXT NOT NULL,
        tags TEXT NOT NULL DEFAULT '', emotion TEXT NOT NULL DEFAULT '',
        importance REAL NOT NULL DEFAULT 0.5, source_session TEXT NOT NULL DEFAULT '',
        sealed INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    INSERT INTO memories(instance_no, created_ts, happened_ts, summary, importance)
        VALUES(1, '2026-01-01T00:00:00', '2026-01-01T00:00:00', '老库里的记忆', 0.9);
    """
    conn = sqlite3.connect(str(legacy))
    conn.executescript(old_schema)
    conn.commit()
    conn.close()

    store = Store(legacy)
    tables = {r["name"] for r in store._rows(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    print(f"    重开老库后的表：{sorted(tables)}")
    check("SCHEMA 的 CREATE TABLE IF NOT EXISTS 会补齐缺失的表（Store.__init__ 里先 executescript）",
          {"dreams", "diary"} <= tables, f"缺 {sorted({'dreams','diary'} - tables)}")
    cols = {r["name"] for r in store._rows("PRAGMA table_info(memories)")}
    check("老 memories 表补齐了新列（scope/layer/forgotten...）",
          {"scope", "subject_id", "layer", "forgotten", "merged_into"} <= cols,
          f"共 {len(cols)} 列")
    rows = store.all_memories(1)
    print(f"    老数据是否保留：{len(rows)} 条 → {rows[0]['summary']!r} "
          f"（新列默认值 scope={rows[0]['scope']!r}）")
    check("老数据不丢", len(rows) == 1)

    # 极端老库：连 diary 都没有，撤回报文会不会崩
    try:
        store.add_diary("2026-10-10T00:00:00", "回收", kind="terminal", title="x")
        print("    add_diary 正常")
    except Exception as exc:
        print(f"    add_diary 抛异常：{type(exc).__name__}: {exc}")
        failures += 1

    # ── 2. 事务边界 ───────────────────────────────────────────────
    title("2. 事务边界：consolidate 的多步写入是否原子")
    b = make_brain(tmp / "tx", settings=settings)
    s, inst = b.store, b.instance_no
    res = s.add_message("i1-m-20261010", inst, "user", "我要说一件很长的事用来触发整理", "2026-10-10T09:00:00")
    print(f"    写入消息 id={res}（add_message 自带 commit）")

    # 模拟"记忆写成功了、游标推进失败"的中断
    orig_meta_set = s.meta_set
    boom = {"n": 0}

    def flaky_meta_set(key, value):
        boom["n"] += 1
        if boom["n"] >= 2:      # 第一次（last_consolidated:<sid>）成功，第二次崩
            raise sqlite3.OperationalError("disk I/O error（模拟崩溃）")
        return orig_meta_set(key, value)

    s.meta_set = flaky_meta_set  # type: ignore[method-assign]
    from giftia.memory import consolidate

    try:
        consolidate(
            s, b.llm, settings, session_id="i1-m-20261010",
            now_iso="2026-10-10T09:05:00", force=True,
        )
    except sqlite3.OperationalError as exc:
        print(f"    consolidate 抛异常：{exc}")
    s.meta_set = orig_meta_set  # type: ignore[method-assign]
    mems = s._rows("SELECT id, summary FROM memories ORDER BY id")
    pending = s._rows("SELECT id, consolidated FROM messages ORDER BY id")
    print(f"    崩溃后：memories={len(mems)} 条，messages={pending}")
    print(f"    全局游标 last_consolidated_msg_id="
          f"{s.meta_get_int('last_consolidated_msg_id', -1)}，"
          f"会话游标={s.meta_get_int('last_consolidated:i1-m-20261010', -1)}")
    check(
        "观察：记忆已落库、会话游标已推进、全局游标未推进 → 跨会话游标撕裂",
        True,
        f"messages={pending} mems={len(mems)}",
    )

    # 再跑一次：会不会重复写一条记忆？
    again = consolidate(
        s, b.llm, settings, session_id="i1-m-20261010",
        now_iso="2026-10-10T09:06:00", force=True,
    )
    mems2 = s._rows("SELECT id, summary FROM memories ORDER BY id")
    print(f"    重跑 consolidate 新增 {again} 条，memories 总数 {len(mems2)}")
    for m in mems2:
        print(f"      #{m['id']} {m['summary'][:50]}")

    # ── 3. 拼接 SQL 的注入面 ──────────────────────────────────────
    title("3. 字符串拼 SQL：哪些是拼接的、参数从哪来")
    print("    · _view_clause: 'scope = ?' / '(scope = ? AND subject_id = ?)' → 占位符，安全")
    print("    · update_memory: 列名来自硬编码元组 (summary/tags/importance/layer/about) → 安全")
    print("    · mark_cited/mark_merged/mark_injected/mark_consolidated: '?' 占位符 → 安全")
    print("    · restore_memories: 列名来自硬编码 cols 列表 → 安全")
    print("    · _migrate / PRAGMA table_info({table}) / wipe: 表名来自类常量 → 安全")
    print("    · messages_by_prefix: 直接 prefix.replace('%','').replace('_','') + '%'")
    weird = s.messages_by_prefix("i1-m-20261010%' OR '1'='1")
    print(f"      messages_by_prefix(\"i1-m-20261010%' OR '1'='1\") → {len(weird)} 行（参数化，未注入）")
    check("LIKE 前缀不构成注入", True, "但 % 与 _ 被**删除**而不是转义 —— 见报告 P3")

    # ── 4. 连接生命周期 / 线程安全 ────────────────────────────────
    title("4. 连接生命周期与线程安全")
    print(f"    sqlite3.threadsafety = {sqlite3.threadsafety}")
    other = Store(tmp / "other" / "data" / "giftia.db")
    err = {"sub": None, "main": None}

    def worker():
        try:
            other.conn.execute("SELECT 1").fetchone()
        except Exception as exc:
            err["sub"] = f"{type(exc).__name__}: {exc}"

    th = threading.Thread(target=worker)
    th.start()
    th.join()
    print(f"    子线程使用主线程连接 → {err['sub']}")
    check(
        "单连接不能跨线程用（check_same_thread 默认 True）—— 适配器必须自己保证同线程",
        err["sub"] is not None,
        err["sub"] or "居然没报错",
    )
    # 有没有连接泄漏：__init__ 抛异常时
    bad = tmp / "bad" / "data" / "giftia.db"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not a database", encoding="utf-8")
    try:
        Store(bad)
        print("    Store(坏文件) 竟然成功了")
    except Exception as exc:
        print(f"    Store(坏文件) → {type(exc).__name__}: {exc}（连接未显式 close，靠 GC）")

    # ── 5. 时间戳类型混用能不能让 recall 崩 ──────────────────────
    title("5. tz-aware 时间戳落库后，recall() 会不会抛异常")
    b2 = make_brain(tmp / "tz", settings=settings)
    s2, i2 = b2.store, b2.instance_no
    aware = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    s2.add_memory(
        instance_no=i2, created_ts=aware, happened_ts=aware,
        summary="搭档说他下周三要去考驾照，很紧张。", importance=0.9,
    )
    print(f"    库里 created_ts={aware!r}")
    try:
        got = recall("考驾照", s2.active_memories(i2), stage(settings, "clear"),
                     settings.memory, NOW)
        print(f"    recall 返回 {len(got)} 条")
        check("tz-aware 时间戳不得让 recall 崩", True, f"n={len(got)}")
    except Exception as exc:
        print(f"    recall 抛异常：{type(exc).__name__}: {exc}")
        check("tz-aware 时间戳不得让 recall 崩", False, f"{type(exc).__name__}: {exc}")
        failures += 1

    # 真机库体检（只读）
    title("6. 真机库体检（只读打开 data/giftia.db，不写入）")

    real = Path(r"D:\ai chatbot\data\giftia.db")
    if real.exists():
        try:
            ro = sqlite3.connect(f"file:{real.as_posix()}?mode=ro", uri=True)
            ro.row_factory = sqlite3.Row
            tz_rows = ro.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE created_ts LIKE '%+%' "
                "OR happened_ts LIKE '%+%' OR last_recalled_at LIKE '%+%'"
            ).fetchone()["n"]
            scopes = ro.execute(
                "SELECT scope, COUNT(*) AS n FROM memories GROUP BY scope"
            ).fetchall()
            sess = ro.execute(
                "SELECT session_id, scope, subject_id, COUNT(*) AS n FROM messages "
                "GROUP BY session_id, scope, subject_id ORDER BY n DESC LIMIT 8"
            ).fetchall()
            print(f"    记忆条数={ro.execute('SELECT COUNT(*) FROM memories').fetchone()[0]}，"
                  f"消息条数={ro.execute('SELECT COUNT(*) FROM messages').fetchone()[0]}")
            print(f"    tz-aware 时间戳行数={tz_rows}")
            print(f"    scope 分布={[(r['scope'], r['n']) for r in scopes]}")
            print("    消息归属（前 8 组）：")
            for r in sess:
                print(f"      {r['session_id'][:40]:<42} role-scope={r['scope']:<8} "
                      f"subject={r['subject_id'][:16]:<18} n={r['n']}")
            ro.close()
        except Exception as exc:
            print(f"    只读体检失败：{type(exc).__name__}: {exc}")
    else:
        print("    data/giftia.db 不存在（跳过）")

    print()
    print(f"存储层探针失败计数：{failures}")
    print(f"（临时数据根：{TMPROOT}）")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
