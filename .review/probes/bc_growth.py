#!/usr/bin/env python
"""Stream B 探针 7：无界增长 — 花名册进提示词 / people_stats 只写不读。

    python .review\\probes\\bc_growth.py
"""
from __future__ import annotations

import dataclasses
import os
import sys
import time
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from giftia.brain import Giftia  # noqa: E402
from giftia.config import load_settings  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402

PASS = FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  -> {detail}" if detail else ""))


class Rec(MockLLM):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.system = ""

    def chat(self, messages, **kw):
        self.system = str(messages[0]["content"])
        return super().chat(messages, **kw)

    def chat_with_tools(self, messages, *, tools=None, **kw):
        self.system = str(messages[0]["content"])
        return super().chat_with_tools(messages, tools=tools, **kw)


def env(name: str, hours: float = 81920.0):
    tag = f"{os.getpid()}-{int(time.time())}"
    d = ROOT / ".review" / "tmp" / f"brain-checker-{name}-{tag}"
    d.mkdir(parents=True, exist_ok=True)
    base = load_settings(lifespan_override=hours, force_mock=True)
    return dataclasses.replace(base, root=d)


print("== 1. 花名册（known_people）进群提示词的条数上限 ==")
h = Giftia(env("growth"), llm=Rec(env("growth").model))
h.set_handler_id("OWNER")
h.set_handler_name("椎名")
for i in range(300):
    h.remember_person(f"U{i:04d}", f"成员{i:03d}")
h.remember_person("OWNER", "椎名")
h.handle("群里的第一句话", user_id="OWNER", scene="group", group_id="GBIG",
         speaker_name="椎名", is_handler=True)
roster = [b for b in h.llm.system.split("\n\n") if b.startswith("【群里你认得的人】")]
roster_block = roster[0] if roster else ""
lines = [l for l in roster_block.splitlines() if l.startswith("- ")]
print(f"      认识 301 人 → 提示词里花名册 {len(lines)} 行 / {len(roster_block)} 字")
# ★ 期望翻转（lead 授权，2026-10-10）：原来这两条断言"花名册无上限、全部进提示词"（>2000 字），
#   那是 P1-12 的缺陷（认识的人越多，每轮 system 越长）。修后 persona.ROSTER_MAX=30 硬截断，
#   并且只带"本群见过的人 + 搭档"（这 300 人是 record 时没带 group_id 的，所以本群 GBIG 里
#   只有搭档一行——群隔离与上限同时生效）。正向对照：搭档永远在名单里。
check("1a ★ 花名册有条数上限（≤ ROSTER_MAX 行，不再 301 行全进）",
      0 < len(lines) <= 30, str(len(lines)))
check("1b 花名册不再吃掉上下文（≤ 300 字）", len(roster_block) <= 300, str(len(roster_block)))
check("1c 对照：搭档仍然在名单第一行（截断不会把搭档挤掉）",
      lines and "椎名" in lines[0], str(lines[:1]))
print(f"      整条 system 长度：{len(h.llm.system)} 字")

print("\n== 2. people_stats：每条群消息都全量重写，而且没人读 ==")
h2 = Giftia(env("stats"), llm=MockLLM(env("stats").model))
h2.set_handler_id("OWNER")
for i in range(1000):
    h2.note_activity(f"U{i:05d}")
raw = str(h2.store.state_get("people_stats", "") or "")
print(f"      1000 人的 people_stats 序列化大小：{len(raw)} 字")
t0 = time.perf_counter()
for i in range(200):
    h2.note_activity("U00001")
dt = (time.perf_counter() - t0) / 200 * 1000
print(f"      单次 note_activity（1000 人表）耗时：{dt:.3f} ms")
check("2a people_stats 随时增长（没有裁剪）", len(raw) > 20000, str(len(raw)))

import subprocess  # noqa: E402
grep = subprocess.run(
    [sys.executable, "-c",
     "import pathlib;"
     "print({str(p) for p in list(pathlib.Path('giftia').rglob('*.py')) + "
     "list(pathlib.Path('adapters').rglob('*.py')) "
     "if 'people_stats' in p.read_text(encoding='utf-8')})"],
    capture_output=True, text=True, cwd=str(ROOT),
)
files = grep.stdout.strip()
print(f"      引用 people_stats 的文件：{files}")
check("2b ★ people_stats 只在 brain.py 的定义/读写里出现，别的模块（含 persona.py）从不读它",
      files == "{'giftia\\\\brain.py'}", files)

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
