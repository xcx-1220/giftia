"""Giftia 验收测试总入口。

在 DSH 沙箱下直接跑 `python tools/selftest.py` 会因为 CPython 的 `mkdtemp()` 用 0o700
建目录而 `PermissionError: [WinError 5]`（沙箱不给新目录继承 ACE）。这里统一：

  1. 把临时根指到工作区内（`.review/tmp/runtime`）；
  2. 把 `tempfile.mkdtemp` 换成 0o777 版本；
  3. 逐个跑测试套件，抓真实输出、真实项数与耗时，落盘 `.review/evidence/<label>.log`
     并在 `.review/evidence/summary.json` 汇总。

用法：
    python .review/acceptance.py                 # 跑全部
    python .review/acceptance.py selftest audit  # 只跑指定几项
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMPROOT = ROOT / ".review" / "tmp" / "runtime"
EVIDENCE = ROOT / ".review" / "evidence"
TMPROOT.mkdir(parents=True, exist_ok=True)
EVIDENCE.mkdir(parents=True, exist_ok=True)
try:
    os.chmod(TMPROOT, 0o777)
except OSError:
    pass

SUITES: dict[str, tuple[list[str], str]] = {
    # label: (argv, 说明)
    "selftest": (["tools/selftest.py"], "端到端自测（离线，不需要 key）"),
    "ws_selftest": (["tools/ws_selftest.py"], "WebSocket 协议自测（本地假服务端）"),
    "qq_protocol_selftest": (["tools/qq_protocol_selftest.py"], "QQ 适配器协议自测（本地假网关）"),
    "audit": (["tools/audit.py"], "静态一致性审计（声明 ↔ 实现）"),
    "demo_life": (["tools/demo_life.py"], "一次看完她的一生（离线）"),
    "show_prompt": (["tools/show_prompt.py"], "打印这一轮实际注入的提示词"),
    "check_deploy": (["tools/check_deploy.py"], "部署后核对（迁移/记忆是否还在）"),
    "qq_dry_run": (["adapters/qq_official.py", "--dry-run"], "适配器假事件接线（不联网）"),
    "reconcile_live_help": (["tools/reconcile_live.py", "--help"], "手动整理工具的参数面"),
}

_COUNT_PATTERNS = [
    re.compile(r"通过\s*(\d+)\s*项[，,]\s*失败\s*(\d+)\s*项"),
    re.compile(r"(\d+)\s*项通过"),
    re.compile(r"通过\s*(\d+)"),
    re.compile(r"(\d+)\s*/\s*(\d+)"),
    re.compile(r"(\d+)\s*passed", re.I),
]


def parse_counts(text: str) -> dict:
    """从输出里抽真实项数（能抽到几个算几个，抽不到就留空）。"""
    out: dict[str, int] = {}
    total = 0
    for pat in _COUNT_PATTERNS:
        for m in pat.finditer(text):
            nums = [int(g) for g in m.groups() if g is not None]
            if not nums:
                continue
            total += sum(nums)
    if total:
        out["numbers"] = total
    out["pass_marks"] = len(re.findall(r"\[PASS\]", text)) + len(re.findall(r"^\s*✓", text, re.M))
    out["fail_marks"] = len(re.findall(r"\[FAIL\]", text)) + len(re.findall(r"^\s*✗", text, re.M))
    return out


def run_suite(label: str, argv: list[str]) -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["TEMP"] = str(TMPROOT)
    env["TMP"] = str(TMPROOT)
    env["GIFTIA_EVIDENCE_LABEL"] = label
    started = time.time()
    proc = subprocess.run(
        [sys.executable, str(ROOT / ".review" / "run_tests.py"), *argv],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
    )
    elapsed = time.time() - started
    log = EVIDENCE / f"{label}.log"
    log.write_text(
        f"$ python .review/run_tests.py {' '.join(argv)}\n"
        f"exit_code={proc.returncode}  elapsed={elapsed:.1f}s\n"
        f"{'-' * 70}\nSTDOUT\n{'-' * 70}\n{proc.stdout}\n"
        f"{'-' * 70}\nSTDERR\n{'-' * 70}\n{proc.stderr}\n",
        encoding="utf-8",
    )
    combined = proc.stdout + "\n" + proc.stderr
    return {
        "label": label,
        "argv": argv,
        "exit_code": proc.returncode,
        "elapsed_s": round(elapsed, 1),
        "counts": parse_counts(combined),
        "log": str(log.relative_to(ROOT)),
        "tail": "\n".join(combined.strip().splitlines()[-12:]),
    }


def main(argv: list[str]) -> int:
    labels = [a for a in argv if a in SUITES] or list(SUITES)
    results = []
    for label in labels:
        script, note = SUITES[label]
        print(f"\n{'=' * 72}\n[{label}] {note}\n  -> {' '.join(script)}\n{'=' * 72}", flush=True)
        try:
            res = run_suite(label, script)
        except subprocess.TimeoutExpired:
            res = {"label": label, "argv": script, "exit_code": None, "elapsed_s": 1800.0,
                   "counts": {}, "log": "", "tail": "TIMEOUT"}
        results.append(res)
        print(f"  exit={res['exit_code']}  {res['elapsed_s']}s  counts={res['counts']}")
        for line in res["tail"].splitlines()[-8:]:
            print(f"    | {line}")

    summary = EVIDENCE / "summary.json"
    summary.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n汇总 -> {summary}")
    bad = [r["label"] for r in results if r["exit_code"] != 0]
    print("失败的套件：" + (", ".join(bad) if bad else "无"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
