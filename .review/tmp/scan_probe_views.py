"""复查：.review/probes/*.py 里还有没有不带 view 的 active_memories 调用（AST 级）。"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
PROBES = ROOT / ".review" / "probes"

for path in sorted(PROBES.glob("*.py")):
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        print(f"{path.name}: 语法错误 {exc}")
        continue
    lines = src.split("\n")
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "active_memories":
            has_kw = any(kw.arg == "view" for kw in node.keywords)
            if has_kw or len(node.args) >= 2:
                continue
            snippet = lines[node.lineno - 1].strip()
            hits.append((node.lineno, snippet[:96]))
    for ln, snip in sorted(hits):
        print(f"{path.name}:{ln}: {snip}")
print("（以上是仍不带 view 的调用；空 = 全部已迁移）")
sys.exit(0)
