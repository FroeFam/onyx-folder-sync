"""Reconcile: explicit repair after Onyx-side changes or a restored state file.

Plan section 5.7. Compares what Onyx lists for a root's entry with state and
with a complete, error-free walk:

- Documents state has but Onyx lacks are marked for a new push.
- ``fc_`` ids Onyx has but state lacks are deleted when no file on disk maps
  to them; when the file exists but is filtered out they are only reported,
  unless ``delete_excluded`` is set. When the file is eligible, the sync pass
  that follows pushes it again under the same id.
- Ids without the ``fc_`` prefix are never touched.

This is the one delete path for ids that are not in state. The proof of
ownership here is the ``fc_`` prefix plus membership in the root's own entry,
which preflight verified as created by this installation. The delete guard
applies as everywhere else.

A root that is not in state at all cannot be reconciled: document ids derive
from the root id kept in state.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from onyx_folder_sync.engine import (
    StopRun,
    check_entry,
    check_onyx,
    match_roots,
    run_sync,
    short_error,
)
from onyx_folder_sync.filters import build_filter
from onyx_folder_sync.guard import evaluate_guard
from onyx_folder_sync.ids import document_id
from onyx_folder_sync.interfaces import OnyxAuthError, OnyxError, RootUnavailableError, SyncDeps
from onyx_folder_sync.models import ID_PREFIX, Config, ExitCode, SyncOptions, SyncReport
from onyx_folder_sync.walk import walk_root


def _repair(
    config: Config,
    deps: SyncDeps,
    root_name: str,
    *,
    delete_excluded: bool,
    allow_deletes: int | None,
    messages: list[str],
) -> tuple[int, int]:
    """Returns (deleted, blocked). Raises StopRun."""
    if not check_onyx(deps):
        raise StopRun(ExitCode.OK, "Onyx is unreachable; nothing done")
    pairs = [p for p in match_roots(config, deps, write=False) if p[0].name == root_name]
    if not pairs:
        raise StopRun(ExitCode.CONFIG_OR_STATE, f"root '{root_name}' is not in the config")
    root, record = pairs[0]
    if not record.root_id or record.cc_pair_id is None:
        raise StopRun(
            ExitCode.CONFIG_OR_STATE,
            f"root '{root_name}' has no state to reconcile against. Document ids derive from "
            "the root id in state, so after a lost state file remove the old entry in the "
            "Onyx admin page and run 'sync'.",
        )
    check_entry(deps, record)
    try:
        walk = walk_root(
            Path(root.path),
            build_filter(root, config.defaults),
            descend_excluded=True,
            scandir=deps.scandir,
        )
    except RootUnavailableError as exc:
        raise StopRun(
            ExitCode.CONFIG_OR_STATE, f"root '{root_name}' is unavailable: {short_error(str(exc))}"
        ) from exc
    if not walk.complete:
        raise StopRun(
            ExitCode.CONFIG_OR_STATE,
            f"root '{root_name}': the walk had errors in {len(walk.errored_dirs)} folder(s); "
            "reconcile needs a complete walk",
        )

    in_onyx = set(deps.onyx.list_document_ids(record.cc_pair_id))
    rows = deps.state.list_documents(record.root_id)
    in_state = {r.onyx_id for r in rows if r.onyx_id is not None}

    lost = [r for r in rows if r.onyx_id is not None and r.onyx_id not in in_onyx]
    for row in lost:
        deps.state.upsert_document(
            replace(
                row,
                onyx_id=None,
                pushed_at=None,
                payload_hash=None,
                last_error="missing in Onyx (reconcile)",
            )
        )
    if lost:
        messages.append(f"{len(lost)} documents are missing in Onyx and will be pushed again")

    eligible_ids = {document_id(record.root_id, e.rel_path) for e in walk.eligible}
    filtered_ids = {document_id(record.root_id, s.rel_path): s.rel_path for s in walk.skipped}
    foreign = sum(1 for i in in_onyx if not i.startswith(ID_PREFIX))
    if foreign:
        messages.append(f"{foreign} documents without the '{ID_PREFIX}' prefix were left alone")

    doomed: list[str] = []
    for onyx_id in sorted(in_onyx - in_state):
        if not onyx_id.startswith(ID_PREFIX) or onyx_id in eligible_ids:
            continue
        if onyx_id in filtered_ids and not delete_excluded:
            messages.append(
                f"kept {onyx_id}: its file '{filtered_ids[onyx_id]}' exists but is filtered out "
                "(use --delete-excluded to remove it)"
            )
            continue
        doomed.append(onyx_id)
    if not doomed:
        return 0, 0
    allowed = evaluate_guard(
        len(doomed), len(in_onyx), max_delete_fraction=config.defaults.max_delete_fraction
    )
    if not allowed and allow_deletes != len(doomed):
        messages.append(
            f"delete guard blocked {len(doomed)} deletes; nothing was deleted. "
            f"Run 'reconcile {root_name} --allow-deletes {len(doomed)}' to proceed."
        )
        return 0, len(doomed)
    deleted = 0
    for onyx_id in doomed:
        deps.onyx.delete_document(onyx_id)
        deleted += 1
    messages.append(f"deleted {deleted} documents that have no file on disk")
    return deleted, 0


def reconcile_root(
    config: Config,
    deps: SyncDeps,
    root_name: str,
    *,
    delete_excluded: bool = False,
    allow_deletes: int | None = None,
) -> SyncReport:
    messages: list[str] = []
    try:
        deleted, blocked = _repair(
            config,
            deps,
            root_name,
            delete_excluded=delete_excluded,
            allow_deletes=allow_deletes,
            messages=messages,
        )
    except StopRun as stop:
        return SyncReport(exit_code=stop.code, messages=[*messages, stop.message])
    except OnyxAuthError:
        return SyncReport(
            exit_code=ExitCode.AUTH_FAILURE, messages=[*messages, "Onyx rejected the token"]
        )
    except OnyxError as exc:
        return SyncReport(
            exit_code=ExitCode.DOCUMENT_FAILURES,
            messages=[*messages, f"reconcile failed: {short_error(exc)}"],
        )
    # Pushes still happen when the guard blocked the deletes.
    report = run_sync(config, deps, SyncOptions())
    report.messages = [*messages, *report.messages]
    for root_report in report.roots:
        if root_report.name == root_name:
            root_report.deleted += deleted
            root_report.blocked_deletes += blocked
    if blocked and report.exit_code in (ExitCode.OK, ExitCode.DOCUMENT_FAILURES):
        report.exit_code = ExitCode.DELETE_GUARD_BLOCKED
    return report
