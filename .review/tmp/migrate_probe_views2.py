"""把 .review/probes/*.py 里不满足新契约的 `active_memories(...)` 调用补上视图（v2）。

Lead 授权（2026-10-10）：
  · 只加 `view=Store.ALL_SCOPES`（或 `view="all"`）+ 必要的 import；
  · **不许**改断言与期望值；
  · 故意测 `view=None` / 故意漏传 view 的调用保持原样（在 EXCLUDE 里点名）。

v1 的教训：`ast` 的 col_offset 是 **UTF-8 字节**偏移，直接当字符下标用会在中文行里
插错位置（把十几个探针插成语法错误）。这里统一用 `_char_col()` 换算成字符下标。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
PROBES = ROOT / ".review" / "probes"
INSERT = ", view=Store.ALL_SCOPES"

# (文件名, 行号)：故意漏传 view 的调用（新契约的 fail-loud 测试），保持原样
EXCLUDE: set[tuple[str, int]] = {
    ("fix_store_p0.py", 73),
}


def _char_col(line: str, byte_col: int) -> int:
    """AST 的列是 UTF-8 字节偏移 → 换算成 str 下标。"""
    return len(line.encode("utf-8")[:byte_col].decode("utf-8", errors="ignore"))


def store_name_bound(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.name if alias.asname is None else alias.asname) == "Store":
                    return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "giftia.store" and alias.asname in (None, "store"):
                    return True
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "Store":
                    return True
    return False


def import_insert_offset(lines: list[str]) -> int:
    """插到最后一个顶层 import 之后。"""
    last = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if line[:1] not in (" ", "\t") and (
            stripped.startswith("import ") or stripped.startswith("from ")
        ):
            last = i
    return last + 1 if last >= 0 else 0


def migrate(path: Path) -> tuple[int, bool]:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        print(f"  !! {path.name}: 语法错误，跳过（{exc}）")
        return 0, False
    lines = src.split("\n")
    insertions: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "active_memories"):
            continue
        if any(kw.arg == "view" for kw in node.keywords) or len(node.args) >= 2:
            continue
        if (path.name, node.lineno) in EXCLUDE:
            continue
        ends = [(a.end_lineno, a.end_col_offset) for a in node.args]
        ends += [(kw.value.end_lineno, kw.value.end_col_offset) for kw in node.keywords]
        line_no, byte_col = max(ends) if ends else (func.end_lineno, func.end_col_offset)
        insertions.append((line_no, _char_col(lines[line_no - 1], byte_col)))
    if not insertions:
        return 0, False
    for line_no, col in sorted(insertions, reverse=True):
        line = lines[line_no - 1]
        lines[line_no - 1] = line[:col] + INSERT + line[col:]
    added_import = False
    if not store_name_bound(tree):
        lines.insert(import_insert_offset(lines), "from giftia.store import Store  # noqa: E402")
        added_import = True
    path.write_text("\n".join(lines), encoding="utf-8", newline="")
    return len(insertions), added_import


def main() -> int:
    total = touched = 0
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
