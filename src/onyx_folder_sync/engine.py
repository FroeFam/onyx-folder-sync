"""Sync engine: one pass of plan section 5.2, with 5.5 and 5.6.

The caller holds the lock and owns the lifetime of everything in ``SyncDeps``.
Order of a pass: preflight, walk every root, then per root diff, extract and
push, and last the deletes of all roots under the delete guard. Nothing is
deleted when the run stopped early or when the guard blocked any root.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from onyx_folder_sync.filters import build_filter
from onyx_folder_sync.guard import evaluate_guard
from onyx_folder_sync.ids import document_id
from onyx_folder_sync.interfaces import (
    OnyxAuthError,
    OnyxEntryNotFoundError,
    OnyxError,
    OnyxRequestError,
    OnyxTransportError,
    PathFilter,
    RootUnavailableError,
    StateError,
    SyncDeps,
)
from onyx_folder_sync.models import (
    DELIBERATE_SKIPS,
    ID_PREFIX,
    MISSING_WALKS_BEFORE_DELETE,
    Config,
    DocumentRecord,
    EntryInfo,
    ExitCode,
    FileEntry,
    OnyxDocument,
    OnyxSection,
    RootConfig,
    RootRecord,
    RootReport,
    SkipReason,
    SyncOptions,
    SyncReport,
    WalkResult,
)
from onyx_folder_sync.walk import walk_root

ENTRY_SOURCE = "ingestion_api"
MIN_TIMEOUT_SECONDS = 120.0
# Assumed indexing rate until this run has observed one (characters per second).
INITIAL_RATE = 4000.0
TIMEOUT_SAFETY_FACTOR = 3.0
MAX_TRANSPORT_FAILURES = 5
MAX_ERROR_CHARS = 300

# Rows with these reasons and an unchanged file need no new extraction.
_STICKY_SKIPS = frozenset({SkipReason.NO_TEXT, SkipReason.TEXT_TOO_LARGE})


PUSH_PENDING = "push pending"


def may_be_in_onyx(row: DocumentRecord) -> bool:
    """Onyx holds a document for this row, or a push of it may have arrived."""
    return row.has_document or (row.skip_reason is None and row.last_error is not None)


class StopRun(Exception):  # noqa: N818
    """Ends the run with an exit code and a message for the report."""

    def __init__(self, code: ExitCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(slots=True)
class PendingDelete:
    record: DocumentRecord
    reason: str  # "missing" or a SkipReason value
    # Row to store after a successful delete; None removes the row.
    replacement: DocumentRecord | None = None


@dataclass(slots=True)
class RootRun:
    config: RootConfig
    record: RootRecord
    report: RootReport
    walk: WalkResult | None = None
    documents: dict[str, DocumentRecord] = field(default_factory=dict)
    pending: list[PendingDelete] = field(default_factory=list)
    # Documents in Onyx per state when the run started; the delete guard's base.
    documents_at_start: int = 0
    path_filter: PathFilter | None = None

    @property
    def total_documents(self) -> int:
        return sum(1 for d in self.documents.values() if d.has_document)


@dataclass(slots=True)
class RequestPacer:
    """Pause between requests, timeout from the observed rate, failure streak."""

    deps: SyncDeps
    pause_seconds: float
    sent: int = 0
    chars: int = 0
    seconds: float = 0.0
    transport_failures: int = 0

    def before_request(self) -> None:
        if self.sent and self.pause_seconds > 0:
            self.deps.sleep(self.pause_seconds)
        self.sent += 1

    def timeout_for(self, chars: int) -> float:
        rate = self.chars / self.seconds if self.seconds > 0 and self.chars > 0 else INITIAL_RATE
        rate = min(rate, INITIAL_RATE * 10)
        return max(MIN_TIMEOUT_SECONDS, TIMEOUT_SAFETY_FACTOR * chars / rate)

    def observed(self, chars: int, seconds: float) -> None:
        self.chars += chars
        self.seconds += seconds
        self.transport_failures = 0

    def transport_failure(self) -> None:
        self.transport_failures += 1
        if self.transport_failures >= MAX_TRANSPORT_FAILURES:
            raise StopRun(
                ExitCode.DOCUMENT_FAILURES,
                f"{MAX_TRANSPORT_FAILURES} consecutive transport failures; run ended",
            )


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry_name(root_name: str, instance_id: str) -> str:
    return f"folder: {root_name} [{instance_id}]"


def short_error(exc: BaseException | str) -> str:
    text = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    return " ".join(text.split())[:MAX_ERROR_CHARS]


def build_document(
    root: RootConfig, record: RootRecord, entry: FileEntry, text: str
) -> OnyxDocument:
    """Document mapping of plan section 5.4."""
    name = entry.rel_path.rsplit("/", 1)[-1]
    link = None
    if root.link_template:
        encoded = "/".join(quote(part, safe="") for part in entry.rel_path.split("/"))
        link = root.link_template.replace("{relpath}", encoded)
    return OnyxDocument(
        id=document_id(record.root_id, entry.rel_path),
        semantic_identifier=name,
        title=name,
        source="file",
        sections=[OnyxSection(text=text, link=link)],
        metadata={"root": root.name, "path": entry.rel_path, "extension": entry.extension},
        doc_updated_at=iso_utc(datetime.fromtimestamp(entry.mtime_ns / 1e9, UTC)),
    )


def payload_hash(document: OnyxDocument, extractor_version: str) -> str:
    """Hash of everything Onyx indexes, without the timestamp."""
    payload = document.model_dump(exclude={"doc_updated_at"})
    data = json.dumps([extractor_version, payload], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------


def check_onyx(deps: SyncDeps) -> bool:
    """True when Onyx answers and accepts the token, False when unreachable."""
    try:
        deps.onyx.check_auth()
    except OnyxAuthError as exc:
        raise StopRun(ExitCode.AUTH_FAILURE, "Onyx rejected the token (HTTP 401 or 403)") from exc
    except OnyxError as exc:
        deps.log(f"Onyx unreachable: {short_error(exc)}")
        return False
    return True


def check_entry(deps: SyncDeps, record: RootRecord) -> None:
    """Invariant I10: the root's entry is ours and can never be pruned."""
    if record.cc_pair_id is None:
        raise StopRun(ExitCode.CONFIG_OR_STATE, f"root '{record.name}' has no Onyx entry")
    _validate_entry(deps, record, record.cc_pair_id, deps.onyx.get_entry(record.cc_pair_id))


