from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from onyx_folder_sync import state as state_module
from onyx_folder_sync.interfaces import StateError
from onyx_folder_sync.models import DocumentRecord, RunCounts, SkipReason
from onyx_folder_sync.state import open_state


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "sub" / "state.db"


def _doc(root_id: str, rel_path: str, **kw: object) -> DocumentRecord:
    return DocumentRecord(root_id=root_id, rel_path=rel_path, size=1, mtime_ns=2, **kw)  # type: ignore[arg-type]


def test_creates_parent_dir_and_schema(db_path: Path) -> None:
    store = open_state(db_path)
    try:
        assert db_path.exists()
        assert store.list_roots() == []
        assert store.last_run() is None
    finally:
        store.close()
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_instance_id_is_stable_and_hex16(db_path: Path) -> None:
    store = open_state(db_path)
    first = store.instance_id()
    assert len(first) == 16
    int(first, 16)
    assert store.instance_id() == first
    store.close()
    again = open_state(db_path)
    assert again.instance_id() == first
    again.close()


def test_add_get_list_roots(db_path: Path) -> None:
    store = open_state(db_path)
    root = store.add_root("docs", "/data/docs")
    assert len(root.root_id) == 32
    int(root.root_id, 16)
    assert root.cc_pair_id is None
    assert store.get_root("docs") == root
    assert store.get_root("nope") is None
    other = store.add_root("alpha", "/data/alpha")
    assert other.root_id != root.root_id
    assert [r.name for r in store.list_roots()] == ["alpha", "docs"]
    with pytest.raises(StateError):
        store.add_root("docs", "/elsewhere")
    store.close()


def test_set_root_entry_and_path_persist(db_path: Path) -> None:
    store = open_state(db_path)
    root = store.add_root("docs", "/data/docs")
    store.set_root_entry(root.root_id, 7, 8, 9)
    store.set_root_path(root.root_id, "/new")
    store.close()
    store = open_state(db_path)
    got = store.get_root("docs")
    assert got is not None
    assert (got.cc_pair_id, got.connector_id, got.credential_id, got.path) == (7, 8, 9, "/new")
    with pytest.raises(StateError):
        store.set_root_entry("missing", 1, 2, 3)
    with pytest.raises(StateError):
        store.set_root_path("missing", "/x")
    store.close()


def test_rename_root_keeps_id_and_documents(db_path: Path) -> None:
    store = open_state(db_path)
    root = store.add_root("old", "/p")
    store.upsert_document(_doc(root.root_id, "a.txt", onyx_id="fc_1"))
    store.rename_root("old", "new")
    assert store.get_root("old") is None
    renamed = store.get_root("new")
    assert renamed is not None and renamed.root_id == root.root_id
    assert len(store.list_documents(root.root_id)) == 1
    store.add_root("taken", "/q")
    with pytest.raises(StateError):
        store.rename_root("new", "taken")
    with pytest.raises(StateError):
        store.rename_root("ghost", "other")
    assert store.get_root("new") is not None
    store.close()


def test_remove_root_removes_only_its_documents(db_path: Path) -> None:
    store = open_state(db_path)
    a = store.add_root("a", "/a")
    b = store.add_root("b", "/b")
    store.upsert_document(_doc(a.root_id, "x.txt"))
    store.upsert_document(_doc(a.root_id, "y.txt"))
    store.upsert_document(_doc(b.root_id, "x.txt"))
    store.remove_root(a.root_id)
    assert store.get_root("a") is None
    assert store.list_documents(a.root_id) == []
    assert [d.rel_path for d in store.list_documents(b.root_id)] == ["x.txt"]
    assert store.get_root("b") == b
    store.close()


def test_document_roundtrip_and_replace(db_path: Path) -> None:
    store = open_state(db_path)
    root = store.add_root("a", "/a")
    full = DocumentRecord(
        root_id=root.root_id,
        rel_path="d/e.pdf",
        size=10,
        mtime_ns=123456789012,
        payload_hash="h",
        extractor_version="v1",
        onyx_id="fc_abc",
        pushed_at="2026-01-01T00:00:00Z",
        last_error="boom",
        missing_count=1,
        skip_reason=SkipReason.NO_TEXT,
    )
    store.upsert_document(full)
    assert store.get_document(root.root_id, "d/e.pdf") == full
    assert store.get_document(root.root_id, "other") is None
    replaced = _doc(root.root_id, "d/e.pdf")
    store.upsert_document(replaced)
    got = store.get_document(root.root_id, "d/e.pdf")
    assert got == replaced
    assert got is not None and got.skip_reason is None and got.onyx_id is None
    assert len(store.list_documents(root.root_id)) == 1
    store.delete_document(root.root_id, "d/e.pdf")
    store.delete_document(root.root_id, "d/e.pdf")
    assert store.list_documents(root.root_id) == []
    store.close()


def test_skip_reason_stored_as_string_value(db_path: Path) -> None:
    store = open_state(db_path)
    root = store.add_root("a", "/a")
    store.upsert_document(_doc(root.root_id, "f", skip_reason=SkipReason.TOO_LARGE))
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("SELECT skip_reason FROM document").fetchone()[0] == "too-large"
    finally:
        conn.close()
    got = store.get_document(root.root_id, "f")
    assert got is not None and got.skip_reason is SkipReason.TOO_LARGE
    store.close()


