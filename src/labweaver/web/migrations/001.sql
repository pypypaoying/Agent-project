CREATE TABLE conversations (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, frozen INTEGER NOT NULL DEFAULT 0 CHECK(frozen IN (0,1))
);
CREATE TABLE runs (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
 request_id TEXT NOT NULL, task TEXT NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 core_session_id TEXT, core_task_id TEXT, report_path TEXT, report_json TEXT,
 error_json TEXT, pending_json TEXT, context_ids TEXT NOT NULL DEFAULT '[]',
 UNIQUE(conversation_id, request_id)
);
CREATE TABLE messages (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
 run_id TEXT REFERENCES runs(id), role TEXT NOT NULL, content TEXT NOT NULL,
 state TEXT NOT NULL, created_at TEXT NOT NULL, question_id TEXT, request_id TEXT,
 UNIQUE(run_id, request_id)
);
CREATE TABLE files (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
 run_id TEXT REFERENCES runs(id), name TEXT NOT NULL, relative_path TEXT NOT NULL UNIQUE,
 media_type TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
 preview_json TEXT, parse_error_json TEXT, category TEXT NOT NULL
);
CREATE TABLE conversation_files (
 conversation_id TEXT NOT NULL REFERENCES conversations(id),
 file_id TEXT NOT NULL UNIQUE REFERENCES files(id), role TEXT NOT NULL,
 position INTEGER NOT NULL, PRIMARY KEY(conversation_id,file_id)
);
CREATE TABLE executions (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
 attempt INTEGER NOT NULL, code_file_id TEXT REFERENCES files(id), record_json TEXT NOT NULL
);
CREATE TABLE artifacts (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
 core_id TEXT NOT NULL, metadata_json TEXT NOT NULL, UNIQUE(run_id,core_id)
);
CREATE TABLE artifact_files (
 artifact_id TEXT NOT NULL REFERENCES artifacts(id), file_id TEXT NOT NULL REFERENCES files(id),
 name TEXT NOT NULL, PRIMARY KEY(artifact_id,name)
);
CREATE TABLE run_events (
 run_id TEXT NOT NULL REFERENCES runs(id), seq INTEGER NOT NULL,
 type TEXT NOT NULL, time TEXT NOT NULL, payload_json TEXT NOT NULL,
 PRIMARY KEY(run_id,seq)
);
CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX messages_by_conversation ON messages(conversation_id,created_at);
CREATE INDEX runs_by_conversation ON runs(conversation_id,created_at);
CREATE UNIQUE INDEX one_csv_per_conversation ON conversation_files(conversation_id) WHERE role='csv';
PRAGMA user_version=1;
