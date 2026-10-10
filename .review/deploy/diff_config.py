"""逐键比对：服务器 config vs 本地仓库 config（比"解析后的值"，不是比文本）。

用法：python .review/deploy/diff_config.py <ssh目标>
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
TARGET = sys.argv[1] if len(sys.argv) > 1 else "ubuntu@150.158.75.223"
APP = "/opt/giftia"

PAIRS = [("settings.toml", "config/settings.toml"), ("persona.toml", "config/persona.toml")]


def flatten(data: dict, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in data.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(value, f"{name}."))
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            for i, item in enumerate(value):
                out.update(flatten(item, f"{name}[{i}]."))
        else:
            out[name] = value
    return out


def main() -> int:
    remote_script = (
        "import json,tomllib;"
        "d={};"
        "[d.update({p: tomllib.load(open(f'/opt/giftia/config/{p}','rb'))}) "
        "for p in ('settings.toml','persona.toml')];"
        "print(json.dumps(d))"
    )
    # config/*.toml 在服务器上是 600（giftia 属主），借 sudo 读
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", TARGET,
         f"sudo python3 -c \"{remote_script}\""],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        print("ssh/解析失败：", proc.stderr[:400])
        return 2
    remote = json.loads(proc.stdout.strip().splitlines()[-1])

    total_diff = 0
    for name, rel in PAIRS:
        local_raw = tomllib.loads((ROOT / rel).read_text(encoding="utf-8"))
        rflat = flatten(remote[name])
        lflat = flatten(local_raw)
        only_local = {k: v for k, v in lflat.items() if k not in rflat}
        only_remote = {k: v for k, v in rflat.items() if k not in lflat}
        differs = {k: (lflat[k], rflat[k]) for k in lflat if k in rflat and lflat[k] != rflat[k]}
        print(f"\n=== {name} ===")
        print(f"  仓库键数 {len(lflat)} / 服务器键数 {len(rflat)}")
        print(f"  值不同的键：{len(differs)}")
        for k, (a, b) in list(differs.items())[:20]:
            print(f"     {k}: 仓库={a!r}  服务器={b!r}")
        print(f"  只在仓库里（服务器没有 → 走代码默认值）：{len(only_local)}")
        for k in list(only_local)[:20]:
            print(f"     缺 {k} = {lflat[k]!r}")
        print(f"  只在服务器里（她多出来的东西）：{len(only_remote)}")
        for k in list(only_remote)[:20]:
            print(f"     多 {k} = {rflat[k]!r}")
        total_diff += len(differs) + len(only_local) + len(only_remote)

    print(f"\n合计差异：{total_diff}")
    if total_diff == 0:
        print("结论：服务器 config 与仓库 config **语义完全一致**（文本可能不同，值相同）")
    return 0 if total_diff == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
