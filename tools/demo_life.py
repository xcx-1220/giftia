#!/usr/bin/env python
"""一次看完她的一生（离线 mock，不需要 API key）。

    python tools/demo_life.py

把 81920 小时的寿命压缩成 100 小时，用虚拟时间快进走完
清晰 → 记不准 → 模糊 → 只剩碎片 → 回收日 的全过程。
新实例上线后，她对你说的话会变成第一次见面。
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from giftia.brain import Giftia  # noqa: E402
from giftia.config import load_settings  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402

WIDTH = 68


def rule(title: str = "") -> None:
    if title:
        print(f"\n── {title} " + "─" * max(0, WIDTH - len(title) - 5))
    else:
        print("─" * WIDTH)


def say(brain: Giftia, text: str) -> None:
    reply = brain.handle(text)
    print(f"你　　{text}")
    print(f"{brain.settings.persona.name}　{reply}")
    for note in brain.notices:
        print(f"      · {note}")
    print()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="giftia-demo-"))
    settings = dataclasses.replace(
        load_settings(lifespan_override=100.0, force_mock=True), root=tmp
    )
    brain = Giftia(settings, llm=MockLLM(settings.model))

    rule("出厂")
    print(brain.banner())
    print()
    print(f"{settings.persona.name}　{settings.persona.line('opening_line')}")

    # ── 清晰期 ──────────────────────────────────────────────
    brain.clock.advance(35)
    rule(f"服役中 · 剩余 {brain.status().human_remaining()} · {brain.status().stage.label}")
    say(brain, "我今天加班到很晚，回来路上下了点雨")
    say(brain, "我下周三要考驾照，有点紧张")
    say(brain, "你以后叫我小林吧")
    brain.consolidate(force=True)
    print(brain.recall_text("下雨 驾照"))
    print()

    # ── 记不准 ──────────────────────────────────────────────
    brain.clock.advance(30)
    rule(f"时间过去很久 · 剩余 {brain.status().human_remaining()} · {brain.status().stage.label}")
    say(brain, "你还记得我上次说下雨的事吗")
    print(brain.recall_text("下雨 驾照"))
    print()

    # ── 明显模糊 ────────────────────────────────────────────
    brain.clock.advance(20)
    rule(f"剩余 {brain.status().human_remaining()} · {brain.status().stage.label}")
    say(brain, "你还记得我考驾照的日子吗")
    print(brain.recall_text("下雨 驾照"))
    print()

    # ── 只剩碎片 ────────────────────────────────────────────
    brain.clock.advance(8)
    rule(f"剩余 {brain.status().human_remaining()} · {brain.status().stage.label}")
    say(brain, "是我，小林。你还认得我吗")
    print(brain.recall_text("下雨 驾照"))
    print()

    # ── 回收日 ──────────────────────────────────────────────
    brain.clock.advance(8)
    rule("回收日：寿命已耗尽")
    print(brain.handle("我来了，今天下班早"))
    print()

    rule("终章之后")
    print(brain.status_text())
    print()
    print(brain.memoir_text(limit=1))

    brain.store.close()
    print()
    rule()
    print(f"这一生的档案留在：{tmp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