def _validate_entry(
    deps: SyncDeps, record: RootRecord, cc_pair_id: int, info: EntryInfo | None
) -> None:
    problem = None
    if info is None:
        problem = "no longer exists in Onyx"
    elif info.source != ENTRY_SOURCE:
        problem = f"has source '{info.source}', expected '{ENTRY_SOURCE}'"
    elif info.prune_freq is not None:
        problem = "has pruning switched on, which would delete every pushed document"
    elif not info.name.endswith(f"[{deps.state.instance_id()}]"):
        problem = "belongs to another installation"
    if problem:
        raise StopRun(
            ExitCode.CONFIG_OR_STATE,
            f"root '{record.name}': Onyx entry {cc_pair_id} {problem}. "
            f"Fix it in Onyx or run 'forget {record.name}'.",
        )


def create_entry(deps: SyncDeps, record: RootRecord) -> RootRecord:
    cc_pair_id = deps.onyx.create_entry(entry_name(record.name, deps.state.instance_id()))
    info = deps.onyx.get_entry(cc_pair_id)
    # Checked before the entry is stored, so nothing is ever pushed into an
    # entry that Onyx created with pruning on or under another name.
    _validate_entry(deps, record, cc_pair_id, info)
    if info is None:
        raise StopRun(ExitCode.CONFIG_OR_STATE, f"new Onyx entry {cc_pair_id} cannot be read")
    deps.state.set_root_entry(record.root_id, cc_pair_id, info.connector_id, info.credential_id)
    return replace(
        record,
        cc_pair_id=cc_pair_id,
        connector_id=info.connector_id,
        credential_id=info.credential_id,
    )


