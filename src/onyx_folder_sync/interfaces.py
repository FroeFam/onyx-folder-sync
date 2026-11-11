"""Module interfaces. Frozen contract: only the orchestrator changes this file.

Each module below must provide exactly the named callables with the given
signatures. Other modules and the tests depend on nothing else.

config.py (package 1)
    load_config(path: Path) -> Config
        Reads YAML, validates. Raises ConfigError for: unreadable or invalid
        file, duplicate root names (case-insensitive), nested or overlapping
        root paths, a state path on a network filesystem, a token in the file.
    read_token(env: Mapping[str, str]) -> str
        From ONYX_TOKEN, else the file named by ONYX_TOKEN_FILE (stripped).
        Raises ConfigError when neither yields a non-empty token.
    is_network_path(path: Path) -> bool

filters.py (package 1)
    build_filter(root: RootConfig, defaults: Defaults) -> PathFilter

walk.py (package 2)
    walk_root(root_path: Path, path_filter: PathFilter, *,
              descend_excluded: bool = False,
              scandir: ScandirFn = os.scandir) -> WalkResult
        Raises RootUnavailableError when root_path is missing or not a
        directory or cannot be listed at all.

state.py (package 3)
    open_state(path: Path) -> StateStore
        Creates the file and schema when missing, migrates when older.
        Raises StateError for a corrupt file or a newer schema version.

lock.py (package 3)
    acquire_lock(state_path: Path) -> contextlib.AbstractContextManager[None]
        Exclusive, non-blocking OS lock on "<state_path>.lock". Raises
        LockHeldError when another process holds it.

extract/ (package 4)
    create_runner(*, timeout_seconds: float, memory_mb: int) -> ExtractionRunner
    build_extract_report(files: Iterable[FileEntry], runner: ExtractionRunner,
                         *, max_text_chars: int) -> ExtractReport

onyx_client.py (package 5)
    create_client(base_url: str, token: str, *,
                  sleep: Callable[[float], None] = time.sleep) -> OnyxApi

ids.py (package 6)
    document_id(root_id: str, rel_path: str) -> str
        "fc_" + first 32 hex chars of SHA-256 over "<root_id>\\n<rel_path>"
        (UTF-8; rel_path NFC with forward slashes).

guard.py (package 6)
    evaluate_guard(pending: int, total_documents: int, *,
                   max_delete_fraction: float) -> bool
        True when the deletes of one root may proceed: pending <=
        FREE_DELETES_PER_ROOT, or pending / total_documents <=
        max_delete_fraction.

engine.py (package 6)
    run_sync(config: Config, deps: SyncDeps, options: SyncOptions) -> SyncReport
        One pass of section 5.2 from preflight to report. The caller holds the
        lock. Never raises for expected failures; they become exit codes.

reconcile.py, roots.py (package 7)
    reconcile_root(config: Config, deps: SyncDeps, root_name: str, *,
                   delete_excluded: bool = False,
                   allow_deletes: int | None = None) -> SyncReport
    forget_root(deps: SyncDeps, root_name: str, *, state_only: bool = False) -> None
    rename_root(deps: SyncDeps, old: str, new: str) -> None

cli.py (package 7)
    main(argv: Sequence[str] | None = None) -> int
        Global option --config PATH (env ONYX_FOLDER_SYNC_CONFIG). Commands and
        exit codes of section 5.1. Acquires the lock before opening state.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from onyx_folder_sync.models import (
    DocumentRecord,
    EntryInfo,
    ExtractResult,
    OnyxDocument,
    RootRecord,
    RunCounts,
    RunRecord,
    SkipReason,
)

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class FolderSyncError(Exception):
    """Base class. Messages never contain the token or request headers."""


class ConfigError(FolderSyncError):
    """Invalid configuration. Exit code 2."""


class StateError(FolderSyncError):
    """Unusable state database, or state and config disagree. Exit code 2."""


class LockHeldError(FolderSyncError):
    """Another run holds the lock. Exit code 5."""


class RootUnavailableError(FolderSyncError):
    """A root is missing, unmounted or cannot be listed."""


class OnyxError(FolderSyncError):
    """Base class for Onyx client failures."""


class OnyxAuthError(OnyxError):
    """HTTP 401 or 403. Stops the run with exit code 4."""


class OnyxTransportError(OnyxError):
    """No HTTP response: connection refused, DNS failure, connection reset."""


class OnyxTimeoutError(OnyxError):
    """The request timed out. Not retried in the same run."""


class OnyxRequestError(OnyxError):
    """Any other non-2xx response, after retries for 409 and 5xx."""

    def __init__(self, status_code: int, error_code: str | None, message: str) -> None:
        super().__init__(f"HTTP {status_code}: {error_code or message}")
        self.status_code = status_code
        self.error_code = error_code


class OnyxEntryNotFoundError(OnyxRequestError):
    """Upsert answered CONNECTOR_NOT_FOUND: the root's entry is gone."""


# --------------------------------------------------------------------------
# Filters and walk
# --------------------------------------------------------------------------

ScandirFn = Callable[[str], AbstractContextManager[Iterator["os.DirEntry[str]"]]]


