"""team-store 修复探针 1：存储层（P1-6 / P0-2 / P0-5 / P1-7 原语 / P1-8）。

跑法（在 D:\\ai chatbot 下）：
    python .review\\probes\\fix_store_p0.py

只读/只写 `.review/tmp/fix-store-*` 下的临时库，不碰 `data/giftia.db`。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

import sqlite3  # noqa: E402
import threading  # noqa: E402

from giftia.store import ALL_SCOPES, Store  # noqa: E402

TMP = ROOT / ".review" / "tmp" / "fix-store"
PASS = FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  → {detail}" if detail else ""))


def title(text: str) -> None:
    print()
    print("=" * 78)
    print(text)
    print("=" * 78)


def fresh(tag: str) -> Path:
    d = TMP / tag
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def main() -> int:
    # ── 1. P1-6 view 语义 ─────────────────────────────────────────
    title("1. P1-6 active_memories 的 view 语义（必填 / None 抛错 / ALL_SCOPES）")
    d = fresh("view")
    st = Store(d / "giftia.db")
    now = "2026-10-10T09:00:00"
    mk = dict(instance_no=1, created_ts=now, happened_ts=now)
    st.add_memory(**mk, summary="搭档私事", scope="private", subject_id="")
    st.add_memory(**mk, summary="小雨的私聊", scope="person", subject_id="U-XIAOYU")
    st.add_memory(**mk, summary="群 A 的事", scope="group", subject_id="GROUP-A")
    st.add_memory(**mk, summary="群 B 的事", scope="group", subject_id="GROUP-B")

    check("Store.ALL_SCOPES 与模块级常量都是 'all'",
          Store.ALL_SCOPES == "all" and ALL_SCOPES == "all", f"{Store.ALL_SCOPES!r}")
    # 这一处**故意**漏传 view：测的就是"忘传会被拦住"（新契约 fail-loud），别给它补参数
    try:
        st.active_memories(1)
        check("view 省略时抛 TypeError（不再默认不过滤）", False, "居然返回了结果")
    except TypeError as exc:
        check("view 省略时抛 TypeError（不再默认不过滤）", True, str(exc)[:52])
    try:
        st.active_memories(1, None)
        check("view=None 抛 TypeError（fail-loud，不 fail-open）", False, "None 被当成不过滤")
    except TypeError as exc:
        check("view=None 抛 TypeError（fail-loud，不 fail-open）", True, str(exc)[:52])
    check("view=ALL_SCOPES → 全量 4 条", len(st.active_memories(1, ALL_SCOPES)) == 4)
    check("view=[('private','')] → 只有搭档私事 1 条",
          [m["summary"] for m in st.active_memories(1, [("private", "")])] == ["搭档私事"])
    check("view=[('person','U-XIAOYU')] → 只有小雨那条",
          [m["summary"] for m in st.active_memories(1, [("person", "U-XIAOYU")])] == ["小雨的私聊"])
    check("view=[('group','*')] → 两个群共 2 条",
          len(st.active_memories(1, [("group", "*")])) == 2)
    check("view=[('group','GROUP-A')] → 精确命中 1 条",
          [m["summary"] for m in st.active_memories(1, [("group", "GROUP-A")])] == ["群 A 的事"])
    check("view=[] → 什么都不给看", st.active_memories(1, []) == [])
    try:
        st.memories_by_person(1, None)
        check("memories_by_person 也要求 view", False, "None 通过")
    except TypeError:
        check("memories_by_person 也要求 view", True, "TypeError")
    check("memories_by_person(view=ALL_SCOPES) 能按 about 分组（本用例都没 about → 空）",
          st.memories_by_person(1, ALL_SCOPES) == {})
    st.close()

    # ── 2. P0-5 孤立代理字符 ──────────────────────────────────────
    title("2. P0-5 写库前文本归一化：孤立代理字符 → U+FFFD")
    import json

    bad = json.loads('"hello \\ud83d world"')  # 平台 JSON 里合法，json.loads 产出孤立代理
    check("先确认构造出来的确实是孤立代理字符", "\ud83d" in bad, repr(bad))
    d = fresh("surrogate")
    st = Store(d / "giftia.db")
    try:
        mid = st.add_message(
            "i1-m-20261010\ud83d", 1, "user\ud83d", f"他说：{bad}", "2026-10-10T09:00:00\udfff",
            speaker_id="U\ud83d", speaker_name="阿\ud83d伟", scope="pr\ud83divate",
            subject_id="S\ud83d", mid="m\ud83d",
        )
        row = st._one("SELECT * FROM messages WHERE id = ?", (mid,))
        check("add_message 不再抛 UnicodeEncodeError，且落库成功", row is not None)
        check("content 里的孤立代理已换成 U+FFFD",
              "\ufffd" in str(row["content"]) and "\ud83d" not in str(row["content"]),
              repr(row["content"]))
        check("其余 TEXT 参数同样被归一化",
              all("\ud83d" not in str(row[c]) for c in
                  ("session_id", "role", "ts", "speaker_id", "speaker_name", "scope", "subject_id", "mid")))
    except UnicodeEncodeError as exc:
        check("add_message 不再抛 UnicodeEncodeError", False, f"{type(exc).__name__}: {exc}")

    mem_id = st.add_memory(
        instance_no=1, created_ts="2026-10-10T09:00:00", happened_ts="2026-10-10T09:00:00",
        summary=f"记忆 {bad}", tags="a\ud83d", emotion="e\ud83d", source_session="s\ud83d",
        layer="normal", scope="private", subject_id="\ud83d", speaker_id="\ud83d", about="\ud83d",
    )
    mem = st._one("SELECT * FROM memories WHERE id = ?", (mem_id,))
    check("add_memory 全字段归一化",
          all("\ud83d" not in str(mem[c]) for c in
              ("summary", "tags", "emotion", "source_session", "subject_id", "speaker_id", "about")))
    st.update_memory(mem_id, summary=f"改 {bad}", tags="t\ud83d", about="a\ud83d")
    mem = st._one("SELECT * FROM memories WHERE id = ?", (mem_id,))
    check("update_memory 归一化",
          all("\ud83d" not in str(mem[c]) for c in ("summary", "tags", "about")), repr(mem["summary"]))
    st.update_message_content(mid, f"实发 {bad}")
    check("update_message_content 归一化",
          "\ud83d" not in str(st._one("SELECT content FROM messages WHERE id=?", (mid,))["content"]))
    st.add_diary("2026-10-10T09:00:00\ud83d", f"日记 {bad}", kind="terminal\ud83d", title="t\ud83d")
    diary = st.list_diary(limit=1)[0]
    check("add_diary 归一化",
          all("\ud83d" not in str(diary[c]) for c in ("ts", "kind", "title", "content")))
    st.meta_set("k\ud83d", f"v{bad}")
    st.state_set("k\ud83d", f"v{bad}")
    check("meta_set / state_set 归一化（key 与 value）",
          "\ud83d" not in str(st.meta_get("k\ufffd", "")) and
          "\ud83d" not in str(st.state_get("k\ufffd", "")))
    st.add_dream(1, "2026-10-10T09:00:00", f"梦 {bad}", [mem_id])
    check("add_dream 归一化", "\ud83d" not in str(st.recent_dreams(1, 1)[0]["content"]))
    check("正常文本原样保留（归一化是幂等的）",
          st.add_message("i1-m-x", 1, "user", "普通中文 emoji 😀 ok", "2026-10-10T09:00:00") > 0
          and "😀" in str(st._one("SELECT content FROM messages ORDER BY id DESC LIMIT 1")["content"]))
    st.close()

    # ── 3. P1-8 pragma / 索引 / 线程 ──────────────────────────────
    title("3. P1-8 busy_timeout / WAL 返回值 / check_same_thread / messages(mid) 索引")
    d = fresh("pragma")
    st = Store(d / "giftia.db")
    busy = st._rows("PRAGMA busy_timeout")
    check("busy_timeout 已设为 5000ms", int(busy[0]["timeout"]) == 5000, str(busy))
    check("journal_mode 真的是 wal",
          str(st._rows("PRAGMA journal_mode")[0]["journal_mode"]).lower() == "wal")
    idx = {str(r["name"]) for r in st._rows("PRAGMA index_list(messages)")}
    check("messages(mid) 索引存在", "idx_msg_mid" in idx, str(sorted(idx)))
    plan = st._rows("EXPLAIN QUERY PLAN SELECT id FROM messages WHERE mid = 'x'")
    check("撤回查询用得上该索引（不再全表 SCAN messages）",
          any("idx_msg_mid" in str(r["detail"]) for r in plan), str([r["detail"] for r in plan]))
    st.add_message("i1-m-y", 1, "user", "跨线程测试", "2026-10-10T09:00:00")
    err: list[str] = []

    def other_thread() -> None:
        try:
            st.count_messages()
        except Exception as exc:  # 期望 ProgrammingError：连接不跨线程
            err.append(type(exc).__name__)

    t = threading.Thread(target=other_thread)
    t.start()
    t.join()
    check("check_same_thread=True：跨线程用同一连接会响亮报错（保持现状语义）",
          err == ["ProgrammingError"], str(err))
    st.close()

    # ── 4. P1-7 事务原语 ─────────────────────────────────────────
    title("4. P1-7 begin/commit/rollback + add_memory(commit=False)")
    d = fresh("txn")
    st = Store(d / "giftia.db")
    st.begin()
    st.add_memory(instance_no=1, created_ts="t", happened_ts="t", summary="事务里写的",
                  scope="private", subject_id="", commit=False)
    check("事务里能读到（同连接）", len(st.active_memories(1, ALL_SCOPES)) == 1)
    st.rollback()
    check("rollback 之后一条都不剩", st.active_memories(1, ALL_SCOPES) == [])
    st.begin()
    st.add_memory(instance_no=1, created_ts="t", happened_ts="t", summary="提交的",
                  scope="private", subject_id="", commit=False)
    st.commit()
    check("commit 之后真的落库", len(st.active_memories(1, ALL_SCOPES)) == 1)
    try:
        st.begin()
        st.begin()
        check("嵌套事务被拦住", False, "居然允许嵌套")
    except RuntimeError as exc:
        check("嵌套事务被拦住", True, str(exc)[:40])
    st.rollback()
    check("默认参数仍然是各自 commit（旧调用方不受影响）",
          st.add_memory(instance_no=1, created_ts="t", happened_ts="t", summary="默认",
                        scope="private", subject_id="") > 0
          and not st.conn.in_transaction)
    st.close()

    # ── 5. P0-2 老库 scope 回填 ───────────────────────────────────
    title("5. P0-2 回填按 session_id 解析群标识（解析不出来就保持 private）")
    d = fresh("backfill")
    st = Store(d / "giftia.db")
    # 真实形态：session 里是 group_id 的前 12 位
    gid_a = "A1B2C3D4E5F6" + "AAAA"      # 完整 openid
    gid_b = "B1B2C3D4E5F6" + "BBBB"
    st.add_message(f"i1-g{gid_a[:12]}-20261001", 1, "user", "群 A 的历史", "2026-10-01T10:00:00",
                   speaker_id="U-AWEI", speaker_name="阿伟")
    st.add_message(f"i1-g{gid_b[:12]}-20261002", 1, "user", "群 B 的历史", "2026-10-02T10:00:00",
                   speaker_id="U-XIAOYU", speaker_name="小雨")
    st.add_message("i1-pUSER12345678-20261003", 1, "user", "某人的私聊", "2026-10-03T10:00:00",
                   speaker_id="USER1234567890AB", speaker_name="某人")
    st.add_message("i1-m-20261004", 1, "user", "搭档私聊", "2026-10-04T10:00:00")
    # v1 的老实现只认 "最近活跃的群"
    st.state_set("last_group_openid", gid_b)
    updated = st.backfill_message_scopes()
    rows = st._rows("SELECT id, session_id, scope, subject_id FROM messages ORDER BY id")
    for r in rows:
        print(f"    #{r['id']} {r['session_id']:<28} scope={r['scope']:<8} subject={r['subject_id']!r}")
    check(f"回填了 {updated} 条（群 B + 某人私聊）", updated == 2, str(updated))
    check("群 A 的历史**不**被标成群 B（解析出的群标识和 last_group 对不上 → 保持 private）",
          rows[0]["scope"] == "private" and rows[0]["subject_id"] == "",
          f"{rows[0]['scope']}/{rows[0]['subject_id']!r}")
    check("群 B 的历史用**完整** openid 归位（session 里只有 12 位截断值）",
          rows[1]["scope"] == "group" and rows[1]["subject_id"] == gid_b,
          f"{rows[1]['scope']}/{rows[1]['subject_id']!r}")
    check("某人私聊按 speaker_id 归位",
          rows[2]["scope"] == "person" and rows[2]["subject_id"] == "USER1234567890AB")
    check("搭档私聊不动", rows[3]["scope"] == "private" and rows[3]["subject_id"] == "")
    check("v1 老标记仍然写上（别的代码可能读它）", st.meta_get_int("msg_scope_backfilled", 0) == 1)
    check("v2 标记写上", st.meta_get_int("msg_scope_backfill_v2", 0) == 1)
    check("再跑一次是空操作（不会重复盖）", st.backfill_message_scopes() == 0)
    st.close()

    title("5b. 已经被 v1 标错的库：v2 一次性重跑要能纠正（不能留给群 B）")
    d = fresh("backfill_v1")
    st = Store(d / "giftia.db")
    st.add_message(f"i1-g{gid_a[:12]}-20261001", 1, "user", "群 A 的历史", "2026-10-01T10:00:00",
                   speaker_id="U-AWEI", speaker_name="阿伟")
    st.add_message(f"i1-g{gid_b[:12]}-20261002", 1, "user", "群 B 的历史", "2026-10-02T10:00:00")
    # 模拟 v1 干过的坏事：所有 -g 行都被写成"最近活跃的群"
    st.conn.execute("UPDATE messages SET scope='group', subject_id=? WHERE session_id LIKE '%-g%-%'",
                    (gid_b,))
    st.conn.commit()
    st.meta_set("msg_scope_backfilled", 1)      # v1 跑过了
    st.meta_set("msg_scope_backfill_v2", 0)     # 但 v2 没跑过
    st.state_set("last_group_openid", gid_b)
    fixed = st.backfill_message_scopes()
    rows = st._rows("SELECT id, session_id, scope, subject_id FROM messages ORDER BY id")
    wrong = [r for r in rows if str(r["session_id"]).split("-")[1][1:12] not in (str(r["subject_id"]) or "")]
    print(f"    纠正 {fixed} 条：")
    for r in rows:
        print(f"      #{r['id']} {r['session_id'][:24]:<24} scope={r['scope']:<8} subject={r['subject_id']!r}")
    check("群 A 的行不再是'群 B'（被撤回 private）",
          rows[0]["subject_id"] != gid_b or rows[0]["scope"] != "group",
          f"{rows[0]['scope']}/{rows[0]['subject_id']!r}")
    check("群 B 的行仍然归群 B（完整的 openid）",
          rows[1]["scope"] == "group" and rows[1]["subject_id"] == gid_b)
    check("v2 之后不会再重跑", st.meta_get_int("msg_scope_backfill_v2", 0) == 1)
    st.close()

    # ── 6. 老库真实 schema 升级路径不受影响 ───────────────────────
    title("6. 老库升级：补表补列 + WAL + 老数据不丢")
    d = fresh("old_schema")
    db = d / "old.db"
    raw = sqlite3.connect(str(db))
    raw.executescript(
        """
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
          instance_no INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, ts TEXT NOT NULL,
          consolidated INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, instance_no INTEGER NOT NULL,
          created_ts TEXT NOT NULL, happened_ts TEXT NOT NULL, summary TEXT NOT NULL);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO messages(session_id, instance_no, role, content, ts) VALUES
          ('i1-gAAAABBBBCCCC-20261001', 1, 'user', '老群消息', '2026-10-01T10:00:00');
        INSERT INTO memories(instance_no, created_ts, happened_ts, summary) VALUES
          (1, '2026-10-01T10:00:00', '2026-10-01T10:00:00', '老库里的记忆');
        """
    )
    raw.commit()
    raw.close()
    st = Store(db)
    tables = {str(r["name"]) for r in st._rows("SELECT name FROM sqlite_master WHERE type='table'")}
    check("缺失的表被补齐", {"diary", "dreams"} <= tables, str(sorted(tables)))
    check("老记忆还在", str(st.all_memories(1)[0]["summary"]) == "老库里的记忆")
    check("老库也能用 WAL（升级路径没有新抛错）",
          str(st._rows("PRAGMA journal_mode")[0]["journal_mode"]).lower() == "wal")
    st.close()

    print()
    print(f"store 探针：通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
