"""Loopback gateway and private Cordis callback transport for the synthetic profile."""

from contextlib import contextmanager
from http.server import HTTPServer, ThreadingHTTPServer
import json
import os
import re
from secrets import compare_digest, token_urlsafe
import signal
from threading import Thread

from mock_model import MODEL, MockModel, ScriptedReplies
from role_service import Conflict, GenerationFailed, RoleService, agent_tools
from stage1 import Gateway, HOST, Runtime, TurnCancelled

IDENTITIES = ("synthetic-owner", "synthetic-guest", "synthetic-visitor", "synthetic-successor")
ADMIN = "fixture-admin"
REQUEST_ID = re.compile(r"[a-zA-Z0-9_-]{1,80}")


def load_tokens(root):
    path = root / "identity-tokens.json"
    try:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as target:
            json.dump({identity: token_urlsafe(32) for identity in (*IDENTITIES, ADMIN)}, target)
    except FileExistsError:
        pass
    return json.loads(path.read_text(encoding="utf-8"))


def body(handler):
    size = int(handler.headers.get("Content-Length", "0"))
    if not 0 < size <= 16_384:
        raise ValueError("Invalid request body length")
    result = json.loads(handler.rfile.read(size))
    if not isinstance(result, dict):
        raise ValueError("Expected an object")
    return result


def auth(handler):
    header = handler.headers.get("Authorization", "")
    return header[7:] if header.startswith("Bearer ") and header.isascii() else ""


class Callbacks(Gateway):
    def do_GET(self):
        self.send_error(404)

    def do_POST(self):
        if not compare_digest(auth(self), self.server.token):
            self.send_error(401)
            return
        try:
            payload = body(self)
            roles = self.server.roles
            if self.path == "/prepare":
                answer = roles.get_context(payload["session"])
            elif self.path == "/authorize":
                answer = {"invalidated": roles.context_invalidated(payload["session"])}
            elif self.path == "/extension-access":
                answer = roles.extension_access(payload["session"])
            elif self.path == "/tool":
                if not isinstance(payload["args"], dict) or not isinstance(payload["callId"], str):
                    raise ValueError("Invalid tool invocation")
                answer = roles.tool(payload["session"], payload["name"], payload["callId"], payload["args"])
            else:
                self.send_error(404)
                return
            self.output(200, answer)
        except Conflict:
            self.send_error(409)
        except (ValueError, KeyError, TypeError):
            self.send_error(400)

    def log_message(self, *_args):
        pass


class RoleGateway(Gateway):
    def do_GET(self):
        # A timed-out runtime is recreated on the next authenticated request.
        if self.server.runtime.runtime is None:
            self.send_error(503)
        else:
            super().do_GET()

    def do_POST(self):
        token = auth(self)
        principal = next((key for key, value in self.server.tokens.items() if compare_digest(value, token)), None)
        if principal is None:
            self.send_error(401)
            return
        roles = self.server.roles
        try:
            if self.path == "/api/test-admin" and principal == ADMIN:
                payload = body(self)
                if set(payload) == {"bind"} and payload["bind"] in IDENTITIES:
                    roles.bind(payload["bind"])
                    self.output(200, {"instance": roles.hub.instance_no})
                elif set(payload) == {"advance"} and type(payload["advance"]) in (int, float):
                    roles.hub.clock.advance(payload["advance"])
                    self.output(200, {"instance": roles.hub.instance_no})
                else:
                    self.send_error(400)
                return
            if principal == ADMIN or self.path not in ("/api/chat", "/api/retract"):
                self.send_error(404)
                return
            payload = body(self)
            expected = {"requestId", "text"} if self.path == "/api/chat" else {"requestId"}
            if payload.keys() != expected or not REQUEST_ID.fullmatch(payload["requestId"]):
                raise ValueError("Invalid request fields")
            if self.path == "/api/retract":
                roles.retract(principal, payload["requestId"])
                self.output(200, {"retracted": True})
                return
            text = payload["text"].strip()
            if not text or len(text) > 4000 or text.startswith("/"):
                raise ValueError("Expected ordinary chat text")
            self.output(200, roles.chat(principal, payload["requestId"], text, self.server.runtime))
        except Conflict as error:
            self.output(409, {"error": str(error)})
        except GenerationFailed as error:
            print(f"[giftia] {error}: {error.__cause__}", flush=True)
            self.output(502, {"error": str(error)})
        except (ValueError, KeyError, TypeError, AttributeError):
            self.send_error(400)


class RoleRuntime:
    """The SDK holds all its agent handles until shutdown. Cap each process's lifetime."""

    def __init__(self, lab, development, callbacks, model, *, startup_timeout=20, patches=()):
        self.lab, self.development, self.callbacks, self.model = lab, development, callbacks, model
        self.runtime = None
        self.attempts = 0
        self.startup_timeout = startup_timeout
        self.patches = patches
        self.start()

    @property
    def process(self):
        return self.runtime.process

    def start(self):
        self.close()
        env = {
            "GIFTIA_ROLE_URL": f"http://{HOST}:{self.callbacks.server_port}",
            "GIFTIA_ROLE_TOKEN": self.callbacks.token,
            "GIFTIA_TOOL_SPECS": json.dumps(agent_tools.TOOL_SPECS),
        }
        self.runtime = Runtime(self.lab, self.development, role_server=self.callbacks, role_env=env,
                               patches=self.patches)
        try:
            self.runtime.request("initialize", {"cwd": str(self.lab.workspace), "provider": "deepseek-official",
                                                "model": self.model.name if self.lab.upstream else MODEL,
                                                "reasoningEffort": "off", "maxTokens": self.model.max_tokens},
                                 timeout=self.startup_timeout)
        except BaseException:
            self.close()
            raise
        self.attempts = 0

    def turn(self, session, text, **options):
        if self.runtime is None or self.process.poll() is not None or self.attempts == 32:
            self.start()
        self.attempts += 1
        try:
            return self.runtime.turn(session, text, **options)
        except (TimeoutError, TurnCancelled):
            self.close()
            raise

    def close(self):
        if self.runtime is not None:
            self.runtime.close()
            self.runtime = None


@contextmanager
def model_server(lab):
    with ThreadingHTTPServer((HOST, lab.model_port), MockModel) as mock:
        mock.capture = lab.capture
        mock.reply = ScriptedReplies(lab.root / "mock-script.json")
        worker = Thread(target=mock.serve_forever, daemon=True)
        worker.start()
        try:
            yield
        finally:
            mock.shutdown()
            worker.join()


def serve_role(lab, development):
    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    with HTTPServer((HOST, 0), Callbacks) as callbacks, HTTPServer((HOST, lab.gateway_port), RoleGateway) as gateway:
        callbacks.timeout = 0
        callbacks.token = token_urlsafe(32)
        roles = RoleService(lab.database, lab.root)
        callbacks.roles = gateway.roles = roles
        gateway.tokens = load_tokens(lab.root)
        try:
            with model_server(lab):
                gateway.runtime = RoleRuntime(lab, development, callbacks, roles.hub.settings.model)
                try:
                    gateway.serve_forever()
                finally:
                    gateway.runtime.close()
        except KeyboardInterrupt:
            pass
        finally:
            roles.store.close()
