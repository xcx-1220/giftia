"""Configuration and browser sessions for the HTTPS Harness gateway."""

from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from secrets import compare_digest, token_urlsafe
import sqlite3
from time import time
from urllib.parse import urlsplit

from giftia.config import load_settings

COOKIE = "__Host-isla_session"
SESSION_AGE = 7 * 24 * 60 * 60
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
DIGEST = re.compile(r"[a-f0-9]{64}")


def digest(value):
    return sha256(value.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class WebRelease:
    origin: str
    owner: str
    accounts: dict[str, str]
    settings_file: Path
    persona_file: Path
    legacy: dict | None = None

    @property
    def host(self):
        return urlsplit(self.origin).netloc

    @classmethod
    def load(cls, path):
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise ValueError("Protect the release manifest with mode 0600")
        release = cls(**json.loads(path.read_text(encoding="utf-8")))
        origin = urlsplit(release.origin)
        if (origin.scheme != "https" or not origin.hostname or not origin.hostname.isascii()
                or origin.username is not None or release.origin != f"https://{origin.netloc}"):
            raise ValueError("Expected a single HTTPS origin")
        if (not release.owner or release.owner not in release.accounts or
                not all(isinstance(name, str) and name and name != "web-guest" and DIGEST.fullmatch(value)
                        for name, value in release.accounts.items()) or
                len(set(release.accounts.values())) != len(release.accounts)):
            raise ValueError("Expected distinct per-account credential digests, including the bound owner")
        files = [Path(value) for value in (release.settings_file, release.persona_file)]
        if not all(path.is_absolute() and path.is_file() for path in files):
            raise ValueError("Specify absolute paths to the Giftia settings and persona files")
        return cls(release.origin, release.owner, release.accounts, *files, release.legacy)

    def settings(self, *, rehearsal):
        settings = load_settings(self.settings_file, self.persona_file, force_mock=rehearsal)
        model = settings.model
        if not rehearsal and (model.provider != "deepseek" or not model.name or model.thinking or
                              model.base_url.rstrip("/") != "https://api.deepseek.com"):
            raise ValueError("The effective settings must use the verified DeepSeek endpoint with thinking disabled")
        return settings


def session_cookie(value, age):
    return f"{COOKIE}={value}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age={age}"


class Sessions:
    def __init__(self, database, accounts):
        self.database = database
        self.accounts = accounts
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, principal TEXT NOT NULL, credential TEXT NOT NULL, expires INTEGER NOT NULL
            )""")

    def login(self, token):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return None
        credential = digest(token)
        principal = next((key for key, value in self.accounts.items() if compare_digest(value, credential)), None)
        if principal is None:
            return None
        session = token_urlsafe(32)
        now = int(time())
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (digest(session), principal, credential, now + SESSION_AGE))
        return session

    def identity(self, session):
        if not TOKEN.fullmatch(session):
            return None
        with closing(sqlite3.connect(self.database)) as db:
            row = db.execute("SELECT principal, credential FROM sessions WHERE id=? AND expires>?",
                             (digest(session), int(time()))).fetchone()
        if row and compare_digest(self.accounts.get(row[0], ""), row[1]):
            return row[0]
        return None

    def logout(self, session):
        if TOKEN.fullmatch(session):
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute("DELETE FROM sessions WHERE id=?", (digest(session),))
