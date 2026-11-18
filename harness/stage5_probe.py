"""Prepare and measure a Stage 5 loopback candidate using synthetic fixtures only."""

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import statistics
import subprocess
from threading import Event, Thread
from time import monotonic, sleep

from mock_model import REPLY
from stage1 import Lab, PROFILE
from verify import require, verify_package
from verify_stage3 import event_log, package_sample, profile
from verify_stage4 import GUEST, OWNER, PAGE_ONE, PAGE_TWO, WebCheck, plan_browser
from web_gateway import NO_EVIDENCE

BENCH_VISITOR = "synthetic-visitor"
CUTOVER = "stage5-cutover"
CUTOVER_TEXT = "这是云端切换演练后新增的合成聊天。"


def prepare(lab):
    lab.prepare()
    workspace = lab.profile / "pnpm-workspace.yaml"
    if "storeDir:" not in workspace.read_text(encoding="utf-8"):
        with workspace.open("a", encoding="utf-8") as config:
            config.write("\nstoreDir: .pnpm-store\n")
    lab.pack()
    verify_package(lab)
    lab.install()
    lab.command("plugin", "--profile", PROFILE, "add", str(package_sample(lab)))
    profile(lab, "stage5", partner=True)


def cgroup_value(cgroup, name):
    return int((cgroup / name).read_text(encoding="utf-8"))


@contextmanager
def sample_memory(cgroup):
    values, stop = [], Event()

    def collect():
        while True:
            values.append(cgroup_value(cgroup, "memory.current"))
            if stop.wait(.02):
                break

    thread = Thread(target=collect)
    thread.start()
    try:
        yield values
    finally:
        stop.set()
        thread.join()


def disk(root):
    return int(subprocess.check_output(["du", "-sb", str(root)], text=True).split()[0])


def settled_events(check, session):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        try:
            events = event_log(check, session)
            if any(event["type"] == "turn/end" for event in events):
                return events
        except (RuntimeError, json.JSONDecodeError):
            pass
        sleep(.025)
    raise TimeoutError("Harness session log did not flush its completion event")


def turn_times(check, job_id, text, attachment, reply, expected_pages, *, identity):
    before = len(check.requests())
    started = monotonic()
    check.plan(text, {"text": reply})
    check.send(job_id, text, attachment, identity=identity)
    job = check.wait(job_id, "ready", identity)
    done_ms = round(1000 * (monotonic() - started), 1)
    require(job["reply"] == reply and [item["page"] for item in job["citations"]] == expected_pages,
            "Unexpected synthetic reply or source pages")
    if attachment:
        timing = check.document_timing(job_id)
    else:
        events = settled_events(check, check.session(job_id))
        turn_start = next(event["time"] for event in events if event["type"] == "turn/start")
        turn_end = next(event["time"] for event in events if event["type"] == "turn/end")
        first = min(chunk["time0"] for event in events if event["type"] == "assistant/message"
                    and event["data"]["message"]["source"]["kind"] == "model"
                    for chunk in event["data"]["stream"] if chunk["type"] == "text-chunks" and chunk["texts"])
        timing = {"harnessFirstTextMs": first - turn_start, "harnessTurnMs": turn_end - turn_start}
    calls = len(check.requests()) - before
    require(calls == 1, "Unexpected request count")
    return {"id": job_id, **timing, "webReadyMs": done_ms, "mockCalls": calls, "pages": expected_pages}


