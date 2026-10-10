"""Lead 探针 D5：README 第 5 节说"人设固定 → 命中缓存，那部分几乎免费"。

DeepSeek 的上下文缓存是**前缀缓存**：命中长度取决于两次请求 public prefix 的最长公共前缀。
README 宣称人格内核"永不变，稳定命中缓存"。本探针直接量 system 提示词的最长公共前缀（LCP），
看"几乎免费"的那部分到底有多少字符，以及哪些字段在破坏前缀。
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
sys.path.insert(0, str(ROOT))
for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from giftia.brain import Giftia  # noqa: E402
from giftia.config import load_settings  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402

TMP = Path(r"D:\ai chatbot\.review\tmp\lead-prefix")
TMP.mkdir(parents=True, exist_ok=True)

base = load_settings(lifespan_override=81920.0, force_mock=True)
settings = dataclasses.replace(base, root=TMP)

captured: list[str] = []


class RecordingLLM(MockLLM):
    def chat(self, messages, **kw):  # type: ignore[override]
        captured.append(str(messages[0]["content"]))
        return super().chat(messages, **kw)


brain = Giftia(settings, llm=RecordingLLM(settings.model))
turns = [
    "在吗",
    "我叫小林",
    "我今天加班到很晚，回来路上下了点雨",
    "你还记得我昨天说了什么吗",
    "我下周三要考驾照，有点紧张",
    "在吗",
    "有点累了",
    "你还记得我要考什么吗",
]
for t in turns:
    brain.handle(t)

print(f"共捕获 {len(captured)} 次 system 提示词\n")


def lcp(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


print(f"{'轮次':<6}{'system 长度':>12}{'与前一轮的公共前缀':>20}{'占比':>8}")
for i, sysprompt in enumerate(captured):
    if i == 0:
        print(f"{i + 1:<6}{len(sysprompt):>12}{'—':>20}{'—':>8}")
        continue
    n = lcp(captured[i - 1], sysprompt)
    print(f"{i + 1:<6}{len(sysprompt):>12}{n:>20}{n / max(1, len(sysprompt)):>7.1%}")

prefix = lcp(captured[0], captured[-1])
print(f"\n第 1 轮与最后一轮的公共前缀 = {prefix} 字符 "
      f"（第 1 轮总长 {len(captured[0])}，最后一轮总长 {len(captured[-1])}）")

# 找出第一处分叉点上下文 —— 那就是"破坏缓存"的位置
n = lcp(captured[0], captured[-1])
print("\n── 首处分叉点前 90 字 / 后 90 字 ──")
print(f"  前：{captured[0][max(0, n - 90):n]!r}")
print(f"  后：{captured[-1][n:n + 90]!r}")

# 量一下"人格内核"本身有多大 —— 这是理论上永不变化的部分
core = settings.persona.core_text("小林").strip()
print(f"\n人格内核（persona.toml 的 core）长度 = {len(core)} 字符")
print(f"  → 若缓存只覆盖到人格内核，那么命中 {len(core)} 字符左右；实测公共前缀 {prefix} 字符")