def match_roots(
    config: Config, deps: SyncDeps, *, write: bool
) -> list[tuple[RootConfig, RootRecord]]:
    """Config and state agree on the set of roots; new roots are added to state."""
    in_state = {r.name: r for r in deps.state.list_roots()}
    stale = sorted(set(in_state) - {r.name for r in config.roots})
    if stale:
        raise StopRun(
            ExitCode.CONFIG_OR_STATE,
            f"root(s) in state but not in config: {', '.join(stale)}. "
            "Run 'forget <root>' or 'rename-root <old> <new>'.",
        )
    pairs: list[tuple[RootConfig, RootRecord]] = []
    for root in config.roots:
        record = in_state.get(root.name)
        if record is None:
            record = (
                deps.state.add_root(root.name, root.path)
                if write
                else RootRecord(root_id="", name=root.name, path=root.path)
            )
        elif record.path != root.path and write:
            deps.state.set_root_path(record.root_id, root.path)
            record = replace(record, path=root.path)
        pairs.append((root, record))
    return pairs


def preflight(config: Config, deps: SyncDeps, *, dry_run: bool) -> list[RootRun] | None:
    """Returns the roots to sync, or None when Onyx is unreachable (not dry-run)."""
    reachable = check_onyx(deps)
    if not reachable and not dry_run:
        return None
    runs: list[RootRun] = []
    for root, record in match_roots(config, deps, write=not dry_run):
        if reachable:
            try:
                if record.cc_pair_id is not None:
                    check_entry(deps, record)
                elif not dry_run:
                    record = create_entry(deps, record)
            except OnyxAuthError as exc:
                raise StopRun(ExitCode.AUTH_FAILURE, "Onyx rejected the token") from exc
            except OnyxError as exc:
                raise StopRun(
                    ExitCode.DOCUMENT_FAILURES,
                    f"root '{root.name}': entry check failed: {short_error(exc)}",
                ) from exc
        runs.append(RootRun(config=root, record=record, report=RootReport(name=root.name)))
    return runs


# --------------------------------------------------------------------------
# Walk
# --------------------------------------------------------------------------


def walk_all(config: Config, deps: SyncDeps, runs: list[RootRun]) -> None:
    """Walk every root before anything is written (invariant I3)."""
    for run in runs:
        if run.record.root_id:
            run.documents = {d.rel_path: d for d in deps.state.list_documents(run.record.root_id)}
        try:
            run.path_filter = build_filter(run.config, config.defaults)
            run.walk = walk_root(Path(run.config.path), run.path_filter, scandir=deps.scandir)
        except RootUnavailableError as exc:
            raise StopRun(
                ExitCode.CONFIG_OR_STATE,
                f"root '{run.config.name}' is unavailable ({short_error(str(exc))}); "
                "nothing pushed, nothing deleted",
            ) from exc
        run.report.unknown_subtrees = list(run.walk.errored_dirs)
        run.documents_at_start = run.total_documents
        if not run.walk.eligible:
            raise StopRun(
                ExitCode.CONFIG_OR_STATE,
                f"root '{run.config.name}' has no eligible files ({run.total_documents} "
                "documents in state); nothing pushed, nothing deleted",
            )


# --------------------------------------------------------------------------
# Diff, extract, push
# --------------------------------------------------------------------------


def _unchanged(record: DocumentRecord | None, entry: FileEntry, version: str | None) -> bool:
    if record is None or record.last_error is not None:
        return False
    if record.size != entry.size or record.mtime_ns != entry.mtime_ns:
        return False
    if record.extractor_version != version:
        return False
    if record.skip_reason in _STICKY_SKIPS:
        return True
    return record.has_document and record.skip_reason is None


def _same_file(entry: FileEntry) -> bool:
    """The file on disk is still the regular file the walk saw."""
    try:
        st = os.stat(entry.abs_path, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(st.st_mode) and st.st_size == entry.size and st.st_mtime_ns == entry.mtime_ns
    )


def _count_skip(report: RootReport, reason: SkipReason) -> None:
    report.skipped[reason] = report.skipped.get(reason, 0) + 1


def _record_error(
    deps: SyncDeps, run: RootRun, entry: FileEntry, error: str, skip: SkipReason | None = None
) -> None:
    """Store an error; an existing document and its proof of ownership stay."""
    old = run.documents.get(entry.rel_path)
    if old is not None:
        row = replace(old, last_error=error)
    else:
        row = DocumentRecord(
            root_id=run.record.root_id,
            rel_path=entry.rel_path,
            size=entry.size,
            mtime_ns=entry.mtime_ns,
            last_error=error,
            skip_reason=skip,
        )
    deps.state.upsert_document(row)
    run.documents[entry.rel_path] = row


