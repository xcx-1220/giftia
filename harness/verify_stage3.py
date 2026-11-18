"""Verify the native sample tool through the existing ordinary chat endpoint."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
from uuid import uuid4

from mock_model import MODEL, REPLY
from role_gateway import ADMIN
from stage1 import BUNDLE, HARNESS_VERSION, PROFILE, SOURCE, Lab
from verify import require, verify_package
from verify_stage2 import Check, GUEST, OWNER, tool

SAMPLE = SOURCE / "sample-tool-bundle"
MANIFEST = json.loads((SAMPLE / "package.json").read_text(encoding="utf-8"))
PACKAGE = MANIFEST["name"]
TOOL = "sample_reference"
REFERENCE = "合成资料 SYNTH-REF-001：星砂图书室每周三 14:00 开放。"
MEMORY_TOOLS = {"remember", "search", "forget"}
CASES = ("installed", "permissions", "disabled", "reenabled", "uninstalled")


def package_sample(lab):
    archive = lab.pack_dir / f"{PACKAGE}-{MANIFEST['version']}.tgz"
    subprocess.run([shutil.which("npm"), "pack", "--ignore-scripts", "--pack-destination", str(lab.pack_dir)],
                   cwd=SAMPLE, env=lab.env, check=True)
    with tarfile.open(archive, "r:gz") as package:
        require(set(package.getnames()) == {"package/package.json", "package/index.js", "package/cordis.patch.yml"},
                "Unexpected sample archive contents")
        manifest = json.load(package.extractfile("package/package.json"))
        require(manifest["engines"]["dsh"] == HARNESS_VERSION and not manifest.get("dependencies"),
                "Sample package should pin Harness and run with its native services")
    print(f"PASS {archive.name} SHA-256 {sha256(archive.read_bytes()).hexdigest()}", flush=True)
    return archive


def profile(lab, label, *, partner=None, guest=False, disabled=False, installed=True):
    # Use a fresh isolated profile, preserving its original Stage 2 configuration.
    patch = (SOURCE / "profile.patch.yml").read_text(encoding="utf-8")
    if partner is not None:
        patch += f"""\n- id: giftia-sample-tool
  disabled: {str(disabled).lower()}
  config:
    permissions:
      partner: {str(partner).lower()}
      guest: {str(guest).lower()}