def test_missing_count_logic(db_path: Path) -> None:
    store = open_state(db_path)
    root = store.add_root("a", "/a")
    other = store.add_root("b", "/b")
    for p in ("x", "y", "z"):
        store.upsert_document(_doc(root.root_id, p))
    store.upsert_document(_doc(other.root_id, "x"))
    assert store.bump_missing(root.root_id, ["x", "y", "nope"]) == {"x": 1, "y": 1}
    assert store.bump_missing(root.root_id, ["x"]) == {"x": 2}
    assert store.bump_missing(root.root_id, []) == {}
    counts = {d.rel_path: d.missing_count for d in store.list_documents(root.root_id)}
    assert counts == {"x": 2, "y": 1, "z": 0}
    assert store.list_documents(other.root_id)[0].missing_count == 0
    store.clear_missing(root.root_id, ["x", "nope"])
    counts = {d.rel_path: d.missing_count for d in store.list_documents(root.root_id)}
    assert counts == {"x": 0, "y": 1, "z": 0}
    store.close()


def test_runs_including_unfinished(db_path: Path) -> None:
    store = open_state(db_path)
    assert store.last_run() is None
    first = store.start_run("2026-01-01T00:00:00Z")
    store.finish_run(first, "2026-01-01T00:01:00Z", RunCounts(1, 2, 3, 4, 5, 6), 1, 7)
    got = store.last_run()
    assert got is not None
    assert got.run_id == first
    assert got.counts == RunCounts(1, 2, 3, 4, 5, 6)
    assert (got.ended_at, got.exit_code, got.blocked_deletes) == ("2026-01-01T00:01:00Z", 1, 7)
    second = store.start_run("2026-01-02T00:00:00Z")
    assert second > first
    unfinished = store.last_run()
    assert unfinished is not None
    assert unfinished.run_id == second
    assert unfinished.ended_at is None and unfinished.exit_code is None
    assert unfinished.counts == RunCounts()
    with pytest.raises(StateError):
        store.finish_run(999, "x", RunCounts(), 0, 0)
    store.close()


def test_persistence_across_close_and_reopen(db_path: Path) -> None:
    store = open_state(db_path)
    root = store.add_root("a", "/a")
    store.upsert_document(_doc(root.root_id, "f", onyx_id="fc_1"))
    run = store.start_run("t")
    store.close()
    store = open_state(db_path)
    assert store.get_root("a") == root
    doc = store.get_document(root.root_id, "f")
    assert doc is not None and doc.onyx_id == "fc_1"
    last = store.last_run()
    assert last is not None and last.run_id == run
    store.close()


def test_commit_visible_to_second_connection_without_close(db_path: Path) -> None:
    store = open_state(db_path)
    root = store.add_root("a", "/a")
    store.upsert_document(_doc(root.root_id, "f", onyx_id="fc_1"))
    store.bump_missing(root.root_id, ["f"])
    store.start_run("t")
    # No close(): simulates a crash right after the last call returned.
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("SELECT count(*) FROM root").fetchone()[0] == 1
        assert conn.execute("SELECT onyx_id, missing_count FROM document").fetchone() == (
            "fc_1",
            1,
        )
        assert conn.execute("SELECT count(*) FROM run").fetchone()[0] == 1
    finally:
        conn.close()
    second = open_state(db_path)
    assert second.get_root("a") == root
    second.close()
    store.close()


def test_no_transaction_left_open(db_path: Path) -> None:
    store = open_state(db_path)
    root = store.add_root("a", "/a")
    store.bump_missing(root.root_id, ["x"])
    assert store._conn.in_transaction is False
    store.close()


def test_migrations_run_in_order(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = open_state(db_path)
    store.add_root("a", "/a")
    store.close()
    calls: list[int] = []

    def m1(conn: sqlite3.Connection) -> None:
        calls.append(2)
        conn.execute("ALTER TABLE root ADD COLUMN extra TEXT")

    def m2(conn: sqlite3.Connection) -> None:
        calls.append(3)
        conn.execute("UPDATE root SET extra = 'x'")

    monkeypatch.setattr(state_module, "MIGRATIONS", [m1, m2])
    store = open_state(db_path)
    store.close()
    assert calls == [2, 3]
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute("SELECT extra FROM root").fetchone()[0] == "x"
    finally:
        conn.close()
    open_state(db_path).close()
    assert calls == [2, 3]


def test_migrations_apply_to_new_database(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def m1(conn: sqlite3.Connection) -> None:
        conn.execute("ALTER TABLE root ADD COLUMN extra TEXT")

    monkeypatch.setattr(state_module, "MIGRATIONS", [m1])
    open_state(db_path).close()
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        conn.close()


def test_failed_migration_rolls_back(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    open_state(db_path).close()

    def bad(conn: sqlite3.Connection) -> None:
        conn.execute("ALTER TABLE root ADD COLUMN extra TEXT")
        conn.execute("SELECT * FROM no_such_table")

    monkeypatch.setattr(state_module, "MIGRATIONS", [bad])
    with pytest.raises(StateError):
        open_state(db_path)
    monkeypatch.setattr(state_module, "MIGRATIONS", [])
    store = open_state(db_path)
    store.close()
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        cols = [r[1] for r in conn.execute("PRAGMA table_info(root)")]
        assert "extra" not in cols
    finally:
        conn.close()


def test_newer_schema_version_raises(db_path: Path) -> None:
    open_state(db_path).close()
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(StateError):
        open_state(db_path)


def test_garbage_file_raises(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"this is not a sqlite database " * 100)
    with pytest.raises(StateError):
        open_state(db_path)
