ALTER TABLE conversations ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0,1));
CREATE INDEX conversations_by_pin ON conversations(pinned DESC,updated_at DESC,id);
CREATE TABLE conversation_deletions (
 conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),
 job_id TEXT NOT NULL UNIQUE, manifest_json TEXT NOT NULL,
 phase TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 last_error_code TEXT
);
PRAGMA user_version=3;
