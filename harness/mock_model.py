"""Loopback-only DeepSeek-compatible fixture. Record bodies for prompt assertions."""

import json
from http.server import BaseHTTPRequestHandler
from threading import Lock
from time import sleep

MODEL = "deepseek-flash"
REPLY = "艾拉的合成测试回复。"
KEY = "giftia-stage1-mock"


class ScriptedReplies:
    def __init__(self, path):
        self.path = path
        self.lock = Lock()
        self.plan = None
        self.index = 0

    def __call__(self, request):
        if not self.path.exists():
            return {}
        with self.lock:
            plan = json.loads(self.path.read_text(encoding="utf-8"))
            if plan != self.plan:
                self.plan, self.index = plan, 0
            if self.index >= len(plan["actions"]):
                return {}
            action = plan["actions"][self.index]
            texts = [block.get("text") for message in request["messages"] if message["role"] == "user"
                     for block in message["content"] if block["type"] == "text"]
            if action.get("match") not in texts:
                return {}
            self.index += 1
            return action


class MockModel(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/v1/messages":
            self.send_error(404)
            return
        if self.headers.get("x-api-key") != KEY:
            self.send_error(401)
            return
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 512 * 1024:
            self.send_error(413)
            return
        request = json.loads(self.rfile.read(size))
        with self.server.capture.open("a", encoding="utf-8") as log:
            log.write(json.dumps(request, ensure_ascii=False) + "\n")

        reply = getattr(self.server, "reply", lambda request: {})(request)
        if delay := reply.get("delay"):
            sleep(min(delay, 12))
        if code := reply.get("error"):
            self.send_error(code)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.emit("message_start", message={"id": "giftia-synthetic", "type": "message", "role": "assistant",
                                            "model": MODEL, "content": [], "stop_reason": None,
                                            "usage": {"input_tokens": 10, "output_tokens": 0}})
        tools = reply.get("tools", [])
        for index, tool in enumerate(tools):
            self.block(index, {"type": "tool_use", "id": tool["id"], "name": tool["name"], "input": {}},
                       {"type": "input_json_delta", "partial_json": json.dumps(tool["args"])})
        if not tools or "text" in reply:
            self.block(len(tools), {"type": "text", "text": ""},
                       {"type": "text_delta", "text": reply.get("text", REPLY)})
        self.emit("message_delta", delta={"stop_reason": "tool_use" if tools else "end_turn", "stop_sequence": None},
                  usage={"output_tokens": 8})
        self.emit("message_stop")

    def emit(self, event, **payload):
        self.wfile.write(f"event: {event}\ndata: {json.dumps(dict(payload, type=event))}\n\n".encode())

    def block(self, index, start, delta):
        self.emit("content_block_start", index=index, content_block=start)
        self.emit("content_block_delta", index=index, delta=delta)
        self.emit("content_block_stop", index=index)
