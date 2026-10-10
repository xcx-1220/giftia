"""Stream D（行为引擎）探针公共装置。

只读产品代码、只写 .review/tmp/behavior-*。不使用 tempfile.mkdtemp 默认行为
（沙箱会拒 0o700 目录），一律用显式路径 + os.makedirs。
"""

from __future__ import annotations

import dataclasses
import shutil
import sys
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
TMP_ROOT = ROOT / ".review" / "tmp" / "behavior-checker"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from giftia.brain import Giftia  # noqa: E402
from giftia.config import Settings, load_settings  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402

PASS = 0
FAIL = 0
NOTES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {label}" + (f"  → {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  → {detail}" if detail else ""))
    return bool(condition)


def note(text: str) -> None:
    NOTES.append(text)
    print(f"  [NOTE] {text}")


def fresh_dir(name: str) -> Path:
    """每个探针用独立、显式、可写的数据目录。"""
    p = TMP_ROOT / name
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)
    p.mkdir(parents=True, exist_ok=True)
    (p / "data").mkdir(parents=True, exist_ok=True)
    return p


def base_settings(name: str, hours: float = 81920.0, **overrides) -> Settings:
    tmp = fresh_dir(name)
    base = load_settings(lifespan_override=hours, force_mock=True)
    return dataclasses.replace(base, root=tmp, **overrides)


class CaptureLLM(MockLLM):
    """记下每一轮真正发出去的 messages，用来检查提示词里到底有什么。"""

    def __init__(self, model):
        super().__init__(model)
        self.seen: list[list[dict]] = []

    def chat(self, messages, **kw):  # type: ignore[override]
        self.seen.append([dict(m) for m in messages])
        return super().chat(messages, **kw)


def make_brain(settings: Settings, llm=None) -> Giftia:
    return Giftia(settings, llm=llm or MockLLM(settings.model))


def systems(llm: CaptureLLM) -> str:
    return "\n\n".join(
        str(m.get("content", "")) for call in llm.seen for m in call if m.get("role") == "system"
    )


def report(title: str) -> int:
    print()
    print(f"══ {title}：PASS={PASS} FAIL={FAIL} ══")
    return 1 if FAIL else 0
