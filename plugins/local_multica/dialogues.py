"""Durable, scoped plan bindings. Only authenticated bridge responses write here."""
import sqlite3
from contextlib import contextmanager

from .client import ClientError


class DialogueBindings:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS bindings (
                user_id TEXT NOT NULL, session_id TEXT NOT NULL, dialogue_id TEXT NOT NULL,
                message_id TEXT NOT NULL, turn_id TEXT NOT NULL, version TEXT NOT NULL,
                state TEXT NOT NULL, PRIMARY KEY(user_id, session_id, dialogue_id))''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def remember(self, payload, result):
        if not result.get("dialogue_id"):
            return
        with self.connect() as db:
            db.execute('''INSERT INTO bindings VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(user_id,session_id,dialogue_id) DO UPDATE SET
                message_id=CASE WHEN bindings.message_id='' THEN excluded.message_id ELSE bindings.message_id END,
                turn_id=excluded.turn_id, version=excluded.version, state=excluded.state''',
                (payload['user_id'], payload['session_id'], result['dialogue_id'], payload.get('message_id', ''),
                 result.get('turn_id', ''), result.get('version', ''), result.get('state', '')))

    def resolve(self, payload):
        with self.connect() as db:
            rows = db.execute('''SELECT dialogue_id FROM bindings
                WHERE user_id=? AND session_id=? AND version=?''',
                (payload['user_id'], payload['session_id'], payload['version'])).fetchall()
        if len(rows) > 1:
            raise ClientError("方案版本对应多个讨论；请用pending选择原dialogue_id，再plan审核，不能猜最近任务")
        return rows[0]['dialogue_id'] if rows else ''
