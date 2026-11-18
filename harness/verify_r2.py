"""Targeted R2 rehearsal on synthetic data, using the locked local gateway."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
from time import monotonic, sleep, time
from uuid import uuid4

from giftia.store import Store
from r2_preflight import inspect_candidate, inspect_legacy
from stage1 import GATEWAY_PORT, MODEL_PORT, Lab, SOURCE
from stage5_probe import prepare, settled_events
from stage5_recovery import COMPONENTS, DOCUMENT_MARKER, fingerprints, restore, snapshot
from verify import require
from verify_stage4 import GUEST, OWNER, WebCheck
from web_state import REVOCATIONS_FILE, WebState


def receipts(root):
    now = time()
    principal = "old-owner-view"
    rows = {}
    database = root / "legacy.db"
    store = Store(database)
    try:
        for name, status, messages in (
            ("done", "completed", 2), ("ack-lost", "running", 2),
            ("queued", "queued", 0), ("partial", "running", 1),
            ("failed", "failed", 0), ("expired", "completed", 0),
        ):
            key = f"{principal}:{name}"
            rows[key] = {"principal": principal, "request_id": name, "status": status,
                         "created": now - 90000 if name == "expired" else now, "text": "合成问句", "reply": "合成答复"}
            mid = "web:" + key
            if messages:
                store.add_message("i1-m-rehearsal", 1, "user", "合成问句", "2026-11-17T10:00:00+08:00", mid=mid)
            if messages == 2:
                store.add_message("i1-m-rehearsal", 1, "assistant", "合成答复", "2026-11-17T10:00:01+08:00",
                                  mid=mid + ":reply")
    finally:
        store.close()
    ledger = root / "legacy-web-requests.json"
    ledger.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    result = inspect_legacy(ledger, database, now)
    require(result["classification"] == {"recoverable_db_pair": 2, "queued_without_commit": 1,
                                        "needs_reconciliation": 1, "failed_without_commit": 1, "expired": 1},
            f"Wrong handoff classification: {result}")
    require(result["active"] == 3, "Handoff would lose pending legacy jobs")
    print(json.dumps({"legacyFixture": result}, ensure_ascii=False), flush=True)


def attachment_lifecycle(lab, pdf):
    check = WebCheck(lab)
    plain = "这条普通消息应在删除 PDF 后保留。"

    def ask(name):
        attachment = check.upload(pdf, name + ".pdf")
        check.wait(attachment, "ready")
        question = f"{name} 用例中第 2 页的标签是什么？"
        check.plan(question, {"text": "归还处标签是 LANTERN-72 [p.2]。"})
        check.send(f"r2-{name}", question, attachment)
        check.wait(f"r2-{name}", "ready")
        check.document_timing(f"r2-{name}")
        return attachment

    with check.serve("before-snapshot"):
        check.plan(plain, {"text": "这是普通聊天。"})
        check.send("r2-plain", plain)
        check.wait("r2-plain", "ready")
        ordinary_session = check.session("r2-plain")
        settled_events(check, ordinary_session)
        to_delete = ask("delete")

    backup = lab.root / "before-delete"
    snapshot(lab.root, backup)
    # Model a pre-R2 archive with an older PDF session next to the real ordinary log.
    archive_sessions = backup / COMPONENTS["sessions"]
    old_log = archive_sessions / "legacy-workspace" / "isla-legacy-pdf" / "session.v4.jsonl"
    old_log.parent.mkdir(parents=True)
    old_log.write_text(json.dumps({"type": "user/message", "text": DOCUMENT_MARKER + " R2 page"}) + "\n", encoding="utf-8")
    manifest = backup / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["sessions"] = fingerprints(archive_sessions)
    manifest.write_text(json.dumps(data), encoding="utf-8")

    with check.serve("after-snapshot"):
        for name in ("delete", "expire"):
            attachment = to_delete if name == "delete" else ask(name)
            route = f"/attachments/{attachment}/pages/2"
            check.api(route)
            check.change("attachments", attachment, "delete", identity=GUEST, code=404)
            calls = len(check.requests())
            if name == "delete":
                check.change("attachments", attachment, "delete")
            else:
                with closing(sqlite3.connect(lab.root / "web.db")) as db, db:
                    db.execute("UPDATE jobs SET expires=? WHERE kind='attachment' AND id=?", (time() - 1, attachment))
            check.api(route, code=404)
            check.send(f"r2-blocked-{name}", "请再读一次", attachment, code=404)
            state = check.state(OWNER)
            require("LANTERN-72" not in json.dumps(state, ensure_ascii=False)
                    and [row["id"] for row in state["items"]] == ["r2-plain"], "Residual document/history exposure")
            require(len(check.requests()) == calls, "Revoked attachment reached the model")
        require(check.count_turn("r2-plain") == 1, "Ordinary chat was affected")
        deadline = monotonic() + 3
        while monotonic() < deadline:
            if inspect_candidate(lab.root / "web.db", time())["states"] == {"ready": 1}:
                break
            sleep(.1)
    result = inspect_candidate(lab.root / "web.db", time())
    require(result["states"] == {"ready": 1} and result["documentTurnsWithoutLiveAttachment"] == 0,
            f"Attachment or related jobs lingered: {result}")
    require(len(list((lab.home / "sessions").rglob("session.v4.jsonl"))) == 1, "PDF sessions were persisted")

    untrusted = lab.root / "missing-revocations"
    try:
        restore(backup, untrusted, "attachments")
    except ValueError:
        require(not (untrusted / "web.db").exists(), "A restore without revocations published a database")
    else:
        raise RuntimeError("Attachment restore accepted a missing revocation database")
    untrusted.mkdir()
    shutil.copyfile(backup / COMPONENTS["attachments"], untrusted / "web.db")
    try:
        WebState(untrusted / "web.db")
    except ValueError:
        pass
    else:
        raise RuntimeError("A restored database started without a revocation journal")

    restored = Lab(lab.root / "restored", lab.gateway_port, lab.model_port)
    for component in COMPONENTS:
        restore(backup, restored.root, component, revocations=lab.root / REVOCATIONS_FILE)
    shutil.copyfile(lab.root / "identity-tokens.json", restored.root / "identity-tokens.json")
    prepare(restored)
    recovered = WebCheck(restored)
    with recovered.serve("restored"):
        recovered.api(f"/attachments/{to_delete}/pages/2", code=404)
        recovered.send("r2-restored-blocked", "恢复后请再读一次", to_delete, code=404)
        require([row["id"] for row in recovered.state()["items"]] == ["r2-plain"],
                "Revoked PDF reappeared after restoring older snapshot")
        require(recovered.session("r2-plain") == ordinary_session and settled_events(recovered, ordinary_session),
                "Ordinary session was not restored")
        require(not recovered.requests(), "Recovered state made a model request")
    require(len(list((restored.home / "sessions").rglob("session.v4.jsonl"))) == 1, "Old PDF log was restored")
    print(json.dumps({"attachmentFixture": result, "restoredRevocationApplied": True,
                      "ordinaryLogRestored": True, "legacyPdfLogExcluded": True,
                      "missingRevocationsRejected": True, "mockRequests": len(check.requests())}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=SOURCE / ".stage1" / "r2-validation")
    parser.add_argument("--gateway-port", type=int, default=GATEWAY_PORT)
    parser.add_argument("--model-port", type=int, default=MODEL_PORT)
    args = parser.parse_args()
    root = args.root / uuid4().hex[:10]
    root.mkdir(parents=True)
    print(f"Evidence directory: {root}", flush=True)
    receipts(root)
    lab = Lab(root, args.gateway_port, args.model_port)
    prepare(lab)
    attachment_lifecycle(lab, args.pdf.read_bytes())


if __name__ == "__main__":
    main()
