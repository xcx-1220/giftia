"""检查/修复：迁移时加的 `from giftia.store import Store` 是否出现在**第一次用到 Store 之前**。"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
PROBES = ROOT / ".review" / "probes"

for path in sorted(PROBES.glob("*.py")):
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        continue
    lines = src.split("\n")
    import_lines = [
        i + 1
        for i, line in enumerate(lines)
        if line.startswith("from giftia.store import Store")
    ]
    if not import_lines:
        continue
    store_lines = [
        i + 1 for i, line in enumerate(lines) if re.search(r"\bStore\b", line)
        and i + 1 not in import_lines
    ]
    first = min(store_lines) if store_lines else None
    ok = first is None or min(import_lines) < first
    print(f"{path.name}: import@{import_lines} 首次使用@{first} -> {'OK' if ok else '**NEED FIX**'}")
