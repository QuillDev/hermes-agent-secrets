"""Where secrets live: one encrypted SQLite file per Hermes home (each profile has its own).

Values are encrypted with Fernet under a key kept beside the database, readable only by this
account. That guards copies of the file (backups, a synced folder), not the account itself:
anything running as the account can read both, which is the point (the agent's commands use them).
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet

# Env-var shaped, so `hermes secret run NAME -- cmd` can hand a value over as $NAME.
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MAX_VALUE = 64 * 1024
MAX_NOTE = 500
# Who put a value here: the person, or the agent storing something it made.
ORIGINS = {"you", "agent"}
DIR = "agent-secrets"

SCHEMA = """
CREATE TABLE IF NOT EXISTS secrets (
    name TEXT PRIMARY KEY,
    value BLOB NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


def check_name(name):
    name = str(name or "").strip()
    if not NAME.match(name):
        raise ValueError("A secret's name is letters, digits and _ (like STRIPE_KEY), up to 64 characters")
    return name


def _private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


class Vault:
    def __init__(self, home, clock=time.time):
        self.dir = _private_dir(Path(home) / DIR)
        self.path = self.dir / "vault.db"
        self.key_path = self.dir / "vault.key"
        # Touched on every change, so output masking rereads values only after one.
        self.revision_path = self.dir / "revision"
        self.clock = clock
        self.lock = threading.Lock()
        self.fernet = Fernet(self._key())
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        os.chmod(self.path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def _key(self):
        try:
            return self.key_path.read_bytes().strip()
        except FileNotFoundError:
            key = Fernet.generate_key()
            try:
                fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return self.key_path.read_bytes().strip()
            with os.fdopen(fd, "wb") as handle:
                handle.write(key + b"\n")
            return key

    def _run(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def revision(self):
        try:
            return self.revision_path.stat().st_mtime_ns
        except OSError:
            return None

    def _changed(self):
        self.revision_path.write_text(f"{self.clock()}\n")

    def names(self):
        rows = self._run("SELECT name, note, origin, created_at, updated_at FROM secrets ORDER BY name COLLATE NOCASE")
        return [{"name": r[0], "note": r[1], "origin": r[2], "created_at": r[3], "updated_at": r[4]} for r in rows]

    def about(self, name):
        """A secret's name, note and dates (never the value), or None."""
        name = check_name(name)
        return next((s for s in self.names() if s["name"] == name), None)

    def get(self, name):
        rows = self._run("SELECT value FROM secrets WHERE name=?", (check_name(name),))
        return self.fernet.decrypt(rows[0][0]).decode() if rows else None

    def values(self):
        """Every (name, value), for masking them in tool output."""
        return [(name, self.fernet.decrypt(value).decode()) for name, value in self._run("SELECT name, value FROM secrets")]

    def put(self, name, value, *, note=None, origin="you"):
        name = check_name(name)
        if not isinstance(value, str) or not value:
            raise ValueError("The secret is empty")
        if len(value.encode()) > MAX_VALUE:
            raise ValueError("Secrets must be under 64 KiB")
        if origin not in ORIGINS:
            raise ValueError("bad origin")
        # No note keeps the one already stored.
        note = None if note is None else str(note).strip()[:MAX_NOTE]
        now = self.clock()
        self._run(
            "INSERT INTO secrets (name, value, note, origin, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, origin=excluded.origin, updated_at=excluded.updated_at, "
            "note=CASE WHEN ? IS NULL THEN secrets.note ELSE excluded.note END",
            (name, self.fernet.encrypt(value.encode()), note or "", origin, now, now, note),
        )
        self._changed()
        return self.about(name)

    def remove(self, name):
        removed = bool(self._run("DELETE FROM secrets WHERE name=? RETURNING name", (check_name(name),)))
        if removed:
            self._changed()
        return removed
