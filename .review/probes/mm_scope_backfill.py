"""Stream A 探针 7：历史消息 scope 回填（真实老库）+ 跨群归属。

`Store.backfill_message_scopes()` 的注释说：
    i<n>-g-…  群聊 → subject 用**最近活跃的那个群**

两个问题都要用**真正的老库**（messages 表里还没有 scope/subject_id 列）才能验：
  A. 多群场景下，所有历史群聊消息都会被贴上"最近活跃的那个群"的 openid → 错误归属；
  B. 归属错误会顺着 consolidate 变成 scope=group/subject=群B 的记忆，
     于是群 A 里说过的事会在**群 B** 被检索到并说出口。

跑法：
    python .review\\probes\\mm_scope_backfill.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mm_common import (  # noqa: E402
    TMPROOT,
    check,
    fresh_dir,
    settings_for,
    title,
)
from giftia.memory import consolidate  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402
from giftia.store import Store  # noqa: E402

SECRET_GROUP_A = "SECRET-ALPHA-群A-3311"
SECRET_GROUP_B = "SECRET-BRAVO-群B-5522"

# 老版本 schema：scope/subject_id/speaker_id 都不存在
OLD_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
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
INSERT INTO messages(session_id, instance_no, role, content, ts) VALUES
 ('i1-gGROUP-A-20261001', 1, 'user', 'SECRET-ALPHA-群A-3311 阿伟在群里说他老婆怀孕了', '2026-10-01T09:00:00'),
 ('i1-gGROUP-B-20261002', 1, 'user', 'SECRET-BRAVO-群B-5522 小雨在群里说她换工作了', '2026-10-02T09:00:00'),
 ('i1-m-20261003',        1, 'user', '搭档私聊说的事', '2026-10-03T09:00:00');
INSERT INTO state(key, value) VALUES ('last_group_openid', 'GROUP-B');
INSERT INTO state(key, value) VALUES ('handler_id', 'handler-openid-0001');
INSERT INTO meta(key, value) VALUES ('instance_no', '1');
"""


