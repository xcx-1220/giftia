"""Provision and run the isolated Stage 1 profile and its loopback smoke endpoint."""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
import shutil
import signal
import subprocess
from threading import Thread
from time import monotonic
from uuid import uuid4

from fixtures import seed
from mock_model import KEY, MODEL, MockModel

SOURCE = Path(__file__).resolve().parent
PROFILE = "isla-test"
BUNDLE = "giftia-isla-harness-bundle"
BUNDLE_VERSION = json.loads((SOURCE / "isla-bundle/package.json").read_text(encoding="utf-8"))["version"]
HARNESS_VERSION = "0.2.2-rc.3"
NODE_VERSION = "v24.11.1"
HOST = "127.0.0.1"
GATEWAY_PORT = 18111
MODEL_PORT = 18112


class TurnCancelled(Exception):
    """The application cancelled a turn before commit."""


class Lab:
    def __init__(self, root, gateway_port=GATEWAY_PORT, model_port=MODEL_PORT, *, live_relay=False, upstream=None):
        self.root = root.resolve()
        if not (1024 <= gateway_port <= 65535 and 1024 <= model_port <= 65535) or gateway_port == model_port:
            raise ValueError("Use distinct non-privileged loopback ports")
        self.gateway_port, self.model_port = gateway_port, model_port
        self.live_relay = live_relay
        self.upstream = upstream is not None
        self.home = self.root / "dsh-home"
        self.workspace = self.root / "workspace"
        self.profile = self.home / "profiles" / PROFILE
        self.pack_dir = self.root / "packages"
        self.tarball = self.pack_dir / f"{BUNDLE}-{BUNDLE_VERSION}.tgz"
        self.capture = self.root / "model-requests.jsonl"
        self.database = self.root / "fixtures" / "giftia-test.db"
        self.workspace.mkdir(parents=True, exist_ok=True)

        self.node = Path(shutil.which("node") or "node")
        if subprocess.check_output([str(self.node), "--version"], text=True).strip() != NODE_VERSION:
            raise RuntimeError(f"Put Node.js {NODE_VERSION} on PATH")
        self.dsh = [str(self.node), str(SOURCE / "node_modules/@deepseek-ai/dsh/lib/bin.js")]
        self.env = {
            key: value for key, value in os.environ.items()
            if key.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG"}
        }
        model_key = KEY
        model_url = f"http://{HOST}:{self.model_port}"
        if live_relay:
            model_key = (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "relay").read_text(encoding="utf-8").strip()
            if not model_key:
                raise ValueError("Missing loopback relay credential")
        if upstream is not None:
            model_url, model_key = upstream
        self.env.update(
            PATH=os.pathsep.join([str(self.node.parent), str(SOURCE / "node_modules/.bin"), os.defpath]),
            HOME=str(self.root), USERPROFILE=str(self.root), DSH_HOME=str(self.home),
            DEEPSEEK_API_KEY=model_key, DEEPSEEK_BASE_URL=model_url,
            npm_config_registry="https://registry.npmjs.org/",
            npm_config_update_notifier="false",
        )

    def command(self, *args, **options):
        return subprocess.run(self.dsh + list(args), cwd=self.workspace, env=self.env,
                              check=True, **options)

    def prepare(self):
        if not self.profile.exists():
            self.command("--profile", PROFILE, "--from-default-profile", "sdk-minimal",
                         "--dump-config", stdout=subprocess.DEVNULL)
        shutil.copyfile(SOURCE / "profile.patch.yml", self.profile / "cordis.patch.yml")
        seed(self.database)

    def pack(self):
        self.pack_dir.mkdir(parents=True, exist_ok=True)
        npm = shutil.which("npm")
        if npm is None:
            raise RuntimeError("Put npm on PATH")
        subprocess.run([npm, "pack", "--ignore-scripts", "--pack-destination", str(self.pack_dir)],
                       cwd=SOURCE / "isla-bundle", env=self.env, check=True)

    def install(self):
        self.command("plugin", "--profile", PROFILE, "add", str(self.tarball))

    def uninstall(self):
        self.command("plugin", "--profile", PROFILE, "remove", BUNDLE)


