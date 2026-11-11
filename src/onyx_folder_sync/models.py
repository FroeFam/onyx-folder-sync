"""Shared data models. Frozen contract: only the orchestrator changes this file.

Conventions used everywhere:

- A "relative path" (``rel_path``) is relative to its root, uses forward slashes,
  is NFC-normalized and never starts with a slash. The root itself is ``""``.
- An "extension" is lower case without the leading dot (``"pdf"``).
- Times stored as text are UTC ISO 8601 with a trailing ``Z``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ID_PREFIX = "fc_"

DEFAULT_EXTENSIONS: tuple[str, ...] = (
    "pdf",
    "docx",
    "pptx",
    "xlsx",
    "html",
    "htm",
    "eml",
    "epub",
    "txt",
    "md",
    "csv",
    "tsv",
)

# Entry names (files or directories) that are always excluded, matched
# case-insensitively. Hidden entries (name starts with ".") and Office lock
# files (name starts with "~$") are excluded as well.
BUILTIN_EXCLUDED_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        "@eadir",
        "#recycle",
        "$recycle.bin",
        "system volume information",
        "thumbs.db",
        ".ds_store",
    }
)

IGNORE_FILE_NAME = ".onyxignore"

# Deletes that are always allowed per root per run, before the fraction applies.
FREE_DELETES_PER_ROOT = 10

# A file is deleted from Onyx only once its missing count reaches this value.
MISSING_WALKS_BEFORE_DELETE = 2


class ExitCode(IntEnum):
    OK = 0  # success, or Onyx unreachable at preflight
    DOCUMENT_FAILURES = 1
    CONFIG_OR_STATE = 2
    DELETE_GUARD_BLOCKED = 3
    AUTH_FAILURE = 4
    LOCK_HELD = 5


class SkipReason(StrEnum):
    EXCLUDED = "excluded"  # built-in exclude, config glob or .onyxignore
    EXTENSION = "extension"  # not in the extension allowlist
    TOO_LARGE = "too-large"  # over the file size cap
    PLACEHOLDER = "placeholder"  # cloud placeholder, never opened
    SYMLINK = "symlink"  # symlink or junction, never followed
    SETTLING = "settling"  # modified within the settle window
    NO_TEXT = "no-text"  # extraction returned empty text
    TEXT_TOO_LARGE = "text-too-large"  # extracted text over max_text_chars
    CHANGED_DURING_EXTRACT = "changed-during-extract"  # stat differed before/after
    EXTRACT_ERROR = "extract-error"
    EXTRACT_TIMEOUT = "extract-timeout"


# A file with an existing document that hits one of these has the document
# deleted (under the delete guard). Every other reason keeps the document.
DELIBERATE_SKIPS: frozenset[SkipReason] = frozenset(
    {
        SkipReason.EXCLUDED,
        SkipReason.EXTENSION,
        SkipReason.TOO_LARGE,
        SkipReason.NO_TEXT,
        SkipReason.TEXT_TOO_LARGE,
    }
)


# --------------------------------------------------------------------------
# Configuration (section 5.8). Loading and cross-field validation: config.py.
# --------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OnyxSettings(_Strict):
    url: str
    request_pause_seconds: float = Field(default=1.0, ge=0)


class Defaults(_Strict):
    max_file_mb: float = Field(default=20, gt=0)
    max_text_chars: int = Field(default=2_000_000, gt=0)
    max_delete_fraction: float = Field(default=0.25, ge=0, le=1)
    settle_minutes: float = Field(default=5, ge=0)
    extract_timeout_seconds: float = Field(default=120, gt=0)
    extract_memory_mb: int = Field(default=1024, gt=0)
    # None means DEFAULT_EXTENSIONS. A list replaces the built-in list.
    extensions: tuple[str, ...] | None = None

    def effective_extensions(self) -> frozenset[str]:
        source = DEFAULT_EXTENSIONS if self.extensions is None else self.extensions
        return frozenset(e.lower().lstrip(".") for e in source)


class RootConfig(_Strict):
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    exclude: tuple[str, ...] = ()
    link_template: str | None = None


class Config(_Strict):
    onyx: OnyxSettings
    state_path: str
    defaults: Defaults = Defaults()
    roots: tuple[RootConfig, ...] = Field(min_length=1)


# --------------------------------------------------------------------------
# Walk
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileEntry:
    """A file that passed every pre-extraction filter."""

    rel_path: str
    abs_path: str  # the path as found on disk; use this to open the file
    size: int
    mtime_ns: int

    @property
    def extension(self) -> str:
        name = self.rel_path.rsplit("/", 1)[-1]
        return name.rsplit(".", 1)[-1].lower() if "." in name else ""


@dataclass(frozen=True, slots=True)
class SkippedEntry:
    """A file that exists on disk but was filtered out before extraction."""

    rel_path: str
    reason: SkipReason


@dataclass(frozen=True, slots=True)
class WalkResult:
    eligible: tuple[FileEntry, ...]
    skipped: tuple[SkippedEntry, ...]
    # Directories whose content is unknown for this run (enumeration error, or
    # an unreadable .onyxignore). "" means the root directory itself.
    errored_dirs: tuple[str, ...]

    def is_unknown(self, rel_path: str) -> bool:
        """True when rel_path lies in or under a directory that errored."""
        for d in self.errored_dirs:
            if d == "" or rel_path == d or rel_path.startswith(d + "/"):
                return True
        return False

    @property
    def complete(self) -> bool:
        return not self.errored_dirs


# --------------------------------------------------------------------------
# State (section 5.7)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RootRecord:
    root_id: str  # random, generated once, survives rename-root
    name: str
    path: str
    cc_pair_id: int | None = None
    connector_id: int | None = None
    credential_id: int | None = None


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    """One row per file the tool has made a decision about.

    ``onyx_id`` is None until Onyx returned 2xx for a push of this file, and is
    set back to None after a successful delete when the row is kept (for
    example to remember a ``no-text`` result). ``onyx_id is not None`` is the
    tool's only proof that it owns a document (invariants I4 and I6).
    """

    root_id: str
    rel_path: str
    size: int
    mtime_ns: int
    payload_hash: str | None = None
    extractor_version: str | None = None
    onyx_id: str | None = None
    pushed_at: str | None = None
    last_error: str | None = None
    missing_count: int = 0
    skip_reason: SkipReason | None = None

    @property
    def has_document(self) -> bool:
        return self.onyx_id is not None


@dataclass(frozen=True, slots=True)
class RunCounts:
    pushed: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    deleted: int = 0
    unknown_subtrees: int = 0


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: int
    started_at: str
    ended_at: str | None
    counts: RunCounts
    exit_code: int | None
    blocked_deletes: int


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtractResult:
    status: Literal["ok", "error", "timeout"]
    text: str = ""
    error: str | None = None  # short, single line, never file content


@dataclass(frozen=True, slots=True)
class ExtractTypeCounts:
    ok: int = 0
    no_text: int = 0
    failed: int = 0  # error or timeout
    too_large: int = 0  # text over max_text_chars


@dataclass(frozen=True, slots=True)
class ExtractReport:
    by_extension: dict[str, ExtractTypeCounts]
    total_text_chars: int


# --------------------------------------------------------------------------
# Onyx API (Appendix A)
# --------------------------------------------------------------------------


class OnyxSection(BaseModel):
    text: str
    link: str | None = None


class OnyxDocument(BaseModel):
    """Field order and names are the wire format; also the payload hash input."""

    id: str
    semantic_identifier: str
    title: str
    source: str = "file"
    sections: list[OnyxSection]
    metadata: dict[str, str]
    doc_updated_at: str


@dataclass(frozen=True, slots=True)
class EntryInfo:
    """What GET /manage/admin/cc-pair/{id} tells us about a connector entry."""

    cc_pair_id: int
    name: str
    status: str
    num_docs_indexed: int
    source: str
    prune_freq: int | None
    connector_id: int
    credential_id: int


# --------------------------------------------------------------------------
# Sync engine
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SyncOptions:
    dry_run: bool = False
    repush: bool = False
    # One-shot override of a blocked delete guard; valid only when it equals
    # the total pending delete count of the run.
    allow_deletes: int | None = None


@dataclass(slots=True)
class RootReport:
    name: str
    pushed: int = 0
    unchanged: int = 0
    failed: int = 0
    deleted: int = 0
    skipped: dict[SkipReason, int] = field(default_factory=dict)
    unknown_subtrees: list[str] = field(default_factory=list)
    # Deletes that were due but not performed because the guard blocked them.
    blocked_deletes: int = 0
    # Filled in dry-run mode only (relative paths).
    would_push: list[str] = field(default_factory=list)
    would_delete: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SyncReport:
    exit_code: ExitCode = ExitCode.OK
    roots: list[RootReport] = field(default_factory=list)
    # Human-readable reasons for a stop or a non-zero exit. Never contains the
    # token or request headers.
    messages: list[str] = field(default_factory=list)

    @property
    def counts(self) -> RunCounts:
        return RunCounts(
            pushed=sum(r.pushed for r in self.roots),
            unchanged=sum(r.unchanged for r in self.roots),
            skipped=sum(sum(r.skipped.values()) for r in self.roots),
            failed=sum(r.failed for r in self.roots),
            deleted=sum(r.deleted for r in self.roots),
            unknown_subtrees=sum(len(r.unknown_subtrees) for r in self.roots),
        )

    @property
    def blocked_deletes(self) -> int:
        return sum(r.blocked_deletes for r in self.roots)