def main() -> int:
    tmp = fresh_dir("backfill")
    settings = settings_for(tmp)
    failures = 0

    db = tmp / "old" / "data" / "giftia.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.executescript(OLD_SCHEMA)
    conn.commit()
    conn.close()

    store = Store(db)
    print("    老库升级后（_migrate 补齐了 scope/subject_id/speaker_id，默认值 private/''）：")
    for r in store._rows("SELECT id, session_id, scope, subject_id FROM messages ORDER BY id"):
        print(f"      #{r['id']} {r['session_id']:<24} scope={r['scope']:<8} subject={r['subject_id']!r}")

    title("1. backfill_message_scopes 在多群场景下的归属")
    updated = store.backfill_message_scopes()
    rows = store._rows("SELECT id, session_id, scope, subject_id FROM messages ORDER BY id")
    print(f"    backfill 更新 {updated} 条：")
    for r in rows:
        print(f"      #{r['id']} {r['session_id']:<24} scope={r['scope']:<8} subject={r['subject_id']!r}")
    wrong = [r for r in rows if "gGROUP-A" in r["session_id"] and r["subject_id"] != "GROUP-A"]
    check(
        "P0-7 群 A 的历史消息不得被归属成群 B",
        not wrong,
        (f"#{wrong[0]['id']}（群 A 的消息）被标成 subject={wrong[0]['subject_id']!r}"
         " —— 她最近活跃的是群 B") if wrong else "归属正确",
    )
    if wrong:
        failures += 1

    title("2. 错误归属会不会变成「在群 B 说得出口」的记忆")
    grp = [r for r in rows if "gGROUP-A" in r["session_id"]][0]
    llm = MockLLM(settings.model)
    added = consolidate(store, llm, settings, session_id="i1-gGROUP-A-20261001",
                        now_iso="2026-10-03T09:00:00", force=True,
                        scope=str(grp["scope"]), subject_id=str(grp["subject_id"]))
    mems = store._rows("SELECT id, summary, scope, subject_id FROM memories ORDER BY id")
    print(f"    整理出 {added} 条记忆：")
    for m in mems:
        print(f"      #{m['id']} scope={m['scope']:<8} subject={m['subject_id']:<9} {m['summary'][:46]}")
    visible_b = store.active_memories(1, view=[("group", "GROUP-B")])
    print(f"    群 B 视角可见 {len(visible_b)} 条：{[m['id'] for m in visible_b]}")
    leaked = [m for m in visible_b if SECRET_GROUP_A in str(m["summary"])]
    check(
        "P0-8 群 A 的内容不得出现在群 B 的可见集合里",
        not leaked,
        f"#{[m['id'] for m in leaked]} 泄漏：群 A 的秘密进了群 B 的检索范围" if leaked else "未泄漏",
    )
    if leaked:
        failures += 1
        print("    影响：她在群 B 里被问起时会检索到这条，并可能当众说出来。")

    title("3. 私聊消息（i1-m-…）不会被回填影响")
    priv = [r for r in rows if "i1-m-" in r["session_id"]][0]
    print(f"      #{priv['id']} scope={priv['scope']!r} subject={priv['subject_id']!r}")
    check("私聊消息保持 private/''", priv["scope"] == "private" and priv["subject_id"] == "")

    title("4. 只有一次回填（meta 标记），之后不会再修")
    again = store.backfill_message_scopes()
    print(f"    第二次调用返回 {again}（meta msg_scope_backfilled="
          f"{store.meta_get_int('msg_scope_backfilled', 0)}）")
    check("回填是幂等的（只做一次）", again == 0)

    title("5. 单群场景（对照组）：回填正确")
    db2 = tmp / "single" / "data" / "giftia.db"
    db2.parent.mkdir(parents=True, exist_ok=True)
    c2 = sqlite3.connect(str(db2))
    c2.executescript(OLD_SCHEMA.replace("GROUP-B", "GROUP-A"))
    c2.commit()
    c2.close()
    s2 = Store(db2)
    s2.backfill_message_scopes()
    rows2 = s2._rows("SELECT session_id, scope, subject_id FROM messages ORDER BY id")
    for r in rows2:
        print(f"      {r['session_id']:<24} scope={r['scope']:<8} subject={r['subject_id']!r}")
    ga = [r for r in rows2 if "gGROUP-A" in r["session_id"]][0]
    check("单群场景归属正确", ga["scope"] == "group" and ga["subject_id"] == "GROUP-A",
          f"{ga['scope']}/{ga['subject_id']}")

    title("6. 老库里 scope 列已经是 'private' 的历史群消息（升级后又跑过一轮的情况）")
    db3 = tmp / "second" / "data" / "giftia.db"
    db3.parent.mkdir(parents=True, exist_ok=True)
    c3 = sqlite3.connect(str(db3))
    c3.executescript(OLD_SCHEMA)
    c3.commit()
    c3.close()
    s3 = Store(db3)
    # 先回填一次（拿到 GROUP-B），再插入一条新的群消息并再次回填
    s3.backfill_message_scopes()
    s3.add_message("i1-gGROUP-C-20261005", 1, "user", "群 C 的一句话", "2026-10-05T09:00:00")
    s3.meta_set("msg_scope_backfilled", 0)   # 模拟"手工重跑"
    s3.backfill_message_scopes()
    for r in s3._rows("SELECT id, session_id, scope, subject_id FROM messages "
                      "WHERE session_id LIKE '%gGROUP-C%'"):
        print(f"      #{r['id']} {r['session_id']} scope={r['scope']} subject={r['subject_id']!r}")
    print("    说明：add_message 写入时已经带 scope（新数据不依赖回填），"
          "所以回填只对'升级前就存在的老行'有意义。")

    print()
    print(f"回填探针失败计数：{failures}")
    print(f"（临时数据根：{TMPROOT}）")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