class Runtime:
    def __init__(self, lab, development, *, role_server=None, role_env=None, patches=()):
        args = lab.dsh + ["--profile", PROFILE]
        if development:
            args += ["--patch", str(SOURCE / "development.patch.yml")]
        for patch in patches:
            args += ["--patch", str(patch)]
        self.role_server = role_server
        self.turn_timeout = 70 if lab.live_relay or lab.upstream else 20
        self.process = subprocess.Popen(args, cwd=lab.workspace, env={**lab.env, **(role_env or {})},
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        text=True, encoding="utf-8", bufsize=1)
        self.messages = Queue()
        self.sequence = 0
        Thread(target=self.read, daemon=True).start()

    def read(self):
        try:
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        except (ValueError, OSError) as error:
            self.messages.put(error)
        finally:
            self.messages.put(RuntimeError("Harness RPC closed"))

    def receive(self, deadline, cancelled=None):
        # While waiting on Harness, the gateway's main thread services the
        # authenticated callbacks to the same SQLite connection.
        while True:
            if cancelled is not None and cancelled():
                raise TurnCancelled
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Harness RPC timed out")
            if self.role_server is not None:
                self.role_server.handle_request()
            try:
                message = self.messages.get(timeout=min(remaining, .025) if self.role_server else remaining)
                break
            except Empty:
                continue
        if isinstance(message, Exception):
            raise message
        return message

    def request(self, method, params, notify=lambda message: None, cancelled=None, *, timeout=20):
        self.sequence += 1
        message = {"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params}
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        deadline = monotonic() + timeout
        while True:
            response = self.receive(deadline, cancelled)
            if response.get("id") == self.sequence:
                if "error" in response:
                    raise RuntimeError(f"{method}: {response['error']}")
                return response.get("result")
            notify(response)

    def turn(self, session, text, *, blocks=None, notify=None, cancelled=None, on_event=None):
        before_receipt = []
        content = [{"type": "text", "text": text}]
        if blocks:
            content.extend(blocks)
        self.request("session/prompt", {"sessionId": session, "contentBlocks": content},
                     before_receipt.append, cancelled)
        deadline = monotonic() + self.turn_timeout
        reply = ""
        finish = None
        messages = iter(before_receipt)
        while True:
            message = next(messages, None) or self.receive(deadline, cancelled)
            params = message.get("params", {})
            if params.get("sessionId") != session:
                continue
            if message.get("method") == "session.status" and params.get("status") == "idle" and finish:
                if finish != "completed" or not reply:
                    raise RuntimeError(f"Synthetic turn ended with {finish}")
                return reply
            event = params.get("event", {})
            data = event.get("data", {})
            if on_event is not None and event:
                on_event(event)
            if notify is not None and event.get("type") in {"tool/call", "tool/result", "todo/write"}:
                notify(event)
            if event.get("type") == "assistant/message":
                content = data.get("message", data).get("content", [])
                reply = "".join(block["text"] for block in content if block.get("type") == "text")
            elif event.get("type") == "turn/end":
                finish = data["reason"]["kind"]

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()


class Gateway(BaseHTTPRequestHandler):
    def output(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/health" and self.server.runtime.process.poll() is None:
            self.output(200, {"profile": PROFILE, "harness": HARNESS_VERSION, "synthetic": True})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/test-turn":
            self.send_error(404)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise ValueError("Invalid body length")
            text = json.loads(self.rfile.read(size))["text"]
            if not isinstance(text, str) or not text.strip() or text.lstrip().startswith("/"):
                raise ValueError("Missing text")
        except (ValueError, KeyError, TypeError):
            self.send_error(400)
            return
        session = f"stage1-{uuid4().hex}"
        try:
            reply = self.server.runtime.turn(session, text)
        except Exception as error:
            print(f"[stage1] {error}", flush=True)
            self.send_error(502, "Harness test turn failed")
            return
        self.output(200, {"session": session, "reply": reply})


def serve(lab, development):
    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    with ThreadingHTTPServer((HOST, lab.model_port), MockModel) as mock:
        mock.capture = lab.capture
        thread = Thread(target=mock.serve_forever, daemon=True)
        thread.start()
        try:
            with HTTPServer((HOST, lab.gateway_port), Gateway) as gateway:
                runtime = Runtime(lab, development)
                try:
                    runtime.request("initialize", {"cwd": str(lab.workspace), "provider": "deepseek-official",
                                                   "model": MODEL, "reasoningEffort": "off", "maxTokens": 900})
                    gateway.runtime = runtime
                    gateway.serve_forever()
                finally:
                    runtime.close()
        except KeyboardInterrupt:
            pass
        finally:
            mock.shutdown()
            thread.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=SOURCE / ".stage1" / "lab")
    parser.add_argument("--gateway-port", type=int, default=GATEWAY_PORT)
    parser.add_argument("--model-port", type=int, default=MODEL_PORT)
    parser.add_argument("--live-relay", action="store_true", help="Use the separately running, budgeted loopback relay")
    parser.add_argument("action", choices=("prepare", "pack", "install", "uninstall", "dump", "serve", "serve-role",
                                           "serve-web"))
    parser.add_argument("--development", action="store_true")
    args = parser.parse_args()
    if args.live_relay and args.action != "serve-web":
        parser.error("--live-relay applies only to serve-web")
    lab = Lab(args.root, args.gateway_port, args.model_port, live_relay=args.live_relay)
    if args.action == "dump":
        lab.command("--profile", PROFILE, "--dump-config")
    elif args.action == "serve":
        serve(lab, args.development)
    elif args.action == "serve-role":
        from role_gateway import serve_role
        serve_role(lab, args.development)
    elif args.action == "serve-web":
        from web_gateway import serve_web
        serve_web(lab, args.development)
    else:
        getattr(lab, args.action)()


if __name__ == "__main__":
    main()
