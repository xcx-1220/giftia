"""Launch a project script under a sandbox-compatible temp policy.

Why: on this host the DSH file sandbox denies access to directories created with
mode 0o700 (which is exactly what CPython's ``tempfile.mkdtemp`` uses on Windows).
Any test script that calls ``mkdtemp()`` therefore dies with
``PermissionError: [WinError 5]``.  This launcher (a) points the temp root into
the workspace and (b) re-implements ``mkdtemp`` with mode 0o777 so the created
directories stay accessible.  Product code is not modified.

Usage:
    python .review/run_tests.py tools/selftest.py
    python .review/run_tests.py tools/ws_selftest.py
    TMPROOT=<dir> python .review/run_tests.py tools/demo_life.py
"""

from __future__ import annotations

import os
import runpy
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
TMPROOT = Path(os.environ.get("TMPROOT") or (ROOT / ".review" / "tmp" / "runtime"))
TMPROOT.mkdir(parents=True, exist_ok=True)
try:  # make sure the sandbox SID has access to the shared temp root
    os.chmod(TMPROOT, 0o777)
except OSError:
    pass

tempfile.tempdir = str(TMPROOT)


def _mkdtemp(suffix: str = "", prefix: str = "tmp", dir: str | None = None) -> str:
    """mkdtemp() with a permissive mode so the sandbox keeps access."""
    directory = dir or tempfile.tempdir or TMPROOT
    directory = os.fspath(directory)
    for _ in range(10000):
        candidate = os.path.join(directory, f"{prefix}{next(tempfile._get_candidate_names())}{suffix}")
        try:
            os.mkdir(candidate, 0o777)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError("no usable temporary directory name found")


tempfile.mkdtemp = _mkdtemp  # type: ignore[assignment]

if len(sys.argv) < 2:
    raise SystemExit(__doc__)

target = Path(sys.argv[1])
if not target.is_absolute():
    target = ROOT / target
sys.argv = [str(target), *sys.argv[2:]]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
runpy.run_path(str(target), run_name="__main__")