class PathFilter(Protocol):
    """Filters 1 to 5 of section 5.3 for one root. Stateful per walk."""

    def load_ignore(self, rel_dir: str, abs_dir: str) -> None:
        """Read "<abs_dir>/.onyxignore" when it exists.

        Called by the walker once per directory, parents before children,
        before any check for entries of that directory. Raises OSError when
        the file exists but cannot be read; the walker then records rel_dir as
        errored and does not descend.
        """

    def check_dir(self, rel_path: str) -> SkipReason | None:
        """None to descend, SkipReason.EXCLUDED to skip the directory."""

    def check_file(self, rel_path: str, size: int) -> SkipReason | None:
        """None when eligible. Applies filters 1 to 5 in order."""


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


class StateStore(Protocol):
    """SQLite-backed state. Every method commits before it returns."""

    def instance_id(self) -> str:
        """Random id of this installation; generated on first call."""

    def list_roots(self) -> list[RootRecord]: ...

    def get_root(self, name: str) -> RootRecord | None: ...

    def add_root(self, name: str, path: str) -> RootRecord:
        """Creates a root with a fresh random root_id and no entry ids."""

    def set_root_entry(
        self, root_id: str, cc_pair_id: int, connector_id: int, credential_id: int
    ) -> None: ...

    def set_root_path(self, root_id: str, path: str) -> None: ...

    def rename_root(self, old: str, new: str) -> None:
        """Raises StateError when old is unknown or new already exists."""

    def remove_root(self, root_id: str) -> None:
        """Deletes the root and all its document rows."""

    def list_documents(self, root_id: str) -> list[DocumentRecord]: ...

    def get_document(self, root_id: str, rel_path: str) -> DocumentRecord | None: ...

    def upsert_document(self, record: DocumentRecord) -> None:
        """Insert or fully replace the row (root_id, rel_path)."""

    def delete_document(self, root_id: str, rel_path: str) -> None: ...

    def bump_missing(self, root_id: str, rel_paths: Sequence[str]) -> dict[str, int]:
        """Adds 1 to missing_count of each row; returns the new counts."""

    def clear_missing(self, root_id: str, rel_paths: Sequence[str]) -> None:
        """Sets missing_count to 0 for each row."""

    def start_run(self, started_at: str) -> int: ...

    def finish_run(
        self,
        run_id: int,
        ended_at: str,
        counts: RunCounts,
        exit_code: int,
        blocked_deletes: int,
    ) -> None: ...

    def last_run(self) -> RunRecord | None:
        """The most recently started run, finished or not."""

    def close(self) -> None: ...


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


class Extractor(Protocol):
    """One file type family. Runs inside the child process."""

    extensions: frozenset[str]
    version: str  # bump when output for the same file would change

    def extract(self, path: Path) -> str: ...


class ExtractionRunner(Protocol):
    """Runs extractors in a child process with a timeout and a memory limit."""

    def version_for(self, extension: str) -> str | None:
        """Extractor version for an extension, None when no extractor handles it."""

    def extract(self, path: Path, extension: str) -> ExtractResult:
        """Never raises for a bad file; returns status "error" or "timeout"."""

    def close(self) -> None: ...


# --------------------------------------------------------------------------
# Onyx client
# --------------------------------------------------------------------------


class OnyxApi(Protocol):
    """Every call of Appendix A. Retries 409 and 5xx three times with backoff.

    Any method may raise OnyxAuthError, OnyxTransportError, OnyxTimeoutError or
    OnyxRequestError.
    """

    def check_auth(self) -> None:
        """GET /me."""

    def create_entry(self, name: str) -> int:
        """Creates an ingestion_api entry with null prune_freq; returns cc_pair_id."""

    def get_entry(self, cc_pair_id: int) -> EntryInfo | None:
        """None when Onyx answers 404."""

    def upsert_document(
        self, document: OnyxDocument, cc_pair_id: int, *, timeout: float | None = None
    ) -> None:
        """Raises OnyxEntryNotFoundError on CONNECTOR_NOT_FOUND."""

    def list_document_ids(self, cc_pair_id: int) -> list[str]: ...

    def delete_document(self, document_id: str) -> None:
        """HTTP 404 counts as success."""

    def remove_entry(self, cc_pair_id: int, connector_id: int, credential_id: int) -> None:
        """Pause, then post a deletion attempt."""

    def close(self) -> None: ...


# --------------------------------------------------------------------------
# Engine dependencies
# --------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class SyncDeps:
    """Everything the engine touches outside its own logic; tests replace parts."""

    state: StateStore
    onyx: OnyxApi
    extractor: ExtractionRunner
    scandir: ScandirFn = os.scandir
    now: Callable[[], datetime] = _utc_now
    sleep: Callable[[float], None] = time.sleep
    log: Callable[[str], None] = print


__all__ = [
    "ConfigError",
    "ExtractionRunner",
    "Extractor",
    "FolderSyncError",
    "LockHeldError",
    "OnyxApi",
    "OnyxAuthError",
    "OnyxEntryNotFoundError",
    "OnyxError",
    "OnyxRequestError",
    "OnyxTimeoutError",
    "OnyxTransportError",
    "PathFilter",
    "RootUnavailableError",
    "ScandirFn",
    "StateError",
    "StateStore",
    "SyncDeps",
]
