"""SQLite state store.

Commit behaviour (invariants I6 and I11): the connection is in autocommit mode
(isolation_level None), so every single statement is durable when execute()
returns (WAL with synchronous FULL). Multi-row methods wrap their statements in
one explicit BEGIN IMMEDIATE ... COMMIT and roll back on any error. No method
leaves a transaction open when it returns.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from onyx_folder_sync.interfaces import StateError
from onyx_folder_sync.models import (
    DocumentRecord,
    RootRecord,
    RunCounts,
    RunRecord,
    SkipReason,
)

Migration = Callable[[sqlite3.Connection], None]

# MIGRATIONS[n - 1] upgrades schema version n to n + 1 inside one transaction.
# The current version is 1 + len(MIGRATIONS). schema.sql is version 1.
MIGRATIONS: list[Migration] = []

_DOC_COLUMNS = (
    "root_id, rel_path, size, mtime_ns, payload_hash, extractor_version, onyx_id, "
    "pushed_at, last_error, missing_count, skip_reason"
)


def _doc_from_row(row: tuple[object, ...]) -> DocumentRecord:
    skip = row[10]
    return DocumentRecord(
        root_id=str(row[0]),
        rel_path=str(row[1]),
        size=int(row[2]),  # type: ignore[call-overload]
        mtime_ns=int(row[3]),  # type: ignore[call-overload]
        payload_hash=None if row[4] is None else str(row[4]),
        extractor_version=None if row[5] is None else str(row[5]),
        onyx_id=None if row[6] is None else str(row[6]),
        pushed_at=None if row[7] is None else str(row[7]),
        last_error=None if row[8] is None else str(row[8]),
        missing_count=int(row[9]),  # type: ignore[call-overload]
        skip_reason=None if skip is None else SkipReason(str(skip)),
    )


def _root_from_row(row: tuple[object, ...]) -> RootRecord:
    return RootRecord(
        root_id=str(row[0]),
        name=str(row[1]),
        path=str(row[2]),
        cc_pair_id=None if row[3] is None else int(row[3]),  # type: ignore[call-overload]
        connector_id=None if row[4] is None else int(row[4]),  # type: ignore[call-overload]
        credential_id=None if row[5] is None else int(row[5]),  # type: ignore[call-overload]
    )


class SqliteStateStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # install -------------------------------------------------------------

    def instance_id(self) -> str:
        with self._tx() as c:
            row = c.execute("SELECT instance_id FROM install WHERE id = 1").fetchone()
            if row is not None:
                return str(row[0])
            new_id = secrets.token_hex(8)
            c.execute("INSERT INTO install (id, instance_id) VALUES (1, ?)", (new_id,))
            return new_id

    # roots ---------------------------------------------------------------

    def list_roots(self) -> list[RootRecord]:
        rows = self._conn.execute(
            "SELECT root_id, name, path, cc_pair_id, connector_id, credential_id "
            "FROM root ORDER BY name"
        ).fetchall()
        return [_root_from_row(r) for r in rows]

    def get_root(self, name: str) -> RootRecord | None:
        row = self._conn.execute(
            "SELECT root_id, name, path, cc_pair_id, connector_id, credential_id "
            "FROM root WHERE name = ?",
            (name,),
        ).fetchone()
        return None if row is None else _root_from_row(row)

    def add_root(self, name: str, path: str) -> RootRecord:
        root_id = secrets.token_hex(16)
        try:
            self._conn.execute(
                "INSERT INTO root (root_id, name, path) VALUES (?, ?, ?)",
                (root_id, name, path),
            )
        except sqlite3.IntegrityError:
            raise StateError(f"root name already exists: {name}") from None
        return RootRecord(root_id=root_id, name=name, path=path)

    def _require_one(self, cursor: sqlite3.Cursor, root_id: str) -> None:
        if cursor.rowcount != 1:
            raise StateError(f"unknown root id: {root_id}")

    def set_root_entry(
        self, root_id: str, cc_pair_id: int, connector_id: int, credential_id: int
    ) -> None:
        cur = self._conn.execute(
            "UPDATE root SET cc_pair_id = ?, connector_id = ?, credential_id = ? WHERE root_id = ?",
            (cc_pair_id, connector_id, credential_id, root_id),
        )
        self._require_one(cur, root_id)

    def set_root_path(self, root_id: str, path: str) -> None:
        cur = self._conn.execute("UPDATE root SET path = ? WHERE root_id = ?", (path, root_id))
        self._require_one(cur, root_id)

    def rename_root(self, old: str, new: str) -> None:
        with self._tx() as c:
            if c.execute("SELECT 1 FROM root WHERE name = ?", (old,)).fetchone() is None:
                raise StateError(f"unknown root: {old}")
            if c.execute("SELECT 1 FROM root WHERE name = ?", (new,)).fetchone() is not None:
                raise StateError(f"root name already exists: {new}")
            c.execute("UPDATE root SET name = ? WHERE name = ?", (new, old))

    def remove_root(self, root_id: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM document WHERE root_id = ?", (root_id,))
            c.execute("DELETE FROM root WHERE root_id = ?", (root_id,))

    # documents -----------------------------------------------------------

    def list_documents(self, root_id: str) -> list[DocumentRecord]:
        rows = self._conn.execute(
            f"SELECT {_DOC_COLUMNS} FROM document WHERE root_id = ? ORDER BY rel_path",  # noqa: S608
            (root_id,),
        ).fetchall()
        return [_doc_from_row(r) for r in rows]

    def get_document(self, root_id: str, rel_path: str) -> DocumentRecord | None:
        row = self._conn.execute(
            f"SELECT {_DOC_COLUMNS} FROM document WHERE root_id = ? AND rel_path = ?",  # noqa: S608
            (root_id, rel_path),
        ).fetchone()
        return None if row is None else _doc_from_row(row)

    def upsert_document(self, record: DocumentRecord) -> None:
        skip = None if record.skip_reason is None else str(record.skip_reason.value)
        self._conn.execute(
            f"INSERT OR REPLACE INTO document ({_DOC_COLUMNS}) "  # noqa: S608
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.root_id,
                record.rel_path,
                record.size,
                record.mtime_ns,
                record.payload_hash,
                record.extractor_version,
                record.onyx_id,
                record.pushed_at,
                record.last_error,
                record.missing_count,
                skip,
            ),
        )

    def delete_document(self, root_id: str, rel_path: str) -> None:
        self._conn.execute(
            "DELETE FROM document WHERE root_id = ? AND rel_path = ?", (root_id, rel_path)
        )

    def bump_missing(self, root_id: str, rel_paths: Sequence[str]) -> dict[str, int]:
        result: dict[str, int] = {}
        with self._tx() as c:
            for rel_path in dict.fromkeys(rel_paths):
                cur = c.execute(
                    "UPDATE document SET missing_count = missing_count + 1 "
                    "WHERE root_id = ? AND rel_path = ?",
                    (root_id, rel_path),
                )
                if cur.rowcount == 1:
                    row = c.execute(
                        "SELECT missing_count FROM document WHERE root_id = ? AND rel_path = ?",
                        (root_id, rel_path),
                    ).fetchone()
                    result[rel_path] = int(row[0])
        return result

    def clear_missing(self, root_id: str, rel_paths: Sequence[str]) -> None:
        with self._tx() as c:
            for rel_path in rel_paths:
                c.execute(
                    "UPDATE document SET missing_count = 0 WHERE root_id = ? AND rel_path = ?",
                    (root_id, rel_path),
                )

    # runs ----------------------------------------------------------------

    def start_run(self, started_at: str) -> int:
        cur = self._conn.execute("INSERT INTO run (started_at) VALUES (?)", (started_at,))
        if cur.lastrowid is None:
            raise StateError("could not record run start")
        return cur.lastrowid

    def finish_run(
        self,
        run_id: int,
        ended_at: str,
        counts: RunCounts,
        exit_code: int,
        blocked_deletes: int,
    ) -> None:
        cur = self._conn.execute(
            "UPDATE run SET ended_at = ?, pushed = ?, unchanged = ?, skipped = ?, failed = ?, "
            "deleted = ?, unknown_subtrees = ?, exit_code = ?, blocked_deletes = ? "
            "WHERE run_id = ?",
            (
                ended_at,
                counts.pushed,
                counts.unchanged,
                counts.skipped,
                counts.failed,
                counts.deleted,
                counts.unknown_subtrees,
                exit_code,
                blocked_deletes,
                run_id,
            ),
        )
        if cur.rowcount != 1:
            raise StateError(f"unknown run id: {run_id}")

    def last_run(self) -> RunRecord | None:
        row = self._conn.execute(
            "SELECT run_id, started_at, ended_at, pushed, unchanged, skipped, failed, deleted, "
            "unknown_subtrees, exit_code, blocked_deletes FROM run ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return RunRecord(
            run_id=int(row[0]),
            started_at=str(row[1]),
            ended_at=None if row[2] is None else str(row[2]),
            counts=RunCounts(
                pushed=int(row[3]),
                unchanged=int(row[4]),
                skipped=int(row[5]),
                failed=int(row[6]),
                deleted=int(row[7]),
                unknown_subtrees=int(row[8]),
            ),
            exit_code=None if row[9] is None else int(row[9]),
            blocked_deletes=int(row[10]),
        )

    def close(self) -> None:
        self._conn.close()


def _schema_sql() -> str:
    return (resources.files("onyx_folder_sync") / "schema.sql").read_text(encoding="utf-8")


def _initialize(conn: sqlite3.Connection) -> None:
    """Create or upgrade the schema; leaves no transaction open."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    has_tables = conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None
    latest = 1 + len(MIGRATIONS)
    if version > latest:
        raise StateError(f"state schema version {version} is newer than supported {latest}")
    if version == 0:
        if has_tables:
            raise StateError("state file has tables but no schema version")
        # One transaction: tables and version appear together or not at all.
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _split_statements(_schema_sql()):
                conn.execute(statement)
            conn.execute("PRAGMA user_version = 1")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        version = 1
    while version < latest:
        conn.execute("BEGIN IMMEDIATE")
        try:
            MIGRATIONS[version - 1](conn)
            version += 1
            conn.execute(f"PRAGMA user_version = {version}")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


def _split_statements(script: str) -> list[str]:
    """Split schema.sql into statements; it holds no semicolons inside literals."""
    lines = [ln for ln in script.splitlines() if not ln.lstrip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def open_state(path: Path) -> SqliteStateStore:
    """Open or create the state database at path. Raises StateError when unusable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    except sqlite3.Error as exc:
        raise StateError(f"cannot open state file {path}: {exc}") from None
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        _initialize(conn)
    except sqlite3.Error as exc:
        conn.close()
        raise StateError(f"state file {path} is not a usable database: {exc}") from None
    except BaseException:
        conn.close()
        raise
    return SqliteStateStore(conn)
