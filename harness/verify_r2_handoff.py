"""Rehearse receipt handoff using explicitly mapped synthetic identities only."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
from time import time
from urllib.error import HTTPError
from urllib.request import Request
from uuid import uuid4

from giftia.webgate import WebGate
from r2_rollback import prepare_rollback, write_new_ledger
from release_config import WebRelease
from role_service import PURPOSE, RoleService
from stage1 import GATEWAY_PORT, MODEL_PORT, Lab, SOURCE
from stage5_probe import prepare
from verify import OPENER, require
from verify_stage4 import GUEST, OWNER, WebCheck

OLD_OWNER = "r2-old-owner-request"
OLD_GUEST = "r2-old-guest-request"
NEW_OWNER = "r2-new-owner-request"
NEW_TEXT = "候选期间新增的合成普通消息。"
NEW_REPLY = "这是待回退的合成答复。"
NEW_GUEST = "r2-new-guest-request"
NEW_GUEST_TEXT = "候选期间访客新增的合成普通消息。"
NEW_GUEST_REPLY = "这是访客待回退的合成答复。"
OLD_WEB_RECOVERY = """
async page => {
  const fixture = __FIXTURE__;
  await page.locator('#gate-pass').fill(fixture.passphrase);
  await page.locator('#gate-btn').click();
  await page.locator('#chat').waitFor();
  await page.evaluate(({requestId, text}) => {
    const view = localStorage.getItem('ella.view');
    if (!view) throw new Error('Old browser did not set the authorized view');
    localStorage.setItem('ella.pending.' + view, JSON.stringify({request_id: requestId, text}));
    localStorage.setItem('ella.history.' + view, JSON.stringify([{role:'me', text, ts:Date.now()}]));
  }, fixture);
  await page.reload();
  await page.locator('#stream').getByText(fixture.reply, {exact:true}).waitFor();
  const pending = await page.evaluate(() =>
    localStorage.getItem('ella.pending.' + localStorage.getItem('ella.view')));
  if (pending !== null) throw new Error('Pending request was not cleared after replay');
  return {oldWebDisplayedRecoveredReply: true, pendingRequestCleared: true};
}
"""


def receipt(gate, role, user, request_id, text, reply):
    old_principal = gate.principal(role, user)
    key = old_principal + ":" + request_id
    gate.ledger.reserve(key, {"principal": old_principal, "role": role, "user_id": user,
                              "request_id": request_id, "text": text})
    gate.ledger.update(key, status="completed", reply=reply)
    return key


def seed_legacy(roles, gate, role, principal, request_id):
    is_owner = principal == roles.hub.handler_id
    require(is_owner == (role == "owner"), "Fixture principal was mapped to the wrong role")
    user = "" if is_owner else principal
    scope = "private" if is_owner else "person"
    text, reply = f"来自旧网关的合成问题 {role}。", f"来自旧网关的合成答复 {role}。"
    key = receipt(gate, role, user, request_id, text, reply)
    row = gate.ledger.get(key)
    instance = roles.hub.instance_no
    with roles.db.turn():
        stamp = roles.hub.clock.iso(roles.hub.clock.now())
        for message_role, content, suffix in (("user", text, ""), ("assistant", reply, ":reply")):
            roles.store.add_message(f"i{instance}-m-r2", instance, message_role, content, stamp,
                                    mid="web:" + key + suffix, scope=scope, subject_id=user)
        pair = roles.db.execute("""SELECT id, role, content FROM messages WHERE mid IN (?, ?) AND instance_no=?
            AND scope=? AND subject_id=? AND recalled=0 ORDER BY id""",
                                ("web:" + key, "web:" + key + ":reply", instance, scope, user)).fetchall()
        require([(item["role"], item["content"]) for item in pair] == [("user", row["text"]), ("assistant", row["reply"])],
                "Legacy archived pair does not match its authenticated receipt")
        digest = sha256(row["text"].encode()).hexdigest()
        roles.db.execute("INSERT INTO harness_turns VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (principal, PURPOSE, request_id, instance, roles.binding(principal), digest,
                          "legacy-" + sha256(key.encode()).hexdigest(), pair[0]["id"], pair[1]["id"], 0))
    return text, reply


def legacy_http(gate, role, visitor="", *, payload=None, request_id=None):
    endpoint = "/api/chat" if payload else f"/api/chat/result?request_id={request_id}"
    cookie = f"aila_auth={gate.token(role)}"
    if visitor:
        cookie += f"; aila_visitor={visitor}"
    request = Request(f"http://127.0.0.1:{gate._server.server_port}{endpoint}",
                      data=json.dumps(payload).encode() if payload else None,
                      headers={"Cookie": cookie, "Content-Type": "application/json"})
    try:
        response = OPENER.open(request, timeout=4)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


def browser_recovery(root):
    playwright = shutil.which("playwright-cli")
    if playwright is None:
        raise RuntimeError("Install Playwright CLI to run the old-page probe")
    lab = Lab(root)
    secret = uuid4().hex
    gate = WebGate(port=0, secret=secret, static_dir=SOURCE.parent / "web",
                   data_dir=root / ("old-browser-" + uuid4().hex[:8]))
    roles = RoleService(lab.database, lab.root)
    try:
        hub = roles.hub
        gate._owner_epoch = json.dumps([hub.instance_no, hub.handler_id, hub.handler_bound_at,
                                        hub._int_meta("handler_bound_msg_id")])
        saved = roles.cached_result(roles.saved(OWNER, NEW_OWNER), roles.binding(OWNER),
                                    sha256(NEW_TEXT.encode()).hexdigest())
        receipt(gate, "owner", "", NEW_OWNER, NEW_TEXT, saved["reply"])
    finally:
        roles.store.close()
    require(gate.start(), "Old web probe did not start")
    command = [playwright, "-s=isla-r2-handoff"]
    snippet = gate._data_dir / "probe.js"
    try:
        fixture = {"passphrase": secret, "requestId": NEW_OWNER, "text": NEW_TEXT, "reply": saved["reply"]}
        snippet.write_text(OLD_WEB_RECOVERY.replace("__FIXTURE__", json.dumps(fixture)), encoding="utf-8")
        subprocess.run(command + ["open", f"http://127.0.0.1:{gate._server.server_port}", "--browser=msedge"],
                       cwd=root, check=True)
        output = subprocess.check_output(command + ["--raw", "run-code", "--filename", str(snippet)],
                                         cwd=root, encoding="utf-8")
        require(json.loads(output).get("oldWebDisplayedRecoveredReply"), "Old client did not show restored reply")
        require(gate.jobs.empty(), "Old client recovery enqueued a new request")
        (root / "old-browser-result.json").write_text(output, encoding="utf-8")
        print(output.strip())
    finally:
        subprocess.run(command + ["close"], cwd=root, check=False)
        snippet.unlink(missing_ok=True)
        gate.stop()


def main(parent, gateway_port, model_port):
    root = parent.resolve() / ("handoff-" + uuid4().hex[:10])
    print(f"Evidence directory: {root}", flush=True)
    lab = Lab(root, gateway_port, model_port)
    prepare(lab)
    gate = WebGate(port=0, secret=uuid4().hex, friend_pass=uuid4().hex, static_dir=SOURCE.parent / "web",
                   data_dir=root / "old-gateway", max_per_minute=30)
    roles = RoleService(lab.database, lab.root)
    try:
        hub = roles.hub
        gate._owner_epoch = json.dumps([hub.instance_no, hub.handler_id, hub.handler_bound_at,
                                        hub._int_meta("handler_bound_msg_id")])
        owner = seed_legacy(roles, gate, "owner", OWNER, OLD_OWNER)
        guest = seed_legacy(roles, gate, "friend", GUEST, OLD_GUEST)
    finally:
        roles.store.close()

    check = WebCheck(lab)
    with check.serve("new-gateway"):
        for principal, request_id, (text, reply) in ((OWNER, OLD_OWNER, owner), (GUEST, OLD_GUEST, guest)):
            check.send(request_id, text, identity=principal)
            require(check.wait(request_id, "ready", principal)["reply"] == reply, "Legacy receipt was not replayed")
            require(check.count_turn(request_id, principal) == 1, "Legacy receipt was duplicated")
            check.send(request_id, text + "改文", identity=principal, code=409)
        require(not check.requests(), "A migrated receipt caused a model call")
        require(check.get_status(OLD_OWNER, GUEST) is None and check.get_status(OLD_GUEST, OWNER) is None,
                "Migrated history crossed identity boundaries")
        cutover = time()
        check.plan(NEW_TEXT, {"text": NEW_REPLY})
        check.send(NEW_OWNER, NEW_TEXT)
        require(check.wait(NEW_OWNER, "ready")["reply"] == NEW_REPLY and len(check.requests()) == 1,
                "Candidate ordinary chat did not commit exactly once")
        check.plan(NEW_GUEST_TEXT, {"text": NEW_GUEST_REPLY})
        check.send(NEW_GUEST, NEW_GUEST_TEXT, identity=GUEST)
        require(check.wait(NEW_GUEST, "ready", GUEST)["reply"] == NEW_GUEST_REPLY and len(check.requests()) == 2,
                "Candidate guest chat did not commit exactly once")
    gate.ledger.path.chmod(0o600)
    identities = {OWNER: gate.principal("owner"), GUEST: gate.principal("friend", GUEST)}
    release = WebRelease("https://example.invalid", OWNER, {OWNER: "", GUEST: ""}, Path(), Path(),
                         {"epoch": json.loads(gate._owner_epoch), "principals": identities,
                          "ledger": str(gate.ledger.path)})
    rows, totals = prepare_rollback(lab.database, lab.root / "web.db", release, cutover)
    require(totals == {"added": 2, "alreadyPresent": 0, "unavailable": 0}, "Rollback export omitted a committed pair")
    output = gate.ledger.path.with_name("web_requests.prepared.json")
    write_new_ledger(output, rows)
    output.replace(gate.ledger.path)
    second, repeated = prepare_rollback(lab.database, lab.root / "web.db", release, cutover)
    require(second == rows and repeated["alreadyPresent"] == 2, "Rollback preparation was not idempotent")
    old_secret, old_guest_secret, old_epoch = gate.secret, gate.friend_pass, gate._owner_epoch
    gate = WebGate(port=0, secret=old_secret, friend_pass=old_guest_secret, static_dir=SOURCE.parent / "web",
                   data_dir=output.parent, max_per_minute=30)
    gate._owner_epoch = old_epoch
    require(gate.start(), "Rolled-back web gateway did not start")
    try:
        request = {"request_id": NEW_OWNER, "text": NEW_TEXT}
        status, body = legacy_http(gate, "owner", request_id=NEW_OWNER)
        require(status == 200 and body["reply"] == NEW_REPLY, "Old web result endpoint lost the new reply")
        status, body = legacy_http(gate, "owner", payload=request)
        require(status == 200 and body["reply"] == NEW_REPLY, "Old web submission did not replay receipt")
        require(legacy_http(gate, "friend", GUEST, request_id=NEW_OWNER)[0] == 404, "Owner reply reached old guest")
        require(legacy_http(gate, "owner", request_id=NEW_GUEST)[0] == 404, "Guest reply reached old owner")
        status, body = legacy_http(gate, "friend", GUEST, request_id=NEW_GUEST)
        require(status == 200 and body["reply"] == NEW_GUEST_REPLY, "Old gateway lost guest reply after rollback")
        require(legacy_http(gate, "owner", payload={**request, "text": NEW_TEXT + "改文"})[0] == 409,
                "Old web submission accepted different text under the same request ID")
        require(gate.jobs.empty(), "Replay enqueued new work after rollback")
    finally:
        gate.stop()
    print(json.dumps({"legacyReceiptsReplayed": 2, "harnessReceiptsReplayedAfterRollback": 2,
                      "crossIdentityDenied": True, "changedTextRejected": True, "rollbackIdempotent": True,
                      "newMockCalls": len(check.requests()), "replayAdditionalCalls": 0}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=SOURCE / ".stage1" / "r2-validation")
    parser.add_argument("--gateway-port", type=int, default=GATEWAY_PORT)
    parser.add_argument("--model-port", type=int, default=MODEL_PORT)
    parser.add_argument("--browser-root", type=Path, help="Run the old-page probe against a successful fixture root")
    args = parser.parse_args()
    if args.browser_root:
        browser_recovery(args.browser_root.resolve())
    else:
        main(args.root, args.gateway_port, args.model_port)
