"""把 .review/probes/*.py 里不满足新契约的 `active_memories(...)` 调用补上视图。

Lead 授权（2026-10-10）：
  · 只加 `view=Store.ALL_SCOPES`（或 `view="all"`）+ 必要的 import；
  · **不许**改断言与期望值；
  · 故意测 `view=None` 语义的调用（参数里已经写了 view）保持原样。

实现：AST 找 `*.active_memories(...)` 且没有 view 关键字、位置参数 < 2 的调用，
在最后一个实参之后插入 `, view=Store.ALL_SCOPES`；缺 `Store` import 的文件补一行。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
PROBES = ROOT / ".review" / "probes"
INSERT = ", view=Store.ALL_SCOPES"


def has_store_import(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "Store" or alias.asname == "Store":
                    return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "giftia.store" and alias.asname in (None, "store"):
                    return True
    return False


def store_name_bound(tree: ast.Module) -> bool:
    """文件里已经有名字 `Store` 吗（import 或赋值都算）？"""
    if has_store_import(tree):
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "Store":
                    return True
    return False


def import_insert_offset(lines: list[str]) -> int:
    """在第几行之前插入 import：放进 import 块的最后一行之后。"""
    last_import = -1
    in_block = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            # 顶层 import：行首无缩进
            if line[:1] not in (" ", "\t"):
                last_import = i
                in_block = True
        elif in_block and stripped and not stripped.startswith("#"):
            in_block = False
    return last_import + 1 if last_import >= 0 else 0


def migrate(path: Path) -> tuple[int, bool]:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        print(f"  !! {path.name}: 语法错误，跳过（{exc}）")
        return 0, False
    insertions: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "active_memories"):
            continue
        if any(kw.arg == "view" for kw in node.keywords):
            continue
        if len(node.args) >= 2:
            continue  # 位置参数已经给了 view
        if node.args:
            pos = node.args[-1].end_col_offset  # 1-based 列 → 见下
            line = node.args[-1].end_lineno
        else:
            pos = func.end_col_offset
            line = func.end_lineno
        insertions.append((line, pos))  # type: ignore[arg-type]
    if not insertions:
        return 0, False
    lines = src.split("\n")
    # 从后往前插，保持偏移有效
    for line_no, col in sorted(insertions, reverse=True):
        text = lines[line_no - 1]
        lines[line_no - 1] = text[:col] + INSERT + text[col:]
    added_import = False
    if not store_name_bound(tree):
        at = import_insert_offset(lines)
        lines.insert(at, "from giftia.store import Store  # noqa: E402")
        added_import = True
    path.write_text("\n".join(lines), encoding="utf-8", newline="")
    return len(insertions), added_import


def main() -> int:
    total = 0
    touched = 0
    for path in sorted(PROBES.glob("*.py")):
        n, imp = migrate(path)
        if n:
            touched += 1
            total += n
            print(f"  {path.name}: {n} 处" + ("（+ import Store）" if imp else ""))
    print(f"共 {touched} 个文件、{total} 处调用已补 view=Store.ALL_SCOPES")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
