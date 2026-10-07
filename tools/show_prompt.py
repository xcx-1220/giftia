#!/usr/bin/env python
"""把她这一轮**实际被注入了什么**原样打印出来（离线，不调用 API、不写库）。

    python tools/show_prompt.py

回答的是这个问题：「人格到底有没有被注入？注入的到底是什么？」
你会看到一条 system 消息，结构固定为五块：

    1. 人格内核（来自 config/persona.toml，每轮原样发送 → 稳定命中缓存）
    2. 出戏处理提示
    3. 此刻状态（虚拟时间 / 剩余寿命 / 记忆阶段）
    4. 关于记忆（当前阶段她该怎么对待自己的记忆）
    5. 你现在想起的事（带置信度标记，且只有模糊之后的版本）
    6. 本次对话的规则

它会跑两遍：先是清晰期，再快进到终端服务迫近，让你看到**同一个她、同一套人格**，
只有记忆那两块随寿命变化。
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

WIDTH = 72


def rule(title: str = "") -> None:
    if title:
        print(f"\n{'═' * 3} {title} " + "═" * max(0, WIDTH - len(title) - 5))
    else:
        print("═" * WIDTH)


def show(brain: Giftia, text: str, title: str) -> None:
    rule(title)
    print(brain.preview_prompt(text))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="giftia-prompt-"))
    settings = dataclasses.replace(
        load_settings(lifespan_override=100.0, force_mock=True), root=tmp
    )
    brain = Giftia(settings, llm=MockLLM(settings.model))

    print(f"她的名字：{settings.persona.name}　型号：{settings.persona.model_code}")
    print(f"人格文件：{settings.root / 'config' / 'persona.toml'}")
    print(f"（本次演示用临时数据目录，正式数据不受影响）")

    # 聊几句，攒出记忆
    for u in ["以后你就叫我小林吧，别叫搭档了", "我下周三要考驾照，有点紧张"]:
        brain.handle(u)
    brain.consolidate(force=True)

    show(brain, "你还记得我要考什么吗", "第一遍：记忆清晰期（剩余寿命充裕）")
    _ = brain.handle("你还记得我要考什么吗")

    # 快进到生命尽头，看同一个人的变化
    brain.clock.advance(99)
    show(brain, "你还记得我要考什么吗", "第二遍：终端服务迫近（剩余 1%）")

    rule("对比")
    print("人格内核那两块**一字未变**——她还是她。")
    print("变的只有「此刻状态」和「你现在想起的事」：")
    print("  · 剩余寿命从「4.2 天」变成「1.0 小时」")
    print("  · 内核记忆（你的名字/称呼）模糊度恒为 0，仍标 [可以确信]")
    print("  · 常规记忆（考驾照）被擦掉大半，降级成 [需要含糊] 或 [只是隐约觉得]")
    print()
    print("想看她对同一句话的真实回答，用 python run.py；想核对注入，用 /prompt。")

    brain.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
