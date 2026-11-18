"""Exercise the real Harness web gateway against a two-page synthetic PDF."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request
from uuid import uuid4

from mock_model import REPLY
from role_gateway import load_tokens
from stage1 import HOST, PROFILE, SOURCE, Lab
from verify import OPENER, require, running, verify_package
from verify_stage2 import Check, GUEST, OWNER, excludes, includes, tool
from verify_stage3 import TOOL, event_log, package_sample, profile

PAGE_ONE = "ORBIT-41"
PAGE_TWO = "LANTERN-72"
CASES = ("document", "operations", "recovery")


def fixture(path):
    import fitz

    contents = (
        ("North Lab", "Cabinet label: " + PAGE_ONE, "Access day: Wednesday"),
        ("Return desk", "Desk label: " + PAGE_TWO, "Return deadline: 16:30"),
    )
    with fitz.open() as pdf:
        for number, lines in enumerate(contents, 1):
            page = pdf.new_page(width=420, height=270)
            page.insert_text((38, 42), "SYNTHETIC REFERENCE", fontsize=10, color=(.18, .34, .56))
            page.insert_text((38, 88), lines[0], fontsize=24)
            for index, line in enumerate(lines[1:], 1):
                page.insert_text((38, 98 + index * 30), line, fontsize=14)
            page.insert_text((38, 238), f"Page {number} of {len(contents)}", fontsize=10)
        pdf.save(path)


def setup(lab):
    lab.prepare()
    with (lab.profile / "pnpm-workspace.yaml").open("a", encoding="utf-8") as config:
        config.write("\nstoreDir: .pnpm-store\n")
    lab.pack()
    verify_package(lab)
    lab.install()
    lab.command("plugin", "--profile", PROFILE, "add", str(package_sample(lab)))
    profile(lab, "web", partner=True)
    path = lab.root / "synthetic-reference.pdf"
    fixture(path)
    return path


def browser_inputs(lab, pdf):
    tokens = load_tokens(lab.root)
    values = {"owner": tokens[OWNER], "guest": tokens[GUEST], "pdf": str(pdf), "root": str(lab.root)}
    snippet = (SOURCE / "verify_stage4_browser.js").read_text(encoding="utf-8")
    (lab.root / "browser-run.js").write_text(snippet.replace('"__ISLA_FIXTURE__"', json.dumps(values)), encoding="utf-8")
    plan_browser(lab)


def plan_browser(lab):
    actions = [
        {"match": "网页验收的普通消息。", "tools": [tool(TOOL, "web-ui-tool")]},
        {"match": "网页验收的普通消息。", "text": "普通消息已收到。"},
        {"match": "网页上两页分别有哪些标签？", "text": f"实验室是 {PAGE_ONE} [p.1]，归还处是 {PAGE_TWO} [p.2]。"},
        {"match": "网页追问归还时间呢？", "text": "网页追问的截止时间是 16:30 [p.2]。", "delay": 3},
        {"match": "网页核对无效页码和重试。", "text": "错误引用 [p.99]。"},
        {"match": "网页核对无效页码和重试。", "text": "有效引用是归还截止时间 16:30 [p.2]。"},
    ]
    (lab.root / "mock-script.json").write_text(json.dumps({"nonce": uuid4().hex, "actions": actions}), encoding="utf-8")


class WebCheck(Check):
    def serve(self, name="web"):
        return running(self.lab, name, action="serve-web", startup_timeout=60)

    def api(self, path, *, identity=OWNER, data=None, code=200, view=None, headers=None):
        headers = dict(headers or {})
        token_path = self.lab.root / "identity-tokens.json"
        tokens = json.loads(token_path.read_text(encoding="utf-8"))
        headers["Authorization"] = "Bearer " + tokens.get(identity, "invalid")
        if view:
            headers["X-Conversation"] = view
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        request = Request(f"http://{HOST}:{self.lab.gateway_port}/api/web{path}", data, headers)
        try:
            reply = OPENER.open(request, timeout=15)
        except HTTPError as error:
            reply = error
        with reply:
            actual = reply.code
            result = json.load(reply)
        with (self.lab.root / "api-trace.jsonl").open("a", encoding="utf-8") as trace:
            trace.write(json.dumps({"path": path, "principal": identity, "status": actual, "reply": result},
                                   ensure_ascii=False) + "\n")
        require(actual == code, f"{path} returned {actual}, expected {code}: {result}")
        return result

    def state(self, identity=OWNER):
        return self.api("/state", identity=identity)

    def upload(self, data, name="合成跨页资料.pdf", *, identity=OWNER):
        return self.api("/attachments", identity=identity, data=data, code=202,
                        view=self.state(identity)["conversation"],
                        headers={"Content-Type": "application/pdf", "X-File-Name": quote(name)})["id"]

    def send(self, request_id, text, attachment_id=None, *, identity=OWNER, code=202):
        return self.api("/turns", identity=identity, code=code, view=self.state(identity)["conversation"],
                        data={"requestId": request_id, "text": text, "attachmentId": attachment_id})

    def change(self, group, job_id, action, *, identity=OWNER, code=200):
        return self.api(f"/{group}/{job_id}/{action}", identity=identity, code=code,
                        view=self.state(identity)["conversation"], data=b"")

    def get_status(self, job_id, identity):
        return next((item for item in self.state(identity)["items"] if item["id"] == job_id), None)

    def wait(self, job_id, status, identity=OWNER, *, timeout=15):
        deadline = monotonic() + timeout
        item = None
        while monotonic() < deadline:
            item = self.get_status(job_id, identity)
            if item and item["status"] == status:
                return item
            if item and item["status"] in {"failed", "cancelled", "ready"} and status != "failed":
                break
            sleep(.12)
        raise RuntimeError(f"{job_id}: expected {status}, got {item}")

    def receipt(self, job_id):
        path = (self.lab.root / "web.db").as_uri()
        with closing(sqlite3.connect(path + "?mode=ro", uri=True)) as db:
            receipt, = db.execute("SELECT receipt FROM jobs WHERE kind='turn' AND id=?", (job_id,)).fetchone()
        return json.loads(receipt)

    def session(self, job_id):
        return self.receipt(job_id)["session"]

    def document_timing(self, job_id):
        receipt = self.receipt(job_id)
        session = receipt["session"]
        logs = (self.lab.home / "sessions").glob(f"*/{session}/session.v4.jsonl")
        require(not any(logs), f"Document session {session} was persisted")
        ticks = receipt["timings"]
        start, first, end = ticks["turn/start"], ticks["firstText"], ticks["turn/end"]
        require(start <= first <= end, f"Invalid document timings: {ticks}")
        return {"harnessFirstTextMs": first - start, "harnessTurnMs": end - start}

    def await_model(self, text):
        deadline = monotonic() + 8
        while monotonic() < deadline:
            if any(text in json.dumps(request["messages"], ensure_ascii=False) for request in self.requests()):
                return
            sleep(.10)
        raise RuntimeError(f"Did not observe a model request for {text}")


def document(check, pdf):
    first = "两页分别写了哪个标签？"
    first_reply = f"实验室是 {PAGE_ONE} [p.1]，归还处是 {PAGE_TWO} [p.2]。"
    followup = "后一个地点的截止时间呢？"
    with check.serve("document-first"):
        check.api("/state", identity="invalid", code=401)
        attachment = check.upload(pdf)
        ready = check.wait(attachment, "ready")
        require(ready["pageCount"] == 2, "Did not extract both PDF pages")
        page = f"/attachments/{attachment}/pages/"
        page_one = check.api(page + "1")
        page_two = check.api(page + "2")
        require(PAGE_ONE in page_one["text"] and PAGE_TWO not in page_one["text"]
                and PAGE_TWO in page_two["text"] and PAGE_ONE not in page_two["text"], "Incorrect page extraction")
        check.api(page + "3", code=404)
        check.api(page + "1", identity=GUEST, code=404)
        check.send("foreign", "看别人的 PDF", attachment, identity=GUEST, code=404)
        check.change("attachments", attachment, "cancel", identity=GUEST, code=404)
        check.api("/turns", data={"text": first, "attachmentId": attachment, "requestId": "stale-view"},
                  identity=GUEST, view=check.state()["conversation"], code=409)
        check.plan(first, {"text": first_reply})
        check.send("doc-first", first, attachment)
        result = check.wait("doc-first", "ready")
        require(result["reply"] == first_reply and [x["page"] for x in result["citations"]] == [1, 2],
                "Missing page citations")
        require(not check.state(GUEST)["items"], "Guest saw owner history or attachment")
        request, = check.requests()
        includes(request, "独立的 PDF 问答", first, PAGE_ONE, PAGE_TWO)
        require(not request.get("tools"), "Document turn offered tools")
        blocks = [block.get("text", "") for item in request["messages"] for block in item["content"]]
        data = next(block for block in blocks if block.startswith("PDF 资料（JSON 数据）\n"))
        source = json.loads(data.split("\n", 1)[1])
        require([page["page"] for page in source["pages"]] == [1, 2], "Wire page labels are wrong")
        check.document_timing("doc-first")
        require(not result["tools"], "Document runtime executed a tool")
    with check.serve("document-restart"):
        require(check.get_status("doc-first", OWNER)["reply"] == first_reply, "Document history did not recover")
        require(PAGE_TWO in check.api(page + "2")["text"], "Persisted source page missing after restart")
        check.plan(followup, {"text": "归还截止时间是 16:30 [p.2]。"})
        check.send("doc-followup", followup, attachment)
        require(check.wait("doc-followup", "ready")["citations"][0]["page"] == 2, "Follow-up reference incorrect")
        check.document_timing("doc-followup")
        includes(check.requests()[-1], first, first_reply, followup, PAGE_TWO)
        plain = "回到普通聊天。"
        check.plan(plain, {"text": REPLY})
        check.send("doc-plain", plain)
        check.wait("doc-plain", "ready")
        excludes(check.requests()[-1], PAGE_ONE, PAGE_TWO, first, followup)
        require(check.count_turn("doc-first") == check.count_turn("doc-followup") == 0,
                "Document Q&A entered the role ledger")
        require(check.sql("SELECT count(*) FROM messages WHERE instr(content, ?) OR instr(content, ?)",
                          PAGE_ONE, PAGE_TWO) == [(0,)], "Source material was written to conversation memory")
        require(check.sql("SELECT count(*) FROM memories WHERE instr(summary, ?) OR instr(summary, ?)",
                          PAGE_ONE, PAGE_TWO) == [(0,)], "Source material was written as a shared experience")


def operations(check, pdf):
    slow = "模拟占用并取消这条聊天。"
    question = "给一个可核对的页码。"
    with check.serve():
        check.plan(slow, {"text": REPLY, "delay": 6})
        check.send("cancel-turn", slow)
        check.await_model(slow)
        queued = check.upload(pdf, "cancelled.pdf")
        check.wait(queued, "queued")
        check.change("attachments", queued, "cancel")
        check.wait(queued, "cancelled")
        check.api(f"/attachments/{queued}/pages/1", code=404)
        check.change("turns", "cancel-turn", "cancel")
        check.wait("cancel-turn", "cancelled")
        broken = check.upload(b"%PDF-1.7\nSYNTHETIC_INVALID", "misformed.pdf")
        check.wait(broken, "failed")
        check.change("attachments", broken, "retry")
        check.wait(broken, "failed")

        attachment = check.upload(pdf, "retry.pdf")
        check.wait(attachment, "ready")
        check.plan(question, {"text": "无效页码 [p.99]。"})
        check.send("retry-document", question, attachment)
        failed = check.wait("retry-document", "failed")
        require("页码" in failed["error"] and "reply" not in failed, "Invalid page citation was exposed")
        check.plan(question, {"text": "截止时间是 16:30 [p.2]。"})
        check.change("turns", "retry-document", "retry")
        require(check.wait("retry-document", "ready")["citations"][0]["page"] == 2, "Retry did not recover")
        require(check.count_turn("cancel-turn") == 0, "Cancelled chat mutated role state")
        excludes(check.requests()[-1], slow)


def recovery(check, _pdf):
    text = "重启后请继续这条普通消息。"
    with check.serve("first-process"):
        check.plan(text, {"text": REPLY, "delay": 7})
        check.send("restart-turn", text)
        check.await_model(text)
        check.wait("restart-turn", "running")
    check.plan(text, {"tools": [tool(TOOL, "restart-tool")]}, {"text": REPLY})
    with check.serve("second-process"):
        check.send("restart-turn", text)
        result = check.wait("restart-turn", "ready")
        require(result["reply"] == REPLY and check.count_turn("restart-turn") == 1, "Failed exact-once recovery")
        check.send("restart-turn", "不同输入", code=409)
        statuses = [event["status"] for event in result["tools"]]
        require(statuses == ["running", "ready"] and all(e["name"] == TOOL for e in result["tools"]),
                "Missing safe web tool event projection")
        requests = check.requests()
        require(len(requests) == 3 and TOOL in {item["name"] for item in requests[1]["tools"]},
                "Wrong restart model request count or tool list")
        includes(requests[2], "SYNTH-REF-001")
        require(not check.state(GUEST)["items"], "Guest saw recovered owner history")
        events = event_log(check, check.session("restart-turn"))
        require([event["type"] for event in events if event["type"].startswith("tool/")] == ["tool/call", "tool/result"],
                "Missing original Harness tool event pair")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=SOURCE / ".stage1" / "stage4-validation")
    options = parser.add_mutually_exclusive_group()
    options.add_argument("--case", choices=CASES, action="append")
    options.add_argument("--prepare-browser", action="store_true")
    args = parser.parse_args()
    root = args.root / uuid4().hex[:10]
    print(f"Evidence directory: {root}", flush=True)
    if args.prepare_browser:
        lab = Lab(root)
        browser_inputs(lab, setup(lab))
        return
    failures = []
    for name in args.case or CASES:
        try:
            lab = Lab(root / name)
            pdf = setup(lab)
            check = WebCheck(lab)
            globals()[name](check, pdf.read_bytes())
            require(check.sql("PRAGMA quick_check") == [("ok",)], "Giftia database integrity check failed")
            with closing(sqlite3.connect(lab.root / "web.db")) as db:
                require(db.execute("PRAGMA quick_check").fetchone() == ("ok",), "Web database integrity check failed")
        except Exception as error:
            failures.append(name)
            print(f"FAIL {name}: {error}", flush=True)
        else:
            print(f"PASS {name}", flush=True)
    require(not failures, f"Failed cases: {', '.join(failures)}")


if __name__ == "__main__":
    main()