def _process_file(
    config: Config,
    deps: SyncDeps,
    options: SyncOptions,
    run: RootRun,
    pacer: RequestPacer,
    entry: FileEntry,
) -> None:
    report = run.report
    old = run.documents.get(entry.rel_path)
    version = deps.extractor.version_for(entry.extension)
    if not options.repush and _unchanged(old, entry, version):
        if old is not None and old.skip_reason is not None:
            _count_skip(report, old.skip_reason)
        else:
            report.unchanged += 1
        return
    age = deps.now().timestamp() - entry.mtime_ns / 1e9
    # A slightly negative age is clock skew between this host and the share.
    if -86400 < age < config.defaults.settle_minutes * 60:
        _count_skip(report, SkipReason.SETTLING)
        return
    if options.dry_run:
        report.would_push.append(entry.rel_path)
        return
    if version is None:
        _count_skip(report, SkipReason.EXTRACT_ERROR)
        _record_error(
            deps, run, entry, f"no extractor for '.{entry.extension}'", SkipReason.EXTRACT_ERROR
        )
        return
    if not _same_file(entry):
        _count_skip(report, SkipReason.CHANGED_DURING_EXTRACT)
        return
    result = deps.extractor.extract(Path(entry.abs_path), entry.extension)
    if not _same_file(entry):
        _count_skip(report, SkipReason.CHANGED_DURING_EXTRACT)
        return
    if result.status != "ok":
        reason = (
            SkipReason.EXTRACT_TIMEOUT if result.status == "timeout" else SkipReason.EXTRACT_ERROR
        )
        _count_skip(report, reason)
        _record_error(deps, run, entry, short_error(result.error or result.status), reason)
        deps.log(f"{run.config.name}: {entry.rel_path}: extraction {result.status}")
        return

    base = DocumentRecord(
        root_id=run.record.root_id,
        rel_path=entry.rel_path,
        size=entry.size,
        mtime_ns=entry.mtime_ns,
        extractor_version=version,
    )
    sticky = None
    if not result.text.strip():
        sticky = SkipReason.NO_TEXT
    elif len(result.text) > config.defaults.max_text_chars:
        sticky = SkipReason.TEXT_TOO_LARGE
    if sticky is not None:
        _count_skip(report, sticky)
        row = replace(base, skip_reason=sticky)
        if old is not None and may_be_in_onyx(old):
            # The document goes, under the guard; the row changes only after that.
            # A row with an unconfirmed push counts: Onyx may hold its document.
            run.pending.append(PendingDelete(old, sticky.value, replacement=row))
        else:
            deps.state.upsert_document(row)
            run.documents[entry.rel_path] = row
        return

    document = build_document(run.config, run.record, entry, result.text)
    digest = payload_hash(document, version)
    if not options.repush and old is not None and old.has_document and old.payload_hash == digest:
        row = replace(
            old,
            size=entry.size,
            mtime_ns=entry.mtime_ns,
            extractor_version=version,
            last_error=None,
            missing_count=0,
            skip_reason=None,
        )
        deps.state.upsert_document(row)
        run.documents[entry.rel_path] = row
        report.unchanged += 1
        return

    if run.record.cc_pair_id is None:
        raise StopRun(ExitCode.CONFIG_OR_STATE, f"root '{run.config.name}' has no Onyx entry")
    # Written before the request: if the push reaches Onyx but its answer or
    # the state write after it is lost, state still knows that Onyx may hold
    # this id, and the cleared hash forces a new push of whatever is on disk.
    if old is not None:
        attempt = replace(old, payload_hash=None, last_error=PUSH_PENDING)
    else:
        attempt = replace(base, extractor_version=None, last_error=PUSH_PENDING)
    deps.state.upsert_document(attempt)
    run.documents[entry.rel_path] = attempt
    pacer.before_request()
    started = deps.now()
    try:
        deps.onyx.upsert_document(
            document, run.record.cc_pair_id, timeout=pacer.timeout_for(len(result.text))
        )
    except OnyxAuthError as exc:
        raise StopRun(ExitCode.AUTH_FAILURE, "Onyx rejected the token during the run") from exc
    except OnyxEntryNotFoundError as exc:
        raise StopRun(
            ExitCode.CONFIG_OR_STATE,
            f"root '{run.config.name}': its Onyx entry disappeared during the run",
        ) from exc
    except OnyxError as exc:
        report.failed += 1
        _record_error(deps, run, entry, short_error(exc))
        deps.log(f"{run.config.name}: {entry.rel_path}: push failed: {short_error(exc)}")
        if isinstance(exc, OnyxTransportError):
            pacer.transport_failure()
        elif isinstance(exc, OnyxRequestError):
            pacer.transport_failures = 0
        return
    # Invariant I6: only now, after a 2xx, does state say the document exists.
    pacer.observed(len(result.text), (deps.now() - started).total_seconds())
    row = replace(
        base,
        payload_hash=digest,
        onyx_id=document.id,
        pushed_at=iso_utc(deps.now()),
    )
    deps.state.upsert_document(row)
    run.documents[entry.rel_path] = row
    report.pushed += 1


