"""一次性脚本（team-store）：按 Lead 授权，给 tools/ 里不带 view 的自检调用补上
`view=Store.ALL_SCOPES`。只加参数、不动任何断言/期望值。

用法：python .review/tmp/add_allscopes.py
"""

from __future__ import annotations

import re
from pathlib import Path

TARGETS = [134, 140, 154, 181, 193, 204, 208, 254, 293, 338, 774,
           1096, 1098, 1230, 1233, 1236, 1238, 1344, 1381, 1389, 1408]


def patch(path: str, line_numbers: list[int]) -> None:
    p = Path(path)
    with p.open("r", encoding="utf-8", newline="") as fh:
        lines = fh.read().split("\n")
    changed = []
    for ln in line_numbers:
        idx = ln - 1
        line = lines[idx]
        assert "active_memories(" in line, (path, ln, line)
        assert "view" not in line, (path, ln, line)
        new, n = re.subn(
            r"active_memories\(([^()]*)\)",
            r"active_memories(\1, view=Store.ALL_SCOPES)",
            line,
        )
        assert n == 1, (path, ln, line, n)
        lines[idx] = new
        changed.append((ln, new.strip()))
    with p.open("w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(lines))
    print(f"{path}: 替换 {len(changed)} 处")
    for ln, new in changed:
        print(f"  {ln}: {new}")


def main() -> int:
    patch("tools/selftest.py", TARGETS)

    p = Path("tools/live_check.py")
    with p.open("r", encoding="utf-8", newline="") as fh:
        text = fh.read()
    anchor = "from giftia.memory import retention_of  # noqa: E402"
    assert anchor in text and "from giftia.store import Store" not in text
    text = text.replace(anchor, anchor + "\nfrom giftia.store import Store  # noqa: E402", 1)
    old_call = "mems = brain.store.active_memories(brain.instance_no)"
    assert old_call in text
    text = text.replace(
        old_call,
        "mems = brain.store.active_memories(brain.instance_no, view=Store.ALL_SCOPES)  # 自检：看全量",
        1,
    )
    with p.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    print("tools/live_check.py: 加 import + 1 处调用已补 view")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
