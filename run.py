#!/usr/bin/env python
"""Giftia 启动入口。

    python run.py                  正常聊天（需要 DEEPSEEK_API_KEY）
    python run.py --mock           不需要 API key，先看机器跑不跑得通
    python run.py --lifespan-hours 48   把寿命压成 48 小时，配合 /time +6 看完整一生
    python run.py --once "在吗"     单条模式：只打印她的回复，方便写进适配器
    python run.py --reset --yes    清空所有数据，从第一任重新开始
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from giftia.brain import Giftia, make_llm  # noqa: E402
from giftia.cli import run_cli  # noqa: E402
from giftia.config import DATA_DIR, load_settings  # noqa: E402
from giftia.llm import LLMError  # noqa: E402


def _fix_console_encoding() -> None:
    """Windows 管道/重定向下中文和进度条字符可能编不出来，兜一层。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Giftia —— 有寿命、会遗忘的陪伴型对话内核",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--mock", action="store_true", help="离线模式，不需要 API key")
    p.add_argument(
        "--lifespan-hours",
        type=float,
        default=0.0,
        metavar="N",
        help="覆盖这一任的寿命（小时），用于快速演示退化与回收",
    )
    p.add_argument("--once", metavar="TEXT", help="只发一条消息并打印回复")
    p.add_argument("--user", default=None, metavar="ID", help="发送者 ID（适配器用）")
    p.add_argument("--no-banner", action="store_true", help="不打印开场信息")
    p.add_argument("--reset", action="store_true", help="清空数据库与档案，重新开始")
    p.add_argument("--yes", action="store_true", help="配合 --reset 跳过确认")
    return p


def main(argv: list[str] | None = None) -> int:
    _fix_console_encoding()
    args = build_parser().parse_args(argv)

    if args.reset:
        if not args.yes:
            answer = input(f"这会删除 {DATA_DIR} 下的全部记忆与档案，确定？(yes/N) ")
            if answer.strip().lower() not in ("y", "yes"):
                print("已取消。")
                return 0
        from giftia.store import Store

        store = Store(load_settings().db_path)
        store.wipe()
        store.close()
        print("数据已清空。下一次启动将是全新的第一任。")

    settings = load_settings(
        lifespan_override=args.lifespan_hours or None, force_mock=args.mock
    )

    try:
        llm = make_llm(settings)
    except LLMError as exc:
        print(f"[启动失败] {exc}", file=sys.stderr)
        print("提示：加 --mock 可以先离线跑通整套机制。", file=sys.stderr)
        return 2

    brain = Giftia(settings, llm=llm)

    if args.once is not None:
        try:
            print(brain.handle(args.once, args.user or settings.runtime.user_id))
            for note in brain.notices:
                print(f"  {note}", file=sys.stderr)
        finally:
            brain.close()
        return 0

    return run_cli(brain, banner=not args.no_banner)


if __name__ == "__main__":
    raise SystemExit(main())
