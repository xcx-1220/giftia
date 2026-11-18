"""Rehearse authenticated historical pagination without sending a model request."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
from uuid import uuid4

from role_service import RoleService
from stage1 import GATEWAY_PORT, HOST, MODEL_PORT, Lab, SOURCE
from stage5_probe import prepare
from verify import require
from verify_stage4 import GUEST, OWNER, WebCheck

STAMP = "2026-11-17T12:00:00+08:00"
BROWSER = """
async page => {
  const keys = __TOKENS__;
  const login = async token => {
    await page.getByLabel('会话凭据').fill(token);
    await page.getByRole('button', {name:'进入', exact:true}).click();
  };
  await login(keys.owner);
  await page.getByRole('button', {name:'历史', exact:true}).click();
  const panel = page.getByRole('dialog', {name:'普通聊天历史'});
  await panel.getByText('合成旧搭档历史 45', {exact:true}).waitFor();
  await panel.getByRole('button', {name:'更早消息'}).click();
  await panel.getByText('合成旧搭档历史 01', {exact:true}).waitFor();
  if (await panel.locator('.history-row').count() !== 45) throw new Error('Owner history pagination count');
  await panel.getByRole('button', {name:'关闭', exact:true}).click();
  await page.getByRole('button', {name:'退出', exact:true}).click();
  await login(keys.guest);
  await page.getByRole('button', {name:'历史', exact:true}).click();
  await panel.getByText('合成旧访客历史', {exact:true}).waitFor();
  if (await panel.locator('.history-row').count() !== 1) throw new Error('Guest history leaked');
  return {ownerHistory:45, guestHistory:1, roleSwitchIsolated:true};
}
"""


def set_owner(database, identity):
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE state SET value=? WHERE key='handler_id'", (identity,))


def seed(lab):
    roles = RoleService(lab.database, lab.root)
    try:
        store = roles.store
        instance = roles.hub.instance_no
        private = f"i{instance}-m-20261117"
        guest_session = f"i{instance}-p{GUEST[:12]}-20261117"
        predecessor = store.add_message(private, instance, "user", "旧搭档不可见", STAMP, mid="previous-owner")
        store.meta_set("handler_bound_at", STAMP)
        store.meta_set("handler_bound_msg_id", predecessor)
        store.add_message(f"i{instance}-gother-20261117", instance, "user", "误归私聊的群消息", STAMP, scope="private")
        store.add_message(f"i{instance + 1}-m-20261117", instance + 1, "user", "其他实例", STAMP, scope="private")
        store.add_message(private, instance, "user", "已撤回", STAMP, mid="withdrawn-history")
        store.mark_recalled("withdrawn-history")
        for number in range(1, 46):
            store.add_message(private, instance, "user", f"合成旧搭档历史 {number:02}", STAMP)
        store.add_message(guest_session, instance, "assistant", "合成旧访客历史", STAMP, scope="person", subject_id=GUEST)
        store.add_message(guest_session, instance, "assistant", "其他访客分区", STAMP, scope="person", subject_id="other")
    finally:
        roles.store.close()


def main(parent, gateway_port, model_port):
    root = parent.resolve() / ("history-" + uuid4().hex[:10])
    print(f"Evidence directory: {root}", flush=True)
    lab = Lab(root, gateway_port, model_port)
    prepare(lab)
    seed(lab)
    check = WebCheck(lab)
    with check.serve("history"):
        owner_view = check.state(OWNER)["conversation"]
        guest_view = check.state(GUEST)["conversation"]
        check.api("/history", identity="unrecognized", code=401)
        check.api("/history", identity=GUEST, view=owner_view, code=409)
        page1 = check.api("/history", view=owner_view)
        page2 = check.api(f"/history?before={page1['before']}", view=owner_view)
        received = [row["text"] for row in page1["messages"] + page2["messages"]]
        require(len(page1["messages"]) == 40 and page2["before"] is None, "Pagination boundaries differ")
        require(received == [f"合成旧搭档历史 {number:02}" for number in range(45, 0, -1)],
                "Owner history lost, duplicated or exposed messages")
        guest = check.api("/history", identity=GUEST, view=guest_view)
        require([row["text"] for row in guest["messages"]] == ["合成旧访客历史"], "Guest history crossed scopes")
        set_owner(lab.database, "some-other-owner")
        try:
            check.api("/history", view=owner_view, code=409)
        finally:
            set_owner(lab.database, OWNER)
        require(not check.requests(), "Viewing history unexpectedly invoked the model")
    print("History pagination: 45 owner rows, 1 guest row; withdrawn, previous owner, group and stale view denied.")


def browser(root, gateway_port, model_port):
    playwright = shutil.which("playwright-cli")
    if not playwright:
        raise RuntimeError("Playwright CLI is required for the browser probe")
    lab = Lab(root, gateway_port, model_port)
    command = [playwright, "-s=isla-r2-history"]
    snippet = root / "browser-history.js"
    with WebCheck(lab).serve("browser-history"):
        local = json.loads((root / "identity-tokens.json").read_text(encoding="utf-8"))
        snippet.write_text(BROWSER.replace("__TOKENS__", json.dumps({"owner": local[OWNER], "guest": local[GUEST]})),
                           encoding="utf-8")
        try:
            subprocess.run(command + ["open", f"http://{HOST}:{lab.gateway_port}", "--browser=msedge", "--mobile"],
                           cwd=root, check=True)
            output = subprocess.check_output(command + ["--raw", "run-code", "--filename", str(snippet)],
                                             cwd=root, encoding="utf-8")
            require(json.loads(output).get("roleSwitchIsolated"), "Browser did not prove history isolation")
            (root / "browser-history-result.json").write_text(output, encoding="utf-8")
            print(output.strip())
        finally:
            subprocess.run(command + ["close"], cwd=root, check=False)
            snippet.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=SOURCE / ".stage1" / "r2-validation")
    parser.add_argument("--gateway-port", type=int, default=GATEWAY_PORT)
    parser.add_argument("--model-port", type=int, default=MODEL_PORT)
    parser.add_argument("--browser-root", type=Path, help="Run browser probe against an existing successful fixture")
    args = parser.parse_args()
    if args.browser_root:
        browser(args.browser_root.resolve(), args.gateway_port, args.model_port)
    else:
        main(args.root, args.gateway_port, args.model_port)
