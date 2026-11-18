"""Exercise release login and isolation through a local TLS reverse proxy."""

import argparse
from contextlib import contextmanager
from hashlib import sha256
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import shutil
import ssl
import subprocess
import sys
from threading import Thread
from time import monotonic, sleep, time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from release_config import COOKIE, digest
from giftia.push import LastMessage, generate_keys
from role_service import RoleService
from stage1 import GATEWAY_PORT, HOST, MODEL_PORT, Lab, SOURCE
from stage5_probe import prepare
from verify import require
from verify_r2_history import seed
from verify_stage4 import GUEST, OWNER, WebCheck

VISITOR = "synthetic-visitor"
OLD_REQUEST = "old-release-completed"
MISSING_REQUEST = "old-release-unconfirmed"
OLD_TEXT = "旧请求的合成消息"
OLD_REPLY = "旧请求的合成答复"
OLD_PRINCIPALS = {principal: sha256(principal.encode()).hexdigest() for principal in (OWNER, GUEST)}


def seed_legacy(lab):
    key = f"{OLD_PRINCIPALS[OWNER]}:{OLD_REQUEST}"
    missing = f"{OLD_PRINCIPALS[GUEST]}:{MISSING_REQUEST}"
    roles = RoleService(lab.database, lab.root)
    try:
        hub = roles.hub
        stamp = hub.clock.iso()
        for role, value, suffix in (("user", OLD_TEXT, ""), ("assistant", OLD_REPLY, ":reply")):
            roles.store.add_message(f"i{hub.instance_no}-m-20261117", hub.instance_no, role, value, stamp,
                                    mid="web:" + key + suffix, scope="private")
        epoch = [hub.instance_no, hub.handler_id, hub.handler_bound_at, hub._int_meta("handler_bound_msg_id")]
    finally:
        roles.store.close()
    ledger = lab.root / "legacy-web-requests.json"
    ledger.touch(mode=0o600)
    ledger.write_text(json.dumps({key: {"principal": OLD_PRINCIPALS[OWNER], "request_id": OLD_REQUEST,
                                        "created": time(), "role": "owner", "user_id": "", "status": "running",
                                        "text": OLD_TEXT},
                                  missing: {"principal": OLD_PRINCIPALS[GUEST], "request_id": MISSING_REQUEST,
                                            "created": time(), "role": "friend", "user_id": GUEST, "status": "queued",
                                            "text": "旧访客待处理"}}), encoding="utf-8")
    return {"epoch": epoch, "principals": OLD_PRINCIPALS, "ledger": str(ledger)}


class TLSProxy(BaseHTTPRequestHandler):
    def forward(self):
        backend = HTTPConnection(HOST, self.server.gateway_port, timeout=10)
        headers = {key: self.headers[key] for key in (
            "Host", "Origin", "Content-Type", "Content-Length", "Cookie", "Authorization", "X-Conversation"
        ) if key in self.headers}
        headers["X-Forwarded-Proto"] = "https"
        payload = self.rfile.read(int(headers.get("Content-Length", 0)))
        try:
            backend.request(self.command, self.path, body=payload, headers=headers)
            response = backend.getresponse()
            data = response.read()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in ("server", "date", "connection"):
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)
        except ConnectionError:
            self.close_connection = True
        finally:
            backend.close()

    do_GET = do_POST = forward

    def log_message(self, *_args):
        pass


