#!/usr/bin/env python3
"""她把全部身家放在一个 SQLite 文件里 —— 这个脚本给它做副本。

用 SQLite 自己的 `.backup()`（在线热备），**不会锁库**，她说话不受影响。
备份文件落在 data/backups/，按日期命名，只留最近 N 份。

为什么不用 `cp`：她可能在写的时候被复制，得到的是**半截数据库**（SQLite 官方的
建议就是不要直接 cp 正在用的库）。`.backup()` 会处理页锁和回滚日志。

跑法（systemd timer 每天一次）：
    python3 deploy/backup_db.py
手动验证：
    python3 deploy/backup_db.py --list      # 看现有备份
    python3 deploy/backup_db.py --check     # 逐个打开、校验完整性、对条数
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

KEEP = 14  # 留最近多少份

APP_DIR = Path(__file__).resolve().parent.parent
DB = APP_DIR / "data" / "giftia.db"
DEST = APP_DIR / "data" / "backups"


def _log(msg: str) -> None:
    print(f"[backup] {msg}", flush=True)


def backup(*, keep: int = KEEP) -> Path | None:
    if not DB.is_file():
        _log(f"找不到 {DB}")
        return None
    DEST.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    out = DEST / f"giftia-{stamp}.db"

    # 同一天跑两次就覆盖 —— 每天一份，不是每次一份
    tmp = out.with_suffix(".db.tmp")
    src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)          # ★ 在线热备：源库不用停，也不锁她
        finally:
            dst.close()
    finally:
        src.close()
    tmp.replace(out)

    size = out.stat().st_size
    _log(f"已备份 → {out.name}（{size / 1024:.0f} KB）")

    # 轮转：只留最近 KEEP 份
    olds = sorted(DEST.glob("giftia-*.db"))
    for old in olds[:-keep]:
        old.unlink()
        _log(f"删掉旧备份 {old.name}")

    _log(f"现在共 {len(olds[-keep:])} 份")
    return out


def _open_and_count(path: Path) -> tuple[int, int, str]:
    """返回 (消息数, 记忆数, 完整性)"""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        msgs = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        mems = con.execute(
            "SELECT COUNT(*) FROM memories WHERE archived_at IS NULL"
        ).fetchone()[0]
        return msgs, mems, ok
    finally:
        con.close()


def check(*, keep: int = KEEP) -> int:
    files = sorted(DEST.glob("giftia-*.db"))
    if not files:
        _log("还没有任何备份")
        return 1
    bad = 0
    for f in files[-keep:]:
        try:
            msgs, mems, ok = _open_and_count(f)
            flag = "✓" if ok == "ok" else "✗"
            if ok != "ok":
                bad += 1
            _log(f"{flag} {f.name}  {f.stat().st_size / 1024:6.0f} KB  "
                 f"消息 {msgs:4d}  记忆 {mems:4d}  完整性 {ok}")
        except Exception as exc:
            bad += 1
            _log(f"✗ {f.name}  打不开：{type(exc).__name__}: {exc}")
    _log(f"共 {len(files)} 份，坏的 {bad} 份")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="给她备份 SQLite 数据库")
    ap.add_argument("--list", action="store_true", help="列出已有备份")
    ap.add_argument("--check", action="store_true", help="逐个校验备份能否打开")
    ap.add_argument("--keep", type=int, default=KEEP, help=f"留几份（默认 {KEEP}）")
    args = ap.parse_args()

    if args.list:
        files = sorted(DEST.glob("giftia-*.db"))
        for f in files:
            _log(f"{f.name}  {f.stat().st_size / 1024:.0f} KB")
        _log(f"共 {len(files)} 份（目录 {DEST}）")
        return 0
    if args.check:
        return check(keep=args.keep)

    if backup(keep=args.keep) is None:
        return 1
    # 备份完立刻校验一次：**没验过的备份不算备份**
    return check(keep=args.keep)


if __name__ == "__main__":
    raise SystemExit(main())
