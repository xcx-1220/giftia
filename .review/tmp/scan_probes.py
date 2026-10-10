"""扫描 .review/probes/*.py 里不满足新契约的调用点（team-store 报告用）。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
probes = sorted((ROOT / ".review" / "probes").glob("*.py"))
pat = re.compile(r"active_memories\(([^()]*(?:\([^()]*\)[^()]*)*)\)")
print("== active_memories 调用不带 view 的探针 ==")
total = 0
for p in probes:
    text = p.read_text(encoding="utf-8", errors="replace")
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        if "active_memories(" in line and "view" not in line:
            hits.append((i, line.strip()))
    if hits:
        total += len(hits)
        print(f"{p.name}: {len(hits)} 处")
        for i, line in hits:
            print(f"   {i}: {line[:100]}")
print(f"合计 {total} 处")

print()
print("== 替换 Store.meta_set / add_memory 等写方法签名的探针（新 kwarg commit 可能不兼容）==")
for p in probes:
    text = p.read_text(encoding="utf-8", errors="replace")
    for i, line in enumerate(text.splitlines(), 1):
        if re.search(r"def \w+\(.*\b(key|value|ids|memory_id)\b", line) and (
            "meta_set" in line or "state_set" in line or "add_memory" in line
            or "mark_consolidated" in line
        ):
            print(f"{p.name}:{i}: {line.strip()[:100]}")
        if re.search(r"(meta_set|state_set|add_memory|mark_consolidated)\s*=\s*", line):
            print(f"{p.name}:{i}: {line.strip()[:100]}")
