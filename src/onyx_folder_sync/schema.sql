-- State schema, version 1. Frozen contract: only the orchestrator changes it.
-- state.py applies this file to a new database and then sets
-- PRAGMA user_version = 1. Later versions add numbered migrations in state.py.

CREATE TABLE install (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    instance_id TEXT NOT NULL
);

CREATE TABLE root (
    root_id       TEXT PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    path          TEXT NOT NULL,
    cc_pair_id    INTEGER,
    connector_id  INTEGER,
    credential_id INTEGER
);

CREATE TABLE document (
    root_id           TEXT NOT NULL REFERENCES root (root_id) ON DELETE CASCADE,
    rel_path          TEXT NOT NULL,
    size              INTEGER NOT NULL,
    mtime_ns          INTEGER NOT NULL,
    payload_hash      TEXT,
    extractor_version TEXT,
    onyx_id           TEXT,
    pushed_at         TEXT,
    last_error        TEXT,
    missing_count     INTEGER NOT NULL DEFAULT 0,
    skip_reason       TEXT,
    PRIMARY KEY (root_id, rel_path)
) WITHOUT ROWID;

CREATE TABLE run (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT NOT NULL,
    ended_at         TEXT,
    pushed           INTEGER NOT NULL DEFAULT 0,
    unchanged        INTEGER NOT NULL DEFAULT 0,
    skipped          INTEGER NOT NULL DEFAULT 0,
    failed           INTEGER NOT NULL DEFAULT 0,
    deleted          INTEGER NOT NULL DEFAULT 0,
    unknown_subtrees INTEGER NOT NULL DEFAULT 0,
    exit_code        INTEGER,
    blocked_deletes  INTEGER NOT NULL DEFAULT 0
);