@contextmanager
def tls_proxy(lab, openssl, handler=TLSProxy):
    certificate, key = lab.root / "test-cert.pem", lab.root / "test-key.pem"
    subprocess.run([openssl, "req", "-newkey", "rsa:2048", "-x509", "-nodes", "-days", "1",
                    "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                    "-keyout", str(key), "-out", str(certificate)], check=True, capture_output=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    with ThreadingHTTPServer((HOST, 0), handler) as proxy:
        proxy.gateway_port = lab.gateway_port
        proxy.socket = context.wrap_socket(proxy.socket, server_side=True)
        thread = Thread(target=proxy.serve_forever)
        thread.start()
        try:
            yield f"https://{HOST}:{proxy.server_port}", certificate
        finally:
            proxy.shutdown()
            thread.join()


@contextmanager
def run_candidate(lab, manifest, label, api, push_key=None):
    command = [sys.executable, str(SOURCE / "serve_release.py"), "--rehearse", "--root", str(lab.root),
               "--database", str(lab.database), "--manifest", str(manifest), "--gateway-port", str(lab.gateway_port),
               "--model-port", str(lab.model_port)]
    log_path = lab.root / f"{label}.log"
    env = os.environ.copy()
    if push_key:
        command.extend(("--legacy-data", str(lab.database.parent)))
        env["VAPID_PRIVATE_KEY"] = push_key
    with log_path.open("wb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env)
        try:
            deadline = monotonic() + 50
            while True:
                require(process.poll() is None and monotonic() < deadline, "HTTPS release did not become ready")
                try:
                    api("/api/health")
                    break
                except (OSError, URLError):
                    sleep(.2)
            yield
        except BaseException:
            print(log_path.read_text(encoding="utf-8", errors="replace"), file=sys.stderr)
            raise
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def client(origin, cert):
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context(cafile=str(cert))))

    def api(path, *, token=None, headers=None, expected=200, method=None, json_body=None, raw=False):
        if token is not None:
            json_body = {"token": token}
        headers = dict(headers or {})
        if json_body is not None or method == "POST":
            headers.setdefault("Origin", origin)
        data = None
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(json_body).encode()
        request = Request(origin + path, data=data, headers=headers, method=method)
        try:
            response = opener.open(request, timeout=8)
        except HTTPError as error:
            response = error
        with response:
            require(response.status == expected, f"{path}: expected {expected}, got {response.status}")
            if raw:
                return response.read(), response.headers.get_content_type()
            value = json.load(response)
            return value, response.headers.get("Set-Cookie")

    return api


def sign_in(api, token):
    _, cookie = api("/api/web/login", token=token)
    session = SimpleCookie(cookie)[COOKIE]
    require(session["secure"] and session["httponly"] and session["samesite"] == "Strict"
            and session["path"] == "/" and not session["domain"] and token not in cookie,
            "HTTPS session cookie flags or contents are invalid")
    return {"Cookie": f"{COOKIE}={session.value}"}


def browser_handoff(lab, origin, tokens):
    cli = shutil.which("playwright-cli")
    require(cli, "Playwright CLI is required for the Edge probe")
    fixture = {"origin": origin, "ownerToken": tokens[OWNER], "guestToken": tokens[GUEST],
               "ownerView": OLD_PRINCIPALS[OWNER][:20], "guestView": OLD_PRINCIPALS[GUEST][:20],
               "request": OLD_REQUEST, "missing": MISSING_REQUEST, "expectedReply": OLD_REPLY}
    code = """async page => {
      const f = __FIXTURE__;
      const context = await page.context().browser().newContext({ignoreHTTPSErrors: true, viewport:{width:390,height:844}});
      try {
        const tab = await context.newPage();
        await tab.goto(f.origin);
        await tab.evaluate(f => {
          localStorage.setItem(`ella.history.${f.ownerView}`, JSON.stringify([{role:'her', text:'此设备的搭档旧记录'}]));
          localStorage.setItem(`ella.history.${f.guestView}`, JSON.stringify([{role:'her', text:'此设备的访客旧记录'}]));
          localStorage.setItem(`ella.pending.${f.ownerView}`, JSON.stringify({request_id:f.request}));
          localStorage.setItem(`ella.pending.${f.guestView}`, JSON.stringify({request_id:f.missing}));
        }, f);
        const login = async token => {
          await tab.getByLabel('会话凭据').fill(token);
          await tab.getByRole('button', {name:'进入', exact:true}).click();
          await tab.getByRole('button', {name:'历史', exact:true}).click();
        };
        await login(f.ownerToken);
        await tab.getByText('此设备的搭档旧记录', {exact:true}).waitFor();
        await tab.locator('#legacy-history').getByText(f.expectedReply, {exact:true}).waitFor();
        if (await tab.getByText('此设备的访客旧记录', {exact:true}).count()) throw Error('Guest local text leaked');
        await tab.getByRole('button', {name:'关闭', exact:true}).click();
        await tab.getByRole('button', {name:'退出', exact:true}).click();
        await login(f.guestToken);
        await tab.getByText('此设备的访客旧记录', {exact:true}).waitFor();
        await tab.getByText('这条旧请求尚未确认，已保留原记录。', {exact:true}).waitFor();
        if (await tab.getByText('此设备的搭档旧记录', {exact:true}).count()) throw Error('Owner local text leaked');
        const retained = await tab.evaluate(view => localStorage.getItem(`ella.pending.${view}`), f.guestView);
        if (JSON.parse(retained).request_id !== f.missing) throw Error('Unconfirmed old request was removed');
        return {ownerReceipt:true, guestPendingPreserved:true, localHistoryIsolated:true};
      } finally {
        await context.close();
      }
    }"""
    path = lab.root / "legacy-browser.js"
    path.write_text(code.replace("__FIXTURE__", json.dumps(fixture)), encoding="utf-8")
    command = [cli, "-s=isla-legacy-https"]
    try:
        subprocess.run(command + ["open", "about:blank", "--browser=msedge"], cwd=lab.root, check=True)
        run = subprocess.run(command + ["--raw", "run-code", "--filename", str(path)], cwd=lab.root,
                             encoding="utf-8", capture_output=True)
        require(run.returncode == 0, run.stderr or run.stdout)
        result = json.loads(run.stdout)
        require(all(result.get(key) for key in ("ownerReceipt", "guestPendingPreserved", "localHistoryIsolated")),
                "Edge did not verify the legacy handoff")
        (lab.root / "legacy-browser-result.json").write_text(run.stdout, encoding="utf-8")
        print(run.stdout.strip(), flush=True)
    finally:
        subprocess.run(command + ["close"], cwd=lab.root, check=False)
        path.unlink(missing_ok=True)


def verify_assets(api):
    root = SOURCE.parent / "web"
    for path, content_type in (("/.well-known/assetlinks.json", "application/json"),
                               ("/manifest.json", "application/manifest+json"),
                               ("/icons/icon-192.png", "image/png"), ("/icons/icon-512.png", "image/png")):
        data, actual_type = api(path, raw=True)
        require(data == (root / path.lstrip("/")).read_bytes() and actual_type == content_type,
                f"Public install asset mismatch: {path}")
    html, _ = api("/", raw=True)
    require(b'rel="manifest" href="/manifest.json"' in html, "The page does not link its manifest")
    print("PASS unsigned browser requests reached the TWA association, manifest and icons over TLS", flush=True)


def verify_continuity(api, owner, guest, public_key):
    status, _ = api('/api/web/push/status', headers=owner)
    require(status == {'publicKey': public_key}, 'Active owner lost the established notification key')
    notice, _ = api('/api/push/last', headers=owner)
    require(notice.get('text') == 'Synthetic notification', 'Previous notification was not retained')
    api('/api/push/last', headers=guest, expected=403)
    api('/api/push/last', expected=401)
    subscription = {'endpoint': 'https://fcm.googleapis.com/fcm/send/r2-fixture', 'keys': {}}
    api('/api/web/push/subscribe', headers=guest, json_body=subscription, expected=403)
    api('/api/web/push/subscribe', headers=owner, json_body=subscription)
    print('PASS owner-only push setup, previous notification and visitor denial; no push sent', flush=True)


def test(lab, openssl, browser, assets_only, continuity):
    prepare(lab)
    seed(lab)
    legacy = seed_legacy(lab)
    check = WebCheck(lab)
    config = SOURCE.parent / "config"
    tokens = {principal: token_urlsafe(32) for principal in (OWNER, GUEST, VISITOR)}
    private_key, public_key = generate_keys() if continuity else (None, None)
    if continuity:
        LastMessage(lab.database.parent / 'push_last.json').set('Synthetic notification')
    with tls_proxy(lab, openssl) as (origin, certificate):
        api = client(origin, certificate)
        manifest = lab.root / "release.json"
        value = {"origin": origin, "owner": OWNER, "accounts": {key: digest(token) for key, token in tokens.items()},
                 "legacy": legacy,
                 "settings_file": str(config / "settings.toml"), "persona_file": str(config / "persona.toml")}
        manifest.touch(mode=0o600)
        manifest.write_text(json.dumps(value), encoding="utf-8")
        with run_candidate(lab, manifest, "https-first", api, private_key):
            verify_assets(api)
            if assets_only:
                return
            api("/api/web/state", headers={"Cookie": "aila_auth=owner.synthetic; aila_visitor=synthetic-guest",
                                           "Authorization": f"Bearer {tokens[OWNER]}"}, expected=401)
            api("/api/web/login", token=tokens[OWNER], headers={"Origin": "https://unrelated.invalid"}, expected=403)
            api("/api/web/login", token=token_urlsafe(32), expected=401)
            owner = sign_in(api, tokens[OWNER])
            guest = sign_in(api, tokens[GUEST])
            visitor = sign_in(api, tokens[VISITOR])
            for headers, role, pages in ((owner, "搭档", 40), (guest, "访客", 1), (visitor, "访客", 0)):
                state, _ = api("/api/web/state", headers=headers)
                require(state["role"] == role, "Account mapped to the wrong role")
                headers["X-Conversation"] = state["conversation"]
                history, _ = api("/api/web/history", headers=headers)
                require(len(history["messages"]) == pages, "History crossed account boundaries")
            if continuity:
                verify_continuity(api, owner, guest, public_key)
            for principal, headers in ((OWNER, owner), (GUEST, guest), (VISITOR, visitor)):
                state, _ = api("/api/web/state", headers=headers)
                want = OLD_PRINCIPALS[principal][:20] if principal in OLD_PRINCIPALS else None
                require(state["legacyView"] == want, "Legacy view crossed account boundaries")
            result, _ = api(f"/api/web/legacy-result/{OLD_REQUEST}", headers=owner)
            require(result == {"text": OLD_TEXT, "reply": OLD_REPLY}, "Old running request did not recover")
            api(f"/api/web/legacy-result/{OLD_REQUEST}", headers=guest, expected=404)
            api(f"/api/web/legacy-result/{MISSING_REQUEST}", headers=guest, expected=404)
            for headers, request_id, prompt in ((owner, OLD_REQUEST, OLD_TEXT), (guest, MISSING_REQUEST, "旧访客待处理")):
                api("/api/web/turns", headers=headers, expected=409,
                    json_body={"requestId": request_id, "text": prompt, "attachmentId": None})
            if browser:
                browser_handoff(lab, origin, tokens)
            api("/api/web/history", headers={**guest, "X-Conversation": owner["X-Conversation"]}, expected=409)
            api("/api/web/logout", method="POST", headers=guest)
            api("/api/web/state", headers=guest, expected=401)
            require(not check.requests(), "Login or history called the model")
        with run_candidate(lab, manifest, "https-restart", api, private_key):
            api("/api/web/history", headers=owner)
            api("/api/web/state", headers=guest, expected=401)
            prompt = "这是隔离 HTTPS 验收的普通聊天。"
            check.plan(prompt, {"text": "模拟回复已收到。"})
            request_id = "https-release-turn"
            api("/api/web/turns", headers=owner, expected=202,
                json_body={"requestId": request_id, "text": prompt, "attachmentId": None})
            deadline = monotonic() + 10
            while monotonic() < deadline:
                state, _ = api("/api/web/state", headers=owner)
                if any(item["id"] == request_id and item.get("reply") == "模拟回复已收到。" for item in state["items"]):
                    break
                sleep(.1)
            else:
                raise RuntimeError("Ordinary chat did not finish behind HTTPS")
            require(len(check.requests()) == 1, "Expected one simulated model request")
    print("PASS TLS authentication, three isolated accounts, old receipt recovery (0 calls), restart and ordinary chat (1 mock call)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openssl", default=shutil.which("openssl"), required=shutil.which("openssl") is None)
    parser.add_argument("--root", type=Path, default=SOURCE / ".stage1" / "r2-validation")
    parser.add_argument("--gateway-port", type=int, default=GATEWAY_PORT)
    parser.add_argument("--model-port", type=int, default=MODEL_PORT)
    cases = parser.add_mutually_exclusive_group()
    cases.add_argument("--browser", action="store_true")
    cases.add_argument("--assets-only", action="store_true")
    parser.add_argument("--continuity", action="store_true")
    args = parser.parse_args()
    root = args.root / ("https-" + uuid4().hex[:10])
    print(f"Evidence directory: {root}", flush=True)
    test(Lab(root, args.gateway_port, args.model_port), args.openssl, args.browser, args.assets_only, args.continuity)
