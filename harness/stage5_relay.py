"""A loopback-only relay with a persistent limit on DeepSeek transport attempts."""

import argparse
from contextlib import closing
from hashlib import sha256
from hmac import compare_digest
from http.client import HTTPSConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
from time import monotonic, time

from mock_model import MODEL

UPSTREAM = "api.deepseek.com"
ENDPOINT = "/anthropic/v1/messages"
MAX_BODY = 384 * 1024


class Ledger:
    def __init__(self, path, limit, days):
        self.path, self.limit, self.days = path, limit, days
        with closing(sqlite3.connect(path)) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY, started REAL NOT NULL, request_hash TEXT NOT NULL,
                outcome TEXT NOT NULL DEFAULT 'reserved', detail TEXT
            )""")

    def reserve(self, body):
        now = time()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            count, first = db.execute("SELECT count(*), min(started) FROM requests").fetchone()
            if count >= self.limit or (first is not None and now >= first + self.days * 86400):
                return None
            return db.execute("INSERT INTO requests(started, request_hash) VALUES (?, ?)",
                              (now, sha256(body).hexdigest())).lastrowid

    def finish(self, attempt, outcome, details):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE requests SET outcome=?, detail=? WHERE id=?",
                       (outcome, json.dumps(details), attempt))


def observe(chunk, details, start):
    if not chunk.startswith(b"data:"):
        return
    try:
        event = json.loads(chunk[5:])
    except ValueError:
        return
    kind = event.get("type")
    if kind == "message_start":
        message = event["message"]
        details.update(model=message.get("model"), messageId=message.get("id"))
    elif kind == "content_block_delta" and event.get("delta", {}).get("text"):
        details.setdefault("firstTextMs", round((monotonic() - start) * 1000, 1))
    elif kind == "message_delta":
        details.update(usage=event.get("usage"), stopReason=event.get("delta", {}).get("stop_reason"))
    elif kind == "message_stop":
        details["completed"] = True


def permitted(payload, pdf_only):
    allowed = {"model", "max_tokens", "system", "messages", "stream", "temperature", "thinking",
               "tools", "tool_choice", "metadata", "top_p", "stop_sequences"}
    if (payload.keys() - allowed or payload["model"] != MODEL
            or payload.get("thinking") != {"type": "disabled"} or payload.get("stream") is not True
            or not 1 <= payload["max_tokens"] <= 900):
        return False
    tools = payload.get("tools") or []
    if pdf_only:
        return not tools and any(block.get("text", "").startswith("PDF 资料（JSON 数据）")
                                 for message in payload["messages"] if message["role"] == "user"
                                 for block in message["content"])
    return all(tool.get("name") in {"remember", "search", "forget", "sample_reference"} for tool in tools)


class Relay(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/v1/messages" or not compare_digest(
            self.headers.get("x-api-key", "").encode(), self.server.relay_key.encode()
        ):
            self.send_error(404)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY:
                raise ValueError
            body = self.rfile.read(size)
            payload = json.loads(body)
            if len(body) != size or not permitted(payload, self.server.pdf_only):
                raise ValueError
            if not payload.get("tools"):
                payload.pop("tools", None)
                payload.pop("tool_choice", None)
                body = json.dumps(payload, ensure_ascii=False).encode()
        except (ValueError, KeyError, TypeError, AttributeError):
            self.send_error(400, "Request outside trial scope")
            return
        attempt = self.server.ledger.reserve(body)
        if attempt is None:
            self.send_error(429, "Trial budget exhausted")
            return
        self.forward(attempt, body)

    def forward(self, attempt, body):
        conn = HTTPSConnection(UPSTREAM, timeout=40)
        start = monotonic()
        details = {}
        sent = False
        outcome = "transport_error"
        try:
            conn.request("POST", ENDPOINT, body, {"Content-Type": "application/json",
                                                "x-api-key": self.server.api_key,
                                                "anthropic-version": "2023-06-01"})
            remote = conn.getresponse()
            details.update(httpStatus=remote.status, providerRequestId=remote.getheader("x-request-id"))
            if remote.status != 200:
                outcome = "upstream_http_error"
                self.send_error(remote.status, "DeepSeek request failed")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            sent = True
            downstream = True
            while chunk := remote.readline(1024 * 1024):
                observe(chunk, details, start)
                if downstream:
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except OSError:
                        downstream = False
            outcome = "completed" if details.get("completed") else "incomplete_stream"
        except (OSError, HTTPException, ValueError) as error:
            details["error"] = type(error).__name__
            if not sent:
                self.send_error(502, "DeepSeek unavailable")
        finally:
            conn.close()
            details["elapsedMs"] = round((monotonic() - start) * 1000, 1)
            self.server.ledger.finish(attempt, outcome, details)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--limit", required=True, type=int)
    parser.add_argument("--days", default=7, type=int)
    parser.add_argument("--pdf-only", action="store_true")
    args = parser.parse_args()
    credential = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "deepseek"
    directory = Path(os.environ["STATE_DIRECTORY"])
    with ThreadingHTTPServer(("127.0.0.1", args.port), Relay) as server:
        server.api_key = credential.read_text(encoding="utf-8").strip()
        server.relay_key = credential.with_name("relay").read_text(encoding="utf-8").strip()
        if not server.api_key or not server.relay_key:
            raise ValueError("Missing provider or relay credential")
        server.pdf_only = args.pdf_only
        server.ledger = Ledger(directory / "usage.db", args.limit, args.days)
        server.serve_forever()


if __name__ == "__main__":
    main()
