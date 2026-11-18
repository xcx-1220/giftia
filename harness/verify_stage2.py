"""Acceptance with a packed native bundle, real Harness loop and synthetic peers."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from urllib.error import HTTPError
from urllib.request import Request
from uuid import uuid4

from mock_model import MODEL, REPLY
from stage1 import GATEWAY_PORT, HOST, SOURCE, Lab
from role_gateway import ADMIN
from verify import OPENER, require, running, verify_package

OWNER, GUEST, VISITOR, SUCCESSOR = ("synthetic-owner", "synthetic-guest", "synthetic-visitor", "synthetic-successor")


class Check:
    def __init__(self, lab):
        self.lab = lab

    def serve(self, name="role"):
        return running(self.lab, name, action="serve-role")

    def post(self, path, body, identity=OWNER, code=200):
        tokens = json.loads((self.lab.root / "identity-tokens.json").read_text(encoding="utf-8"))
        request = Request(f"http://{HOST}:{GATEWAY_PORT}/api/{path}", json.dumps(body).encode(), headers={
            "Authorization": f"Bearer {tokens.get(identity, 'invalid')}", "Content-Type": "application/json",
        })
        try:
            with OPENER.open(request, timeout=50) as response:
                require(response.status == code, f"{path} returned {response.status}, expected {code}")
                return json.load(response)
        except HTTPError as error:
            with error:
                require(error.code == code, f"{path} returned {error.code}, expected {code}: {error.read()!r}")
                return None

    def chat(self, request_id, text, identity=OWNER, code=200):
        return self.post("chat", {"requestId": request_id, "text": text}, identity, code)

    def plan(self, text, *actions):
        script = {"nonce": uuid4().hex, "actions": [{"match": text, **action} for action in actions]}
        (self.lab.root / "mock-script.json").write_text(json.dumps(script), encoding="utf-8")

    def requests(self):
        if not self.lab.capture.exists():
            return []
        return [json.loads(line) for line in self.lab.capture.read_text(encoding="utf-8").splitlines()]

    def sql(self, query, *args):
        with closing(sqlite3.connect(self.lab.database.as_uri() + "?mode=ro", uri=True)) as db:
            return db.execute(query, args).fetchall()

    def affinity(self):
        rows = self.sql("SELECT value FROM state WHERE key='bond:i1:affinity'")
        return float(rows[0][0]) if rows else 0

    def count_turn(self, request_id, identity=OWNER):
        return self.sql("SELECT count(*) FROM harness_turns WHERE request_id=? AND principal=?", request_id, identity)[0][0]

    def memory(self, summary):
        return self.sql("SELECT id,forgotten FROM memories WHERE summary=?", summary)


def tool(name, call_id, **args):
    return {"name": name, "id": call_id, "args": args}


def wire(request):
    return json.dumps(request, ensure_ascii=False)


def includes(request, *texts):
    for text in texts:
        require(text in wire(request), f"Missing model-visible text {text!r}")


def excludes(request, *texts):
    for text in texts:
        require(text not in wire(request), f"Unexpected model-visible text {text!r}")


def dialogue(check):
    text, answer = "把合成银铃放在桌上RECENT_A", "我看懂你的合成银铃了REPLY_A。"
    with check.serve():
        check.chat("auth", "身份验证", "missing", 401)
        check.post("chat", {"requestId": "spoof", "text": "伪造", "identity": OWNER}, GUEST, 400)
        check.plan(text, {"text": answer})
        first = check.chat("first", text)
        require(first["reply"] == answer and first["instance"] == 1, "Reply did not come from the mock model")
        gain = check.affinity()
        snapshot = len(check.requests())
        dup = check.chat("first", text)
        require(dup["replayed"] and dup["reply"] == answer and dup["session"] == first["session"], "Duplicate did not replay")
        require(check.affinity() == gain > 0 and len(check.requests()) == snapshot, "Replay changed state or called model")
        check.chat("first", text + "更改", code=409)
        check.post("test-admin", {"advance": 24}, ADMIN)
        second = check.chat("follow", "你还记得纸灯笼放在哪里吗？")
        require(second["session"] != first["session"], "Harness session was reused")
        request = check.requests()[-1]
        require(request["model"] == MODEL and request["max_tokens"] == 900 and request["temperature"] == 1.15
                and request["thinking"]["type"] == "disabled", "Model settings differ from B baseline")
        require("你安静、认真，有一点青涩" in request["system"], "B core missing")
        require("合成搭档把纸灯笼放在窗边" in request["system"], "Giftia long-term recall missing")
        require(any(m["role"] == "user" and text in wire(m) for m in request["messages"]), "Recent user role missing")
        require(any(m["role"] == "assistant" and answer in wire(m) for m in request["messages"]), "Recent assistant role missing")
        require(check.affinity() > gain and check.count_turn("first") == 1, "Relationship or durable ledger mismatch")
        require(check.sql("SELECT count(*) FROM messages WHERE content IN (?,?)", text, answer) == [(2,)], "Duplicate message writes")


def tools(check):
    text = "请记下合成试验用的三项喜好TOOLS_A"
    a, b, c = "搭档爱喝榛果茶TOOL_A", "搭档偏好海盐饼干TOOL_B", "搭档收藏赤陶盘TOOL_C"
    with check.serve():
        check.plan(text, {"tools": [tool("remember", "call-a", content=a, importance=.9),
                                    tool("remember", "call-b", content=b, importance=.9),
                                    tool("remember", "call-c", content=c)]},
                         {"tools": [tool("search", "call-search", query="榛果茶")]}, {"text": "好，我有留意。"})
        require(check.chat("tools", text)["reply"] == "好，我有留意。", "Harness did not complete tool loop")
        calls = check.requests()
        require(len(calls) == 3, f"Expected three model requests, got {len(calls)}")
        require({item["name"] for item in calls[0]["tools"]} == {"remember", "search", "forget"}, "Wrong tools advertised")
        includes(calls[1], "记下了", "这一轮记得够多了")
        includes(calls[2], a)
        require(not calls[2].get("tools"), "Final generation still advertised tools")
        require(len(check.memory(a)) == len(check.memory(b)) == 1 and not check.memory(c), "Tool write quota not enforced")
        require(check.sql("SELECT count(*) FROM memory_evidence e JOIN memories m ON m.id=e.memory_id "
                          "JOIN messages s ON s.id=e.message_id WHERE m.summary=? AND s.content=?", a, text) == [(1,)],
                "Memory evidence did not reference the turn's user message")


def privacy(check):
    text = "我叫冬青。合成搭档约定紫色船票OWNER_SECRET"
    derived = "搭档的合成约定保存在紫色船票DERIVED_SECRET"
    with check.serve():
        check.plan(text, {"tools": [tool("remember", "call-retract", content=derived, importance=.9)]},
                   {"text": "我收到那张船票PARAPHRASE_SECRET。"})
        check.chat("reuse", text)
        gain = check.affinity()
        check.chat("reuse", "这是第一位访客的独有消息GUEST_A", GUEST)
        guest = check.requests()[-1]
        excludes(guest, "冬青", "OWNER_SECRET", "DERIVED_SECRET", "PARAPHRASE_SECRET")
        require(not guest.get("tools"), "Guest received memory tools")
        check.chat("reuse", "我是另一位访客。", VISITOR)
        excludes(check.requests()[-1], "GUEST_A", "OWNER_SECRET", "DERIVED_SECRET", "冬青")
        check.chat("guest-follow", "你还记得我刚才说了什么吗？", GUEST)
        includes(check.requests()[-1], "GUEST_A")
        require(check.affinity() == gain, "Guests advanced the partner relationship")
        check.post("retract", {"requestId": "reuse"})
        check.chat("reuse", text, code=409)
        check.chat("after-retract", "新的合成话题AFTER_RETRACT_NEW")
        excludes(check.requests()[-1], "冬青", "OWNER_SECRET", "DERIVED_SECRET", "PARAPHRASE_SECRET", "GUEST_A")
        require(check.sql("SELECT count(*) FROM messages WHERE recalled=1") == [(2,)], "Both halves of the withdrawn turn must be revoked")
        check.post("retract", {"requestId": "reuse"})
        check.chat("after-repeated-retract", "接着刚才聊。")
        includes(check.requests()[-1], "AFTER_RETRACT_NEW")


def forgetting(check):
    remember, search, forget = "请记一个合成票夹REMEMBER", "查一下合成票夹SEARCH", "请放下先前那条记录FORGET"
    secret = "搭档的合成纪念票夹是雾紫色FADE_SECRET"
    with check.serve():
        check.plan(remember, {"tools": [tool("remember", "call-remember", content=secret, importance=.9)]})
        check.chat("remember", remember)
        [(memory_id, _)] = check.memory(secret)
        check.plan(search, {"tools": [tool("search", "call-find", query="合成纪念票夹")]},
                   {"text": "上次那条结果包含FADE_ECHO。"})
        check.chat("search", search)
        includes(check.requests()[-1], "FADE_SECRET", "call-find")
        before = len(check.requests())
        check.plan(forget, {"tools": [tool("forget", "call-forget", memory_id=memory_id)]}, {"text": "嗯，我把它放下了。"})
        check.chat("forget", forget)
        attempts = check.requests()[before:]
        require(len(attempts) == 2, "Forget must restart exactly once with rebuilt context")
        excludes(attempts[1], "FADE_SECRET", "FADE_ECHO", "call-find", "call-forget")
        require(check.memory(secret) == [(memory_id, 1)], "Forget mutation did not commit")
        check.chat("search", search, code=409)
        check.chat("following", "还有别的事要聊。")
        excludes(check.requests()[-1], "FADE_SECRET", "FADE_ECHO", "call-find", "call-forget")


def lifecycle(check):
    with check.serve():
        old = "我叫松岚。前任的私事PREVIOUS_OWNER"
        check.chat("old", old)
        check.post("test-admin", {"bind": SUCCESSOR}, ADMIN)
        check.chat("old", old, code=409)
        check.chat("new-owner", "我是这一任当前的搭档。", SUCCESSOR)
        excludes(check.requests()[-1], "松岚", "PREVIOUS_OWNER", "合成搭档把纸灯笼放在窗边")
        check.post("test-admin", {"advance": 81921}, ADMIN)
        ended = check.chat("terminal", "到期前的最后一轮FINAL_INSTANCE1", SUCCESSOR)
        require(ended["instance"] == 1 and "#1 → #2" in ended["recycle"], "Expired owner turn did not recycle")
        require(check.sql("SELECT value FROM meta WHERE key='instance_no'") == [("2",)], "New instance not persisted")
        check.chat("terminal", "到期前的最后一轮FINAL_INSTANCE1", SUCCESSOR, 409)
        check.post("test-admin", {"bind": SUCCESSOR}, ADMIN)
        require(check.chat("new-instance", "新的合成问候。", SUCCESSOR)["instance"] == 2, "Wrong new-instance binding")
        excludes(check.requests()[-1], "PREVIOUS_OWNER", "FINAL_INSTANCE1", "松岚", "合成搭档把纸灯笼放在窗边")
    # Recreate durable state at a stop immediately after instance advancement.
    with closing(sqlite3.connect(check.lab.database)) as db, db:
        db.execute("UPDATE harness_turns SET recycle_pending=1 WHERE request_id='terminal'")
        db.execute("UPDATE state SET value='旧称呼STALE_NAME' WHERE key='handler_name'")
    with check.serve("resume-recycle"):
        require(check.sql("SELECT value FROM state WHERE key='handler_id'") == [("",)], "Old binding survived interrupted recycle")
        require(check.sql("SELECT recycle_pending FROM harness_turns WHERE request_id='terminal'") == [(0,)], "Recycle did not settle")
        check.chat("resume-recycle", "重新启动后的合成问候。", SUCCESSOR)
        excludes(check.requests()[-1], "STALE_NAME", "FINAL_INSTANCE1")
        require(not check.requests()[-1].get("tools"), "Old owner still had private tools after recycle")


def recovery(check):
    text, value = "失败后请重试本轮RETRY_INPUT", "合成失败重试记忆RETRY_MEMORY"
    first_text = "重启前的已提交对话RESTART_ORIGINAL"
    with check.serve("before-restart"):
        first = check.chat("committed", first_text)
        gain = check.affinity()
        check.post("test-admin", {"advance": 24}, ADMIN)
        check.plan(text, {"tools": [tool("remember", "call-failed", content=value)]}, {"error": 503})
        check.chat("retry", text, code=502)
        require(check.count_turn("retry") == 0 and not check.memory(value) and check.affinity() == gain,
                "Failed model attempt left a partial durable turn")
        require(check.sql("SELECT count(*) FROM messages WHERE content=?", text) == [(0,)], "Failed input was retained")
    with check.serve("after-restart"):
        before = len(check.requests())
        dup = check.chat("committed", first_text)
        require(dup["replayed"] and dup["session"] == first["session"] and len(check.requests()) == before,
                "Restart lost the idempotent result")
        check.plan(text, {"tools": [tool("remember", "call-retry", content=value)]}, {"text": REPLY})
        result = check.chat("retry", text)
        require(result["reply"] == REPLY and check.count_turn("retry") == 1 and len(check.memory(value)) == 1,
                "Retried request did not commit exactly once")
        require(check.affinity() > gain and check.sql("SELECT count(*) FROM messages WHERE content=?", text) == [(1,)],
                "Retried relationship or message accounting is inconsistent")
        includes(check.requests()[before], "RESTART_ORIGINAL")
        excludes(check.requests()[before], "call-failed", "RETRY_MEMORY")
        require(check.chat("retry", text)["replayed"], "Successful retry did not become replayable")


def main():
    cases = {case.__name__: case for case in (dialogue, tools, privacy, forgetting, lifecycle, recovery)}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=SOURCE / ".stage1" / "stage2-validation")
    parser.add_argument("--case", choices=cases, action="append")
    args = parser.parse_args()
    root = args.root / uuid4().hex[:10]
    print(f"Evidence directory: {root}", flush=True)
    failures = []
    for name in args.case or cases:
        lab = Lab(root / name)
        try:
            lab.prepare()
            lab.pack()
            verify_package(lab)
            lab.install()
            cases[name](Check(lab))
        except Exception as error:
            failures.append(name)
            print(f"FAIL {name}: {error}", flush=True)
        else:
            print(f"PASS {name}", flush=True)
    require(not failures, f"Failed cases: {', '.join(failures)}")


if __name__ == "__main__":
    main()
