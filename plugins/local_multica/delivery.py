"""Durable delivery receipts. Uncertain sends are never automatically replayed."""
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import base64
import hashlib
import sqlite3
import time


MAX_BYTES = 8 * 1024 * 1024


def verify_payload(result: dict) -> str:
    encoded = result.get("image_base64")
    if not isinstance(encoded, str) or len(encoded) > ((MAX_BYTES + 2) // 3) * 4:
        raise ValueError("image payload too large")
    raw = base64.b64decode(encoded, validate=True)
    if not 0 < len(raw) <= MAX_BYTES or len(raw) != result.get("size_bytes"):
        raise ValueError("image size mismatch")
    if hashlib.sha256(raw).hexdigest() != result.get("sha256"):
        raise ValueError("image digest mismatch")
    return encoded


class DeliveryStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS deliveries (
                key TEXT PRIMARY KEY, state TEXT NOT NULL, attempts INTEGER NOT NULL,
                updated REAL NOT NULL, sha256 TEXT, message_id TEXT)""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def status(self, key: str) -> str:
        with self.connect() as db:
            row = db.execute("SELECT state, attempts, updated FROM deliveries WHERE key=?", (key,)).fetchone()
        if not row:
            return "new"
        if row[0] == "failed" and row[1] < 3:
            return "retry" if time.time() - row[2] >= 60 else "cooldown"
        return row[0]

    def begin(self, key: str, sha256: str = "") -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,attempts,updated FROM deliveries WHERE key=?", (key,)).fetchone()
            if row and (row[0] != "failed" or row[1] >= 3 or time.time() - row[2] < 60):
                return False
            db.execute("INSERT OR REPLACE INTO deliveries VALUES (?,?,?,?,?,NULL)",
                       (key, "pending", (row[1] if row else 0) + 1, time.time(), sha256))
        return True

    def finish(self, key: str, result: Any) -> str:
        # Only a documented explicit sent flag is a delivery receipt.
        flag = result.get("sent") if isinstance(result, dict) else None
        state = "sent" if flag is True else "failed" if flag is False else "uncertain"
        with self.connect() as db:
            db.execute("UPDATE deliveries SET state=?, updated=?, message_id=? WHERE key=?",
                       (state, time.time(), str(result.get("message_id") or "") if isinstance(result, dict) else "", key))
        return state
