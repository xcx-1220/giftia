"""审读服务器上的存量数据，判断"要不要重跑消息来修正记忆"。

只读：从服务器读消息/记忆的元数据（不打印记忆内容），在本机做统计。
用法：python .review/deploy/audit_memories.py <ssh目标>
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter, defaultdict

TARGET = sys.argv[1] if len(sys.argv) > 1 else "ubuntu@150.158.75.223"

REMOTE = r"""
import json, sqlite3
c = sqlite3.connect("/opt/giftia/data/giftia.db")
c.row_factory = sqlite3.Row
mem = [dict(r) for r in c.execute(
    "SELECT id, instance_no, created_ts, happened_ts, summary, tags, about, emotion,"
    " importance, layer, sealed, scope, subject_id, speaker_id, source_session,"
    " event_date, merged_into, forgotten, recall_count, strength FROM memories")]
msg = [dict(r) for r in c.execute(
    "SELECT id, session_id, instance_no, role, ts, consolidated, scope, subject_id,"
    " speaker_id, speaker_name, mid, recalled FROM messages")]
state = dict(c.execute("SELECT key, value FROM state"))
meta = dict(c.execute("SELECT key, value FROM meta"))
print(json.dumps({"memories": mem, "messages": msg, "state": state, "meta": meta},
                 ensure_ascii=False))
"""


def main() -> int:
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", TARGET,
         f"sudo -n -u giftia python3 -c '{REMOTE}'"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        print("读取失败：", proc.stderr[:400])
        return 2
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    mems, msgs = data["memories"], data["messages"]
    state, meta = data["state"], data["meta"]

    print("=" * 74)
    print("1. 规模")
    print("=" * 74)
    print(f"  消息 {len(msgs)} 条 / 记忆 {len(mems)} 条 / 实例 #{meta.get('instance_no')}")
    live = [m for m in mems if not m["sealed"] and not m["merged_into"] and not m["forgotten"]]
    print(f"  活跃记忆 {len(live)} 条（sealed={sum(1 for m in mems if m['sealed'])} "
          f"merged={sum(1 for m in mems if m['merged_into'])} "
          f"forgotten={sum(1 for m in mems if m['forgotten'])}）")

    print()
    print("=" * 74)
    print("2. 归属分布（隐私闸门的关键）")
    print("=" * 74)
    by_scope = Counter((m["scope"], "有主体" if m["subject_id"] else "无主体") for m in mems)
    for (scope, sub), n in by_scope.most_common():
        print(f"  scope={scope:<8} {sub}  {n} 条")
    print()
    print("  会话分布（消息）:")
    for sid, n in Counter(m["session_id"] for m in msgs).most_common(12):
        sample = next(m for m in msgs if m["session_id"] == sid)
        print(f"    {sid:<28} {n:>4} 条   scope={sample['scope']:<8} subject={sample['subject_id'][:16]!r}")

    print()
    print("=" * 74)
    print("3. 有没有旧 bug 留下的脏数据")
    print("=" * 74)
    # a) group 归属但没有主体 → 群聊记忆不知道该给哪个群看
    bad_group = [m for m in mems if m["scope"] == "group" and not m["subject_id"]]
    print(f"  a) scope=group 但 subject 为空：{len(bad_group)} 条 → 这些记忆任何群都查不到（等于白记）")
    # b) 群记忆的主体与它来源会话里的群标识不一致 → 跨群串味
    mismatched = []
    for m in mems:
        sid = str(m.get("source_session") or "")
        mm = re.match(r"^i\d+-g(.+?)-\d{8}$", sid)
        if mm and str(m["scope"]) == "group":
            sid_group = mm.group(1)
            sub = str(m["subject_id"] or "")
            if sub and not (sub.startswith(sid_group) or sid_group.startswith(sub[:12])):
                mismatched.append((m["id"], sid, sub))
    print(f"  b) 群记忆的 subject 与来源会话里的群不符：{len(mismatched)} 条")
    for i, sid, sub in mismatched[:8]:
        print(f"       #{i} 会话={sid} subject={sub[:20]}")
    # c) person 归属但无主体 → 别人私聊的记忆无法区分是谁
    bad_person = [m for m in mems if m["scope"] == "person" and not m["subject_id"]]
    print(f"  c) scope=person 但 subject 为空：{len(bad_person)} 条")
    # d) private 归属里 about 指向别人 → 可能是把别人的事记成了搭档的
    print(f"  d) private 记忆的 about 分布（前 8）：")
    for about, n in Counter(str(m.get("about") or "（空）") for m in mems
                            if m["scope"] == "private").most_common(8):
        print(f"       {about[:20]!r}: {n}")

    print()
    print("=" * 74)
    print("4. 重跑的代价")
    print("=" * 74)
    cons = Counter(m["consolidated"] for m in msgs)
    print(f"  messages.consolidated 分布：{dict(cons)}")
    print(f"  → 若要『重跑消息』，需要把 consolidated 清零、删掉 {len(mems)} 条记忆，"
          f"再按每 6 条一次整理 → 约 {len(msgs) // 6} 次模型调用（真花钱）")
    print(f"  state 键 {len(state)} 个（关系亲密度、纪念日『已提过』、"
          f"outreach 额度账本都在 state 里，重跑不会回滚这些）")

    print()
    print("=" * 74)
    print("5. 结论素材")
    print("=" * 74)
    dirty = len(bad_group) + len(mismatched) + len(bad_person)
    print(f"  需要修的脏数据合计：{dirty} 条")
    print(f"  记忆条数：{len(mems)} 条（重跑会全部重新生成，她会『忘掉』现在的版本）")
    print(f"  消息：{len(msgs)} 条，最老 {min(m['ts'] for m in msgs)[:16]}，最新 {max(m['ts'] for m in msgs)[:16]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
