"""核对服务器上的产品代码与本地 HEAD 是否逐文件一致（只读）。

用法：python .review/deploy/verify_parity.py <服务器 ssh 目标>
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
TARGET = sys.argv[1] if len(sys.argv) > 1 else "ubuntu@150.158.75.223"
APP = "/opt/giftia"

# 只比产品代码（部署时真正会被执行的），不比 .review/报告
PATTERNS = ("run.py", "giftia/*.py", "adapters/*.py", "tools/*.py", "config/settings.toml", "config/persona.toml")


def files() -> list[str]:
    out: list[str] = []
    for pat in PATTERNS:
        for p in sorted(ROOT.glob(pat)):
            if p.is_file() and "__pycache__" not in p.parts:
                out.append(p.relative_to(ROOT).as_posix())
    return out


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    names = files()
    print(f"本地待比对文件：{len(names)} 个")
    # config/*.toml 在服务器上是 600（giftia 属主），要借 sudo 读
    remote_cmd = f"cd {APP} && sudo sha256sum " + " ".join(names)
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", TARGET, remote_cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        print("ssh 失败：", proc.stderr[:300])
        return 2
    remote: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            remote[parts[1]] = parts[0]

    same = diff = missing = 0
    problems: list[str] = []
    for name in names:
        if name not in remote:
            missing += 1
            problems.append(f"  服务器缺少   {name}")
            continue
        if sha(ROOT / name) == remote[name]:
            same += 1
        else:
            diff += 1
            problems.append(f"  内容不同     {name}")
    print(f"一致 {same} / 不同 {diff} / 缺失 {missing}")
    for p in problems[:20]:
        print(p)
    if not problems:
        print("\n同步 ✓ 服务器上的产品代码与本地 HEAD 完全一致")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
