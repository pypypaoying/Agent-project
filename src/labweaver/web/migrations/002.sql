CREATE TABLE conversation_models (
 conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),
 selection_json TEXT NOT NULL
);
CREATE TABLE run_models (
 run_id TEXT PRIMARY KEY REFERENCES runs(id),
 selection_json TEXT NOT NULL
);
PRAGMA user_version=2;