def measure(check, pdf, cgroup):
    old_count = len(check.requests())
    with sample_memory(cgroup) as idle:
        sleep(2)
    before = disk(check.lab.root)
    with sample_memory(cgroup) as active:
        started = monotonic()
        attachment = check.upload(pdf.read_bytes(), identity=BENCH_VISITOR)
        check.wait(attachment, "ready", BENCH_VISITOR)
        extract_ms = round(1000 * (monotonic() - started), 1)
        trials = [
            ("stage5-pdf-pair", "云端 PDF 验收，两页的标签是什么？",
             f"实验室 {PAGE_ONE} [p.1]，归还处 {PAGE_TWO} [p.2]。", [1, 2]),
            ("stage5-pdf-followup", "云端 PDF 追问，第二个地点几点截止？", "归还截止时间 16:30 [p.2]。", [2]),
            ("stage5-pdf-absent", "云端 PDF 验收，资料里给出联系电话了吗？", NO_EVIDENCE, []),
        ]
        measurements = [turn_times(check, job, text, attachment, reply, pages, identity=BENCH_VISITOR)
                        for job, text, reply, pages in trials]
    require(len(check.requests()) - old_count == 3, "Unexpected PDF model invocation count")
    pair = check.api(f"/attachments/{attachment}/pages/2", identity=BENCH_VISITOR)
    require("16:30" in pair["text"] and PAGE_ONE not in pair["text"], "Incorrect page extraction")
    check.api(f"/attachments/{attachment}/pages/2", identity=GUEST, code=404)
    for request in check.requests()[old_count:]:
        require(not request.get("tools"), "PDF request offered tools")
    pdf_disk = disk(check.lab.root)
    cutover = turn_times(check, CUTOVER, CUTOVER_TEXT, None, REPLY, [], identity=OWNER)
    before_retry = len(check.requests())
    check.send(CUTOVER, CUTOVER_TEXT)
    check.wait(CUTOVER, "ready")
    check.send(CUTOVER, CUTOVER_TEXT + "改文", code=409)
    require(check.count_turn(CUTOVER) == 1 and len(check.requests()) == before_retry, "Cutover request duplicated")
    return {"idleMemoryBytes": {"median": statistics.median(idle), "max": max(idle), "samples": len(idle)},
            "pdfMemoryBytes": {"sampledPeak": max(active), "servicePeak": cgroup_value(cgroup, "memory.peak"),
                               "samples": len(active)},
            "stateApparentBytes": {"idle": before, "afterPdf": pdf_disk}, "pdfUploadReadyMs": extract_ms,
            "attachment": attachment, "pdf": measurements, "cutover": cutover}


def restored(check):
    owner_ready = check.wait(CUTOVER, "ready")
    old_requests = len(check.requests())
    check.send(CUTOVER, CUTOVER_TEXT)
    require(check.wait(CUTOVER, "ready") == owner_ready and check.count_turn(CUTOVER) == 1,
            "Ordinary receipt was not restored")
    require(len(check.requests()) == old_requests, "Duplicate model call after restore")
    items = check.state(BENCH_VISITOR)["items"]
    attachment, = (item["id"] for item in items if item["kind"] == "attachment" and item["status"] == "ready")
    require(check.wait("stage5-pdf-pair", "ready", BENCH_VISITOR)["citations"][1]["page"] == 2,
            "Attachment history was not restored")
    require(PAGE_TWO in check.api(f"/attachments/{attachment}/pages/2", identity=BENCH_VISITOR)["text"],
            "Source page was not restored")
    check.api(f"/attachments/{attachment}/pages/2", identity=GUEST, code=404)
    require(event_log(check, check.session(CUTOVER)), "Archived ordinary session is missing")
    result = turn_times(check, "stage5-restored-followup", "恢复后第二页的资料是什么？", attachment,
                        f"归还处 {PAGE_TWO}，16:30 截止 [p.2]。", [2], identity=BENCH_VISITOR)
    return {"giftiaReceiptReplayed": True, "sessionsReadable": True, "attachmentSourceReadable": True,
            "foreignSourceDenied": True, "followup": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--gateway-port", type=int, default=18121)
    parser.add_argument("--model-port", type=int, default=18122)
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--cgroup", type=Path)
    parser.add_argument("action", choices=("prepare", "measure", "plan-browser", "restored"))
    args = parser.parse_args()
    lab = Lab(args.root, args.gateway_port, args.model_port)
    check = WebCheck(lab)
    if args.action == "prepare":
        prepare(lab)
    elif args.action == "plan-browser":
        plan_browser(lab)
    elif args.action == "measure":
        if args.pdf is None or args.cgroup is None:
            parser.error("measure requires --pdf and --cgroup")
        result = measure(check, args.pdf, args.cgroup)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(restored(check), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
