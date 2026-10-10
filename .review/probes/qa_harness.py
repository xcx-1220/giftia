"""Stream E（qa-verifier）的独立测试装置。

外部验收视角：不改产品代码，只从外部驱动 giftia.brain.Giftia。
- 自己的临时目录一律在 .review/tmp/qa-* 下（显式 mkdir，默认 0o777，绕开沙箱 0o700 坑）
- 一律 MockLLM（禁止真 API）；RecordingLLM 包一层，记录**真的送进 llm 的 messages**
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import random
import shutil
import string
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from giftia.brain import Giftia  # noqa: E402
from giftia.config import Settings, load_settings  # noqa: E402
from giftia.llm import ChatReply, LLMError, MockLLM  # noqa: E402
from giftia.store import Store  # noqa: E402

TMP_ROOT = ROOT / ".review" / "tmp"


def make_dir(tag: str) -> Path:
    """显式 0o777 目录（tempfile.mkdtemp 的 0o700 会被沙箱拒绝）。"""
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    name = "qa-" + tag + "-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    p = TMP_ROOT / name
    os.makedirs(p, exist_ok=True)
    try:
        os.chmod(p, 0o777)
    except OSError:
        pass
    return p


def rm_dir(p: Path) -> None:
    try:
        shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


def make_settings(tmp: Path, *, hours: float | None = None, **replace) -> Settings:
    base = load_settings(lifespan_override=hours, force_mock=True)
    base = dataclasses.replace(base, root=tmp)
    mem = base.memory
    if "consolidate_every" in replace:
        mem = dataclasses.replace(mem, consolidate_every=replace.pop("consolidate_every"))
    if "tools_max_rounds" in replace:
        mem = dataclasses.replace(mem, tools_max_rounds=replace.pop("tools_max_rounds"))
    if "session_window" in replace:
        mem = dataclasses.replace(mem, session_window=replace.pop("session_window"))
    base = dataclasses.replace(base, memory=mem)
    if "outreach" in replace:
        base = dataclasses.replace(base, outreach=replace.pop("outreach"))
    if "runtime" in replace:
        base = dataclasses.replace(base, runtime=replace.pop("runtime"))
    if "dream" in replace:
        base = dataclasses.replace(base, dream=replace.pop("dream"))
    if "occasion" in replace:
        base = dataclasses.replace(base, occasions=replace.pop("occasion"))
    if replace:
        raise TypeError(f"unknown replace keys: {sorted(replace)}")
    return base


def make_brain(settings: Settings, llm=None) -> Giftia:
    return Giftia(settings, llm=llm or RecordingLLM(settings.model))


# ── 记录型 LLM：能看到"真的送进模型的 messages" ────────────────────
class RecordingLLM(MockLLM):
    def __init__(self, cfg=None, *, fail_chat: int = 0, fail_extract: int = 0,
                 bad_json_extract: bool = False, empty_reply: bool = False):
        super().__init__(cfg)
        self.calls: list[dict] = []
        self.fail_chat = fail_chat          # 前 N 次 chat 抛 LLMError
        self.fail_extract = fail_extract    # 前 N 次 extract 抛 LLMError
        self.bad_json_extract = bad_json_extract
        self.empty_reply = empty_reply
        self.chat_count = 0
        self.extract_count = 0

    # 记录 + 转发
    def chat(self, messages, **kw):
        self.chat_count += 1
        self.calls.append({"kind": "chat", "messages": copy.deepcopy(messages),
                           "kw": dict(kw), "t": time.perf_counter()})
        if self.chat_count <= self.fail_chat:
            raise LLMError("模拟故障：连接被重置(测试注入)")
        if self.empty_reply:
            raise LLMError("模型返回了空内容(测试注入)")
        return super().chat(messages, **kw)

    def chat_with_tools(self, messages, *, tools=None, temperature=None, max_tokens=None):
        self.calls.append({"kind": "chat_with_tools", "messages": copy.deepcopy(messages),
                           "tools": copy.deepcopy(tools) if tools else None,
                           "t": time.perf_counter()})
        self.chat_count += 1
        if self.chat_count <= self.fail_chat:
            raise LLMError("模拟故障：连接被重置(测试注入)")
        return ChatReply(text=super().chat(messages, temperature=temperature,
                                           max_tokens=max_tokens))

    def extract_memories(self, transcript, *, name="Giftia", today="", scene="c2c", known=""):
        self.extract_count += 1
        self.calls.append({"kind": "extract", "transcript": transcript, "scene": scene,
                           "today": today, "t": time.perf_counter()})
        if self.extract_count <= self.fail_extract:
            raise LLMError("模拟整理失败(测试注入)")
        if self.bad_json_extract:
            raise LLMError("整理返回非法 JSON(测试注入)")
        return super().extract_memories(transcript, name=name, today=today, scene=scene, known=known)

    # 便捷查询
    def systems(self) -> list[str]:
        return [m["content"] for c in self.calls for m in c.get("messages", [])
                if m.get("role") == "system"]

    def last_system(self, kind: str = "chat") -> str:
        """默认取"她说话那一轮"的 system（chat 或 chat_with_tools）。"""
        kinds = ("chat", "chat_with_tools") if kind == "chat" else (kind,)
        for c in reversed(self.calls):
            if c["kind"] in kinds:
                for m in c["messages"]:
                    if m.get("role") == "system":
                        return str(m["content"])
        return ""

    def all_text(self) -> str:
        """所有送给模型的内容（含整理稿）拼一起，用于隐私 grep。"""
        buf: list[str] = []
        for c in self.calls:
            if c["kind"] == "extract":
                buf.append(str(c["transcript"]))
            else:
                for m in c.get("messages", []):
                    buf.append(str(m.get("content", "")))
                if c.get("tools"):
                    buf.append(json.dumps(c["tools"], ensure_ascii=False))
        return "\n".join(buf)

    def sent_payloads(self) -> list[str]:
        """只算"她说话这一轮"的 messages（chat / chat_with_tools），不含整理稿。"""
        buf: list[str] = []
        for c in self.calls:
            if c["kind"] in ("chat", "chat_with_tools"):
                for m in c["messages"]:
                    buf.append(str(m.get("content", "")))
        return buf


# ── 断言小工具 ────────────────────────────────────────────────────
class Checker:
    def __init__(self, title: str):
        self.title = title
        self.pass_ = 0
        self.fail_ = 0
        self.fails: list[str] = []
        print("=" * 78)
        print(title)
        print("=" * 78)

    def check(self, label: str, cond: bool, detail: str = "") -> bool:
        if cond:
            self.pass_ += 1
            print(f"  [PASS] {label}")
        else:
            self.fail_ += 1
            self.fails.append(label)
            print(f"  [FAIL] {label}" + (f"  → {detail}" if detail else ""))
        return bool(cond)

    def info(self, msg: str) -> None:
        print(f"  [INFO] {msg}")

    def summary(self) -> int:
        print("-" * 78)
        print(f"通过 {self.pass_} 项，失败 {self.fail_} 项")
        for f in self.fails:
            print(f"  FAILED: {f}")
        return 1 if self.fail_ else 0