def _in_excluded_dir(run: RootRun, rel_path: str) -> bool:
    """The file exists on disk below a folder that the filters exclude."""
    if run.path_filter is None:
        return False
    parts = rel_path.split("/")
    for depth in range(1, len(parts)):
        if run.path_filter.check_dir("/".join(parts[:depth])) is not None:
            return os.path.lexists(os.path.join(run.config.path, *parts))
    return False


def _diff_skipped_and_missing(deps: SyncDeps, options: SyncOptions, run: RootRun) -> None:
    """Queue deletes for deliberate skips and for files proven absent."""
    walk = run.walk
    if walk is None:
        return
    root_id = run.record.root_id
    present: set[str] = {e.rel_path for e in walk.eligible}
    for skipped in walk.skipped:
        present.add(skipped.rel_path)
        _count_skip(run.report, skipped.reason)
        old = run.documents.get(skipped.rel_path)
        if old is None or skipped.reason not in DELIBERATE_SKIPS:
            continue
        if may_be_in_onyx(old):
            run.pending.append(PendingDelete(old, skipped.reason.value))
        elif not options.dry_run:
            deps.state.delete_document(root_id, skipped.rel_path)

    # Seen again, or not seen for certain: either way the count of consecutive
    # absences starts over (invariant I2).
    returned = [
        p
        for p, row in run.documents.items()
        if row.missing_count and (p in present or walk.is_unknown(p))
    ]
    if returned and not options.dry_run:
        deps.state.clear_missing(root_id, returned)

    absent: list[DocumentRecord] = []
    for rel_path, old in run.documents.items():
        if rel_path in present or walk.is_unknown(rel_path):
            continue  # invariant I1: unknown is never treated as absent
        if may_be_in_onyx(old) and _in_excluded_dir(run, rel_path):
            # The walk does not descend into excluded folders. The file is
            # still there, so this is a deliberate skip, not an absence.
            _count_skip(run.report, SkipReason.EXCLUDED)
            run.pending.append(PendingDelete(old, SkipReason.EXCLUDED.value))
        elif may_be_in_onyx(old):
            absent.append(old)
        elif not options.dry_run:
            deps.state.delete_document(root_id, rel_path)
    if not absent:
        return
    if options.dry_run:
        counts = {d.rel_path: d.missing_count + 1 for d in absent}
    else:
        counts = deps.state.bump_missing(root_id, [d.rel_path for d in absent])
    for old in absent:
        # Invariant I2: absent in two consecutive walks before any delete.
        if counts.get(old.rel_path, 0) >= MISSING_WALKS_BEFORE_DELETE:
            run.pending.append(PendingDelete(old, "missing"))


# --------------------------------------------------------------------------
# Deletes
# --------------------------------------------------------------------------


def guard_allows(config: Config, options_allow: int | None, runs: list[RootRun]) -> bool:
    """Invariant I5: one blocked root blocks every delete of the run."""
    total = sum(len(r.pending) for r in runs)
    blocked = [
        r
        for r in runs
        if not evaluate_guard(
            len(r.pending),
            r.documents_at_start,
            max_delete_fraction=config.defaults.max_delete_fraction,
        )
    ]
    if not blocked:
        return True
    return options_allow is not None and options_allow == total