"""
    path = lab.profile / "cordis.patch.yml"
    path.write_text(patch, encoding="utf-8")
    manifest = json.loads((lab.profile / "package.json").read_text(encoding="utf-8"))
    bundles = manifest["dsh"]["profile"]["bundles"]
    dependencies = manifest["dependencies"]
    require(BUNDLE in bundles and BUNDLE in dependencies, "Isla bundle disappeared from the profile")
    require((PACKAGE in bundles) == installed and (PACKAGE in dependencies) == installed,
            f"Unexpected sample package state for {label}")
    shutil.copyfile(path, lab.root / f"{label}.patch.yml")
    (lab.root / f"{label}.profile.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def event_log(check, session):
    paths = list((check.lab.home / "sessions").glob(f"*/{session}/session.v4.jsonl"))
    require(len(paths) == 1, f"Missing session evidence for {session}")
    return [json.loads(line) for line in paths[0].read_text(encoding="utf-8").splitlines()]


def probe(check, label, identity, *, allowed=False, partner=False, force_call=False):
    request_id = f"{label}-{identity}"
    call_id = f"sample-{request_id}"
    text = f"合成资料测试（{request_id}）"
    before = len(check.requests())
    calls_tool = allowed or force_call
    if calls_tool:
        check.plan(text, {"tools": [tool(TOOL, call_id)]}, {"text": REPLY})
    else:
        # A restarted mock can match a prior script against restored chat history.
        check.plan(text, {"text": REPLY})
    reply = check.chat(request_id, text, identity)
    require(reply["reply"] == REPLY and not reply["replayed"], f"{request_id}: ordinary chat failed")
    requests = check.requests()[before:]
    require(len(requests) == (2 if calls_tool else 1), f"{request_id}: unexpected model request count")
    expected_tools = (MEMORY_TOOLS if partner else set()) | ({TOOL} if allowed else set())
    for request in requests:
        actual_tools = {item["name"] for item in request.get("tools", [])}
        require(actual_tools == expected_tools, f"{request_id}: {actual_tools} != {expected_tools}")
        require(request["model"] == MODEL, f"{request_id}: unexpected model")
    events = event_log(check, reply["session"])
    calls = [event for event in events if event.get("type") == "tool/call"]
    results = [event for event in events if event.get("type") == "tool/result"]
    require(len(calls) == len(results) == int(calls_tool), f"{request_id}: unexpected session tool event count")
    if calls_tool:
        call, result = calls[0], results[0]
        require(call["data"]["name"] == TOOL and call["data"]["callId"] == call_id
                and json.loads(call["data"]["arguments"]) == {}, f"{request_id}: wrong tool call")
        message = result["data"]["message"]
        require(message["toolCallId"] == call_id and call["seq"] in result["sourceEventSeqs"]
                and message["isError"] == (not allowed), f"{request_id}: wrong tool result event")
        blocks = [block for entry in requests[1]["messages"] for block in entry["content"]]
        require(any(block["type"] == "tool_use" and block["name"] == TOOL
                    and block["id"] == call_id and block["input"] == {} for block in blocks),
                f"{request_id}: no actual tool call in model continuation")
        returned = [block for block in blocks if block["type"] == "tool_result" and block["tool_use_id"] == call_id]
        require(len(returned) == 1, f"{request_id}: no correlated result in model continuation")
        if allowed:
            expected_content = [{"type": "text", "text": REFERENCE}]
            require(message["content"] == returned[0]["content"] == expected_content,
                    f"{request_id}: wrong synthetic reference")
    if not allowed:
        require(REFERENCE not in json.dumps([requests, events], ensure_ascii=False),
                f"{request_id}: unauthorized reference disclosed")
    require(check.count_turn(request_id, identity) == 1, f"{request_id}: ordinary chat did not commit once")
    print(f"PASS {request_id} tools={sorted(expected_tools)} mock_requests={len(requests)} "
          f"tool_calls={len(calls)} tool_results={len(results)} session={reply['session']}", flush=True)


def run_case(check, case):
    lab = check.lab
    if case == "installed":
        profile(lab, "default")
        with check.serve("default"):
            probe(check, "default", OWNER, partner=True)
            probe(check, "default", GUEST)
        profile(lab, "partner-only", partner=True)
        with check.serve("partner-only"):
            probe(check, "partner-only", OWNER, partner=True, allowed=True)
            probe(check, "partner-only", GUEST, force_call=True)
    elif case == "permissions":
        profile(lab, "guest-only", partner=False, guest=True)
        with check.serve("guest-only"):
            probe(check, "guest-only", OWNER, partner=True)
            probe(check, "guest-only", GUEST, allowed=True)
            check.post("test-admin", {"bind": GUEST}, ADMIN)
            probe(check, "after-rebind", OWNER, allowed=True)
            probe(check, "after-rebind", GUEST, partner=True)
            check.post("test-admin", {"bind": OWNER}, ADMIN)
    elif case == "disabled":
        profile(lab, case, partner=True, guest=True, disabled=True)
        with check.serve(case):
            probe(check, case, OWNER, partner=True)
            probe(check, case, GUEST)
    elif case == "reenabled":
        profile(lab, "disabled-before-reenable", partner=True, guest=True, disabled=True)
        profile(lab, case, partner=True, guest=True)
        with check.serve(case):
            probe(check, case, OWNER, partner=True, allowed=True)
            probe(check, case, GUEST, allowed=True)
    elif case == "uninstalled":
        lab.command("plugin", "--profile", PROFILE, "remove", PACKAGE)
        profile(lab, case, installed=False)
        with check.serve(case):
            probe(check, case, OWNER, partner=True)
            probe(check, case, GUEST)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=SOURCE / ".stage1" / "stage3-validation")
    parser.add_argument("--case", choices=CASES, action="append")
    args = parser.parse_args()
    lab = Lab(args.root / uuid4().hex[:10])
    print(f"Evidence directory: {lab.root}", flush=True)
    lab.prepare()
    # A profile-local pnpm store works on Windows without directory symlink privileges.
    with (lab.profile / "pnpm-workspace.yaml").open("a", encoding="utf-8") as workspace:
        workspace.write("\nstoreDir: .pnpm-store\n")
    lab.pack()
    verify_package(lab)
    lab.install()
    archive = package_sample(lab)
    lab.command("plugin", "--profile", PROFILE, "add", str(archive))
    check = Check(lab)
    failures = []
    for case in args.case or CASES:
        try:
            run_case(check, case)
        except Exception as error:
            failures.append(case)
            print(f"FAIL {case}: {error}", flush=True)
        else:
            print(f"PASS {case}", flush=True)
    require(check.sql("PRAGMA quick_check") == [("ok",)], "Synthetic database quick_check failed")
    require(not failures, f"Failed cases: {', '.join(failures)}")


if __name__ == "__main__":
    main()
