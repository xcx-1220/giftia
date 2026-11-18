"""Stage 1 acceptance, or a probe of an already running test service."""

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from mock_model import MODEL, REPLY
from stage1 import BUNDLE, GATEWAY_PORT, HARNESS_VERSION, HOST, PROFILE, SOURCE, Lab

PROMPT = "当前对话使用合成数据验证艾拉的 Harness profile。"
OPENER = build_opener(ProxyHandler({}))


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def http(path, payload=None, timeout=3, *, port=GATEWAY_PORT):
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(f"http://{HOST}:{port}/api/{path}", data=data,
                      headers={"Content-Type": "application/json"})
    with OPENER.open(request, timeout=timeout) as response:
        return json.load(response)


def ready(process=None, timeout=35, *, port=GATEWAY_PORT):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if process is not None:
            require(process.poll() is None, "Test service exited before readiness")
        try:
            response = http("health", port=port)
        except (URLError, TimeoutError, ConnectionError):
            sleep(.25)
        else:
            require(response == {"profile": PROFILE, "harness": HARNESS_VERSION, "synthetic": True},
                    f"Unexpected health response: {response}")
            return
    raise TimeoutError("Test service did not become ready")


def probe(lab, expected_prompt):
    ready(port=lab.gateway_port)
    text = f"合成验收 {uuid4().hex}"
    response = http("test-turn", {"text": text}, timeout=45, port=lab.gateway_port)
    require(response.get("reply") == REPLY, f"Unexpected test response: {response}")
    requests = [json.loads(line) for line in lab.capture.read_text(encoding="utf-8").splitlines()]
    matches = [item for item in requests if text in json.dumps(item.get("messages"), ensure_ascii=False)]
    require(len(matches) == 1, f"Expected one model request, got {len(matches)}")
    request = matches[0]
    require((PROMPT in json.dumps(request.get("system"), ensure_ascii=False)) == expected_prompt,
            "Unexpected bundle prompt in captured request")
    require(request.get("model") == MODEL and request.get("max_tokens") == 900, "Unexpected model settings")
    require(not request.get("tools"), "The stage 1 model unexpectedly received tools")
    print(f"PASS health and synthetic turn, bundle prompt={expected_prompt}, mock requests=1", flush=True)


def verify_database(lab):
    with sqlite3.connect(f"{lab.database.as_uri()}?mode=ro", uri=True) as connection:
        require(connection.execute("PRAGMA quick_check").fetchone() == ("ok",), "Fixture database check failed")
        marker = connection.execute("SELECT value FROM meta WHERE key='harness_fixture'").fetchone()
        require(marker == ("stage1-v1",), "Missing synthetic fixture marker")
        for table in ("messages", "memories"):
            count = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            require(count == 2, f"Expected two synthetic rows in {table}, got {count}")
    print("PASS fixture SQLite quick_check and synthetic rows", flush=True)


def verify_package(lab):
    with tarfile.open(lab.tarball, "r:gz") as archive:
        require(set(archive.getnames()) == {"package/package.json", "package/index.js", "package/cordis.patch.yml"},
                "Unexpected bundle archive members")
        manifest = json.load(archive.extractfile("package/package.json"))
        require(manifest["engines"]["dsh"] == HARNESS_VERSION, "Bundle does not pin compatibility")
    print("PASS npm archive contents and Harness compatibility", flush=True)


@contextmanager
def running(lab, name, development=False, *, action="serve", startup_timeout=35):
    logs = lab.root / "acceptance"
    logs.mkdir(exist_ok=True)
    log_path = logs / f"{name}.log"
    command = [sys.executable, str(SOURCE / "stage1.py"), "--root", str(lab.root),
               "--gateway-port", str(lab.gateway_port), "--model-port", str(lab.model_port), action]
    if development:
        command.append("--development")
    with log_path.open("wb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            ready(process, startup_timeout, port=lab.gateway_port)
            yield
        except Exception:
            print(log_path.read_text(encoding="utf-8", errors="replace"), file=sys.stderr)
            raise
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def manifest_has_bundle(lab):
    manifest = json.loads((lab.profile / "package.json").read_text(encoding="utf-8"))
    return BUNDLE in manifest["dsh"]["profile"]["bundles"]


def acceptance(lab, cases):
    for case in cases:
        print(f"CASE {case}", flush=True)
        if case == "fixture":
            verify_database(lab)
        elif case == "pack":
            lab.pack()
            verify_package(lab)
        elif case in ("boot", "development", "install", "restart", "uninstall"):
            if case in ("boot", "development", "uninstall") and manifest_has_bundle(lab):
                lab.uninstall()
            if case == "install":
                lab.install()
            installed = case in ("install", "restart")
            require(manifest_has_bundle(lab) == installed, f"Unexpected manifest for {case}")
            with running(lab, case, development=case == "development"):
                probe(lab, installed or case == "development")
            if case == "restart":
                require(any((lab.home / "sessions").rglob("*.jsonl")), "Missing persisted session logs")


def main():
    cases = ("fixture", "boot", "development", "pack", "install", "restart", "uninstall")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=SOURCE / ".stage1" / "lab")
    parser.add_argument("--case", choices=cases, action="append")
    parser.add_argument("--probe", choices=("installed", "bare"))
    args = parser.parse_args()
    lab = Lab(args.root)
    if args.probe:
        probe(lab, args.probe == "installed")
    else:
        acceptance(lab, args.case or cases)


if __name__ == "__main__":
    main()
