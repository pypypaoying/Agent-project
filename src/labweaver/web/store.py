"""Small synchronous SQLite repository: one connection and short transaction per operation."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid

from .security import AppError, identifier, managed_path


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class Store:
    def __init__(self, root):
        self.root = Path(root).absolute()
        # Verify the root itself before mkdir or opening a database.
        managed_path(self.root, "attachments")
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = managed_path(self.root, "history.sqlite3")
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                script = (Path(__file__).parent / "migrations/001.sql").read_text(encoding="utf-8")
                conn.executescript("BEGIN IMMEDIATE;\n" + script + "\nCOMMIT;")
            elif version != 1:
                raise AppError("schema_version", "数据库版本不兼容，请使用匹配的应用版本。", 503)
            conn.execute("INSERT OR IGNORE INTO app_meta VALUES ('app_id',?)", (uid(),))
            self.app_id = conn.execute("SELECT value FROM app_meta WHERE key='app_id'").fetchone()[0]

    @contextmanager
    def connect(self):
        for name in ("history.sqlite3", "history.sqlite3-wal", "history.sqlite3-shm"):
            managed_path(self.root, name)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def require(self, table, id, conn=None):
        identifier(id)
        if table not in {"conversations", "runs", "files", "artifacts"}:
            raise ValueError("Unsupported table")
        if conn is None:
            with self.connect() as db:
                return self.require(table, id, db)
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (id,)).fetchone()
        if row is None:
            raise AppError("not_found", "记录不存在。", 404)
        return dict(row)

    def new_conversation(self, title="新对话"):
        item = dict(id=uid(), title=title.strip()[:120] or "新对话", created_at=now(), updated_at=now(), frozen=0)
        with self.connect() as db:
            db.execute("INSERT INTO conversations VALUES (:id,:title,:created_at,:updated_at,:frozen)", item)
        return item

    def conversations(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM conversations ORDER BY updated_at DESC")]

    def append_event(self, run_id, kind, payload, *, conn=None):
        if conn is not None:
            run = self.require("runs", run_id, conn)
            seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM run_events WHERE run_id=?", (run_id,)).fetchone()[0]
            time = now()
            conn.execute("INSERT INTO run_events VALUES (?,?,?,?,?)", (run_id, seq, kind, time, encode(payload)))
            return dict(version=1, conversation_id=run["conversation_id"], run_id=run_id, seq=seq, type=kind, time=time, payload=payload)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self.append_event(run_id, kind, payload, conn=db)

    def events(self, run_id, after=0, limit=500):
        run = self.require("runs", run_id)
        with self.connect() as db:
            return [dict(version=1, conversation_id=run["conversation_id"], run_id=run_id,
                         seq=r["seq"], type=r["type"], time=r["time"], payload=json.loads(r["payload_json"]))
                    for r in db.execute("SELECT * FROM run_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?", (run_id, after, limit))]
