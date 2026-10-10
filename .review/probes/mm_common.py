"""Stream A（memory-man）探针公共装置。

只用标准库 + 项目自己的模块。临时数据根固定在工作区内
（`D:\\ai chatbot\\.review\\tmp\\memory-man-*`），系统 TEMP 不可写。
不修改任何产品代码；需要构造"老库/坏库"时直接对临时库执行 SQL。
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
TMPROOT = ROOT / ".review" / "tmp"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from giftia.brain import Giftia  # noqa: E402
from giftia.clock import Clock  # noqa: E402
from giftia.config import Stage, load_settings  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402
from giftia.store import Store  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"


def fresh_dir(tag: str) -> Path:
    """工作区内的临时目录（默认 mode 0o777，沙箱可访问）。"""
    p = TMPROOT / f"memory-man-{tag}"
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)
    os.makedirs(p, exist_ok=True)
    return p


def settings_for(tmp: Path, **overrides):
    """读真实 settings.toml，但把数据根换到 tmp（不碰 data/giftia.db）。"""
    s = load_settings()
    s = dataclasses.replace(s, root=tmp)
    for key, value in overrides.items():
        section, _, name = key.partition("__")
        if not name:
            raise ValueError(f"override 要写成 memory__top_k 这种形式：{key}")
        cur = getattr(s, section)
        if not hasattr(cur, name):
            raise AttributeError(f"{section} 没有字段 {name}")
        if name == "stages":
            s = dataclasses.replace(s, **{section: value})
        else:
            setattr_ok = dataclasses.replace(cur, **{name: value})
            s = dataclasses.replace(s, **{section: setattr_ok})
    return s


def make_brain(tmp: Path, *, settings=None, clock_offset: float = 0.0, mock=None) -> Giftia:
    s = settings or settings_for(tmp)
    store = Store(s.db_path)
    clock = Clock(store, offset_hours=clock_offset)
    return Giftia(s, llm=mock or MockLLM(s.model), store=store, clock=clock)


def stage(settings, name: str) -> Stage:
    for st in settings.stages:
        if st.name == name:
            return st
    raise KeyError(name)


def mem(
    id_: int,
    summary: str,
    *,
    layer: str = "normal",
    importance: float = 0.5,
    strength: float = 1.0,
    recall_count: int = 0,
    last_recalled_at: str | None = None,
    created_ts: str = "2026-10-08T12:00:00",
    happened_ts: str = "2026-10-08T12:00:00",
    about: str = "",
    tags: str = "",
    emotion: str = "",
    event_date: str | None = None,
    scope: str = "private",
    subject_id: str = "",
    sealed: int = 0,
    merged_into: int = 0,
    forgotten: int = 0,
) -> dict:
    """手工造一条记忆 dict（用于纯函数探针，不落库）。"""
    return {
        "id": id_,
        "instance_no": 1,
        "created_ts": created_ts,
        "happened_ts": happened_ts,
        "summary": summary,
        "tags": tags,
        "emotion": emotion,
        "importance": importance,
        "source_session": "probe",
        "sealed": sealed,
        "layer": layer,
        "strength": strength,
        "recall_count": recall_count,
        "last_recalled_at": last_recalled_at,
        "injected_at": None,
        "event_date": event_date,
        "scope": scope,
        "subject_id": subject_id,
        "speaker_id": "",
        "about": about,
        "merged_into": merged_into,
        "forgotten": forgotten,
    }


def db_memories(store: Store) -> list[dict]:
    return store._rows("SELECT * FROM memories ORDER BY id ASC")


def title(text: str) -> None:
    print()
    print("=" * 78)
    print(text)
    print("=" * 78)


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"[{PASS if ok else FAIL}] {label}" + (f"  → {detail}" if detail else ""))
    return ok


def now_naive() -> datetime:
    return datetime.now()


def tz_aware_stamp(dt: datetime | None = None) -> str:
    base = dt or datetime.now(timezone.utc).astimezone()
    return base.isoformat(timespec="seconds")


def days_ago(days: float) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