def execute_deletes(deps: SyncDeps, run: RootRun, pacer: RequestPacer) -> None:
    root_id = run.record.root_id
    for item in run.pending:
        current = deps.state.get_document(root_id, item.record.rel_path)
        # Invariant I4: only ids with our prefix that state holds for this
        # root. A row whose push may have reached Onyx has no stored id yet;
        # its id is the one derived from the row itself.
        if current is None or not may_be_in_onyx(current):
            continue
        onyx_id = current.onyx_id or document_id(root_id, current.rel_path)
        if not onyx_id.startswith(ID_PREFIX):
            continue
        pacer.before_request()
        try:
            deps.onyx.delete_document(onyx_id)
        except OnyxAuthError as exc:
            raise StopRun(ExitCode.AUTH_FAILURE, "Onyx rejected the token during the run") from exc
        except OnyxError as exc:
            run.report.failed += 1
            deps.log(
                f"{run.config.name}: {item.record.rel_path}: delete failed: {short_error(exc)}"
            )
            if isinstance(exc, OnyxTransportError):
                pacer.transport_failure()
            elif isinstance(exc, OnyxRequestError):
                pacer.transport_failures = 0
            continue
        pacer.transport_failures = 0
        if item.replacement is None:
            deps.state.delete_document(root_id, item.record.rel_path)
        else:
            deps.state.upsert_document(item.replacement)
        run.report.deleted += 1


def finish_deletes(
    config: Config,
    deps: SyncDeps,
    report: SyncReport,
    runs: list[RootRun],
    pacer: RequestPacer,
    *,
    allow_deletes: int | None,
    dry_run: bool,
) -> None:
    """Apply the guard, then delete (or list the deletes in dry-run mode)."""
    total = sum(len(r.pending) for r in runs)
    if not total:
        return
    allowed = guard_allows(config, allow_deletes, runs)
    if dry_run:
        for run in runs:
            run.report.would_delete = sorted(p.record.rel_path for p in run.pending)
        if not allowed:
            report.messages.append(
                f"delete guard would block {total} deletes; "
                f"a real run needs --allow-deletes {total}"
            )
        return
    if not allowed:
        for run in runs:
            run.report.blocked_deletes = len(run.pending)
        report.exit_code = ExitCode.DELETE_GUARD_BLOCKED
        report.messages.append(
            f"delete guard blocked {total} deletes; nothing was deleted. "
            f"Review with 'sync --dry-run', then run 'sync --allow-deletes {total}'."
        )
        return
    for run in runs:
        execute_deletes(deps, run, pacer)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _run(config: Config, deps: SyncDeps, options: SyncOptions, report: SyncReport) -> None:
    runs = preflight(config, deps, dry_run=options.dry_run)
    if runs is None:
        report.messages.append("Onyx is unreachable; nothing done")
        return
    report.roots = [r.report for r in runs]
    try:
        walk_all(config, deps, runs)
    except StopRun:
        # No root was diffed, so this run proves nothing about absence.
        if not options.dry_run:
            for run in runs:
                rows = deps.state.list_documents(run.record.root_id)
                stale = [r.rel_path for r in rows if r.missing_count]
                if stale:
                    deps.state.clear_missing(run.record.root_id, stale)
        raise
    for run in runs:
        _diff_skipped_and_missing(deps, options, run)
    pacer = RequestPacer(deps, config.onyx.request_pause_seconds)
    for run in runs:
        if run.walk is None:
            continue
        for entry in run.walk.eligible:
            _process_file(config, deps, options, run, pacer, entry)
    finish_deletes(
        config,
        deps,
        report,
        runs,
        pacer,
        allow_deletes=options.allow_deletes,
        dry_run=options.dry_run,
    )


def run_sync(config: Config, deps: SyncDeps, options: SyncOptions) -> SyncReport:
    report = SyncReport()
    run_id = None if options.dry_run else deps.state.start_run(iso_utc(deps.now()))
    try:
        _run(config, deps, options, report)
    except StopRun as stop:
        report.exit_code = stop.code
        report.messages.append(stop.message)
    except (sqlite3.Error, StateError) as exc:
        report.exit_code = ExitCode.CONFIG_OR_STATE
        report.messages.append(f"state database error: {short_error(exc)}")
    if report.exit_code == ExitCode.OK and any(r.failed for r in report.roots):
        report.exit_code = ExitCode.DOCUMENT_FAILURES
    if run_id is not None:
        with contextlib.suppress(sqlite3.Error, StateError):
            deps.state.finish_run(
                run_id,
                iso_utc(deps.now()),
                report.counts,
                int(report.exit_code),
                report.blocked_deletes,
            )
    return report
