"""深挖：那 2 条 scope=private 的群聊消息是什么？以及重跑到底"改了哪一层"。

只读。用法：python .review/deploy/audit_deep.py <ssh目标>
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter

TARGET = sys.argv[1] if len(sys.argv) > 1 else "ubuntu@150.158.75.223"

REMOTE = r"""
import json, sqlite3
c = sqlite3.connect("/opt/giftia/data/giftia.db")
c.row_factory = sqlite3.Row
rows = [dict(r) for r in c.execute(
    "SELECT id, session_id, role, ts, consolidated, scope, subject_id, speaker_id,"
    " speaker_name, mid, recalled, substr(content,1,40) AS head FROM messages ORDER BY id")]
state = dict(c.execute("SELECT key, value FROM state"))
meta = dict(c.execute("SELECT key, value FROM meta"))
print(json.dumps({"messages": rows, "state": state, "meta": meta}, ensure_ascii=False))
"""


def main() -> int:
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", TARGET,
         f"sudo -n -u giftia python3 -c '{REMOTE}'"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        print("读取失败：", proc.stderr[:300])
        return 2
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    msgs, state, meta = data["messages"], data["state"], data["meta"]

    print("=" * 74)
    print("A. 群会话里 scope 不是 group 的消息（旧代码留下的边界）")
    print("=" * 74)
    weird = [m for m in msgs if "-g" in str(m["session_id"]) and m["scope"] != "group"]
    print(f"  共 {len(weird)} 条")
    for m in weird:
        print(f"   #{m['id']} {m['session_id']} role={m['role']} scope={m['scope']!r} "
              f"subject={m['subject_id'][:14]!r} ts={m['ts'][:16]} head={m['head'][:24]!r}")

    print()
    print("=" * 74)
    print("B. 每个群会话的群标识 vs 其消息的 subject（有没有串味）")
    print("=" * 74)
    by_sess: dict[str, Counter] = {}
    for m in msgs:
        by_sess.setdefault(str(m["session_id"]), Counter())[str(m["subject_id"])] += 1
    for sid, cnt in sorted(by_sess.items()):
        if "-g" in sid:
            print(f"  {sid:<28} subject 分布={dict(cnt)}")

    print()
    print("=" * 74)
    print("C. 『重跑』能修正的是什么（按数据流分层）")
    print("=" * 74)
    print("  ① 消息层（messages）：原始对话，重跑不改它（它本来就是原料）")
    print(f"     - 群里的话 {sum(1 for m in msgs if m['scope'] == 'group')} 条 / "
          f"私聊 {sum(1 for m in msgs if m['scope'] == 'private')} 条 / "
          f"别人私聊 {sum(1 for m in msgs if m['scope'] == 'person')} 条")
    print("  ② 记忆层（memories）：由模型从消息压缩而来 —— **重跑改的是这一层**")
    print("     但：模糊度、置信度、保留率、排序、隐私过滤都是**读取时**算的，")
    print("     新版代码已经在用新规则读这 106 条旧记忆 → 不需要重跑就已经生效")
    print("  ③ 状态层（state/meta）：关系亲密度、纪念日已提过、outreach 额度、称呼、")
    print("     绑定信息 —— 重跑**不会回滚**这些（它们是独立记账）")
    print(f"     当前 state 键：{sorted(state.keys())}")

    print()
    print("=" * 74)
    print("D. 重跑的机械代价")
    print("=" * 74)
    print(f"  - 要把 {sum(1 for m in msgs if m['consolidated'])} 条 consolidated 清零，"
          f"删掉 106 条记忆（其中 12 条已被合并标记）")
    print(f"  - 约 {len(msgs) // 6} 次 extract_memories 调用；每次调用会把**群聊原文**"
          f"（含别人的话）送给模型")
    print(f"  - 现在有 {len([m for m in msgs if m['scope'] == 'group'])} 条群聊消息，"
          f"重跑后生成的记忆归属取决于当时的 session_id 解析 —— 这版是好的")
    print("  - 梦（5 场）与回忆录不动；但记忆内容会整体换一批")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
