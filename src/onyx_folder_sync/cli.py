"""Command line interface (plan section 5.1)."""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Annotated

import typer

from onyx_folder_sync import __version__
from onyx_folder_sync.config import load_config, read_token
from onyx_folder_sync.engine import run_sync
from onyx_folder_sync.extract import build_extract_report, create_runner
from onyx_folder_sync.filters import build_filter
from onyx_folder_sync.interfaces import (
    ConfigError,
    FolderSyncError,
    LockHeldError,
    OnyxApi,
    OnyxAuthError,
    OnyxError,
    OnyxTransportError,
    RootUnavailableError,
    StateError,
    SyncDeps,
)
from onyx_folder_sync.lock import acquire_lock
from onyx_folder_sync.models import (
    Config,
    EntryInfo,
    ExitCode,
    FileEntry,
    OnyxDocument,
    SyncOptions,
    SyncReport,
)
from onyx_folder_sync.onyx_client import create_client
from onyx_folder_sync.reconcile import reconcile_root
from onyx_folder_sync.roots import forget_root, rename_root
from onyx_folder_sync.state import open_state
from onyx_folder_sync.walk import walk_root

app = typer.Typer(
    name="onyx-folder-sync",
    help="Keeps local or mounted folders in sync with an Onyx knowledge base.",
    add_completion=False,
    pretty_exceptions_enable=False,
)

_INTERVAL = re.compile(r"^(\d+)\s*([smhd]?)$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
# Exit codes after which a loop keeps going.
_LOOP_CONTINUES = {ExitCode.OK, ExitCode.DOCUMENT_FAILURES, ExitCode.DELETE_GUARD_BLOCKED}


def _log(message: str) -> None:
    typer.echo(message, err=True)


class _NoOnyx:
    """Stands in for the client when no token is configured (dry-run only)."""

    def _fail(self) -> OnyxTransportError:
        return OnyxTransportError("no token configured")

    def check_auth(self) -> None:
        raise self._fail()

    def create_entry(self, name: str) -> int:
        raise self._fail()

    def get_entry(self, cc_pair_id: int) -> EntryInfo | None:
        raise self._fail()

    def upsert_document(
        self, document: OnyxDocument, cc_pair_id: int, *, timeout: float | None = None
    ) -> None:
        raise self._fail()

    def list_document_ids(self, cc_pair_id: int) -> list[str]:
        raise self._fail()

    def delete_document(self, document_id: str) -> None:
        raise self._fail()

    def remove_entry(self, cc_pair_id: int, connector_id: int, credential_id: int) -> None:
        raise self._fail()

    def close(self) -> None:
        return


def parse_interval(text: str) -> int:
    """'90', '90s', '15m', '6h' or '1d' as seconds."""
    match = _INTERVAL.match(text.strip().lower())
    if not match or int(match.group(1)) <= 0:
        raise ConfigError(f"invalid interval '{text}'; use for example 30m or 6h")
    return int(match.group(1)) * _UNITS[match.group(2)]


def format_report(report: SyncReport, *, dry_run: bool = False) -> list[str]:
    lines: list[str] = []
    for root in report.roots:
        skipped = ", ".join(f"{reason.value} {n}" for reason, n in sorted(root.skipped.items()))
        if dry_run:
            lines.append(
                f"root '{root.name}': would push {len(root.would_push)}, "
                f"would delete {len(root.would_delete)}, unchanged {root.unchanged}, "
                f"skipped {sum(root.skipped.values())}" + (f" ({skipped})" if skipped else "")
            )
            lines.extend(f"  push   {path}" for path in root.would_push)
            lines.extend(f"  delete {path}" for path in root.would_delete)
        else:
            lines.append(
                f"root '{root.name}': pushed {root.pushed}, unchanged {root.unchanged}, "
                f"failed {root.failed}, deleted {root.deleted}, "
                f"skipped {sum(root.skipped.values())}" + (f" ({skipped})" if skipped else "")
            )
            if root.blocked_deletes:
                lines.append(f"  blocked deletes: {root.blocked_deletes}")
        lines.extend(f"  unknown subtree: {d or '.'}" for d in root.unknown_subtrees)
    lines.extend(report.messages)
    return lines


def _config_path(ctx: typer.Context) -> Path:
    path = ctx.obj
    if not isinstance(path, Path):
        raise ConfigError("no config file; pass --config or set ONYX_FOLDER_SYNC_CONFIG")
    return path


@contextmanager
def _session(
    ctx: typer.Context, *, token_optional: bool = False, read_only: bool = False
) -> Iterator[tuple[Config, SyncDeps]]:
    """Config, lock, state, client and extraction runner for one command.

    The lock is taken before state is opened (invariant I7).
    """
    config = load_config(_config_path(ctx))
    state_path = Path(config.state_path)
    # A dry run before the first sync must not create the state or lock file.
    transient = read_only and not state_path.exists()
    with nullcontext() if transient else acquire_lock(state_path):
        onyx: OnyxApi
        try:
            onyx = create_client(config.onyx.url, read_token(os.environ))
        except ConfigError:
            if not token_optional:
                raise
            onyx = _NoOnyx()
        state = open_state(Path(":memory:") if transient else state_path)
        runner = create_runner(
            timeout_seconds=config.defaults.extract_timeout_seconds,
            memory_mb=config.defaults.extract_memory_mb,
        )
        try:
            yield config, SyncDeps(state=state, onyx=onyx, extractor=runner, log=_log)
        finally:
            runner.close()
            onyx.close()
            state.close()


def _guarded(action: Callable[[], int]) -> int:
    """Map expected errors to the exit codes of plan section 5.1."""
    try:
        return action()
    except LockHeldError as exc:
        _log(f"another run holds the lock: {exc}")
        return ExitCode.LOCK_HELD
    except OnyxAuthError:
        _log("Onyx rejected the token (HTTP 401 or 403)")
        return ExitCode.AUTH_FAILURE
    except OnyxError as exc:
        _log(f"Onyx request failed: {exc}")
        return ExitCode.DOCUMENT_FAILURES
    except (ConfigError, StateError, RootUnavailableError, sqlite3.Error) as exc:
        _log(f"error: {exc}")
        return ExitCode.CONFIG_OR_STATE
    except FolderSyncError as exc:
        _log(f"error: {exc}")
        return ExitCode.CONFIG_OR_STATE


def _finish(code: int) -> None:
    raise typer.Exit(int(code))


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option("--config", envvar="ONYX_FOLDER_SYNC_CONFIG", help="Path of the YAML config."),
    ] = None,
    version: Annotated[bool, typer.Option("--version", help="Print the version.")] = False,
) -> None:
    if version:
        typer.echo(f"onyx-folder-sync {__version__}")
        raise typer.Exit(0)
    ctx.obj = config
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)


@app.command()
def sync(
    ctx: typer.Context,
    loop: Annotated[bool, typer.Option("--loop", help="Repeat until stopped.")] = False,
    interval: Annotated[str, typer.Option(help="Pause between passes, e.g. 30m or 6h.")] = "6h",
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Walk and diff only; write nothing.")
    ] = False,
    repush: Annotated[
        bool, typer.Option("--repush", help="Ignore stored hashes and push everything.")
    ] = False,
    allow_deletes: Annotated[
        int | None,
        typer.Option(help="One-shot override of a blocked delete guard for exactly N deletes."),
    ] = None,
) -> None:
    """Run one sync pass, or repeat with --loop."""

    def action() -> int:
        seconds = parse_interval(interval)
        if loop and dry_run:
            raise ConfigError("--loop and --dry-run cannot be combined")
        with _session(ctx, token_optional=dry_run, read_only=dry_run) as (config, deps):
            options = SyncOptions(dry_run=dry_run, repush=repush, allow_deletes=allow_deletes)
            while True:
                report = run_sync(config, deps, options)
                for line in format_report(report, dry_run=dry_run):
                    typer.echo(line)
                if not loop or report.exit_code not in _LOOP_CONTINUES:
                    return int(report.exit_code)
                # --repush and --allow-deletes apply to the first pass only.
                options = SyncOptions()
                time.sleep(seconds)

    _finish(_guarded(action))


@app.command("extract-report")
def extract_report(ctx: typer.Context) -> None:
    """Extract every eligible file, push nothing, print per-type counts."""

    def action() -> int:
        config = load_config(_config_path(ctx))
        files: list[FileEntry] = []
        for root in config.roots:
            walk = walk_root(Path(root.path), build_filter(root, config.defaults))
            files.extend(walk.eligible)
            for errored in walk.errored_dirs:
                _log(f"root '{root.name}': unknown subtree: {errored or '.'}")
        runner = create_runner(
            timeout_seconds=config.defaults.extract_timeout_seconds,
            memory_mb=config.defaults.extract_memory_mb,
        )
        try:
            result = build_extract_report(
                files, runner, max_text_chars=config.defaults.max_text_chars
            )
        finally:
            runner.close()
        typer.echo(f"{'type':<8}{'ok':>8}{'no-text':>10}{'failed':>8}{'too-large':>11}")
        for extension, counts in sorted(result.by_extension.items()):
            typer.echo(
                f"{extension:<8}{counts.ok:>8}{counts.no_text:>10}"
                f"{counts.failed:>8}{counts.too_large:>11}"
            )
        typer.echo(f"files: {len(files)}, total text: {result.total_text_chars} characters")
        return ExitCode.OK

    _finish(_guarded(action))


@app.command()
def status(ctx: typer.Context) -> None:
    """Show counts, the last run and any blocked state. Reads state only."""

    def action() -> int:
        config = load_config(_config_path(ctx))
        state_path = Path(config.state_path)
        if not state_path.exists():
            typer.echo("no state yet; run 'sync' first")
            return ExitCode.OK
        state = open_state(state_path)
        try:
            typer.echo(f"instance id: {state.instance_id()}")
            for root in state.list_roots():
                rows = state.list_documents(root.root_id)
                typer.echo(
                    f"root '{root.name}' ({root.path}): entry {root.cc_pair_id}, "
                    f"documents {sum(1 for r in rows if r.has_document)}, "
                    f"errors {sum(1 for r in rows if r.last_error)}, "
                    f"missing once {sum(1 for r in rows if r.missing_count)}"
                )
            last = state.last_run()
            if last is None:
                typer.echo("no run recorded")
                return ExitCode.OK
            c = last.counts
            typer.echo(
                f"last run: started {last.started_at}, ended {last.ended_at or 'never'}, "
                f"exit code {'none' if last.exit_code is None else last.exit_code}"
            )
            typer.echo(
                f"  pushed {c.pushed}, unchanged {c.unchanged}, skipped {c.skipped}, "
                f"failed {c.failed}, deleted {c.deleted}, unknown subtrees {c.unknown_subtrees}"
            )
            if last.blocked_deletes:
                typer.echo(
                    f"  BLOCKED: the delete guard held back {last.blocked_deletes} deletes; "
                    f"review with 'sync --dry-run', then 'sync --allow-deletes "
                    f"{last.blocked_deletes}'"
                )
        finally:
            state.close()
        return ExitCode.OK

    _finish(_guarded(action))


@app.command()
def forget(
    ctx: typer.Context,
    root: Annotated[str, typer.Argument(help="Name of the root.")],
    state_only: Annotated[
        bool, typer.Option("--state-only", help="Drop local state; leave Onyx untouched.")
    ] = False,
) -> None:
    """Remove a root's Onyx entry with its documents, and its state."""

    def action() -> int:
        with _session(ctx, token_optional=state_only) as (_, deps):
            forget_root(deps, root, state_only=state_only)
        typer.echo(f"root '{root}' forgotten")
        return ExitCode.OK

    _finish(_guarded(action))


@app.command("rename-root")
def rename_root_command(
    ctx: typer.Context,
    old: Annotated[str, typer.Argument(help="Current name.")],
    new: Annotated[str, typer.Argument(help="New name; also change it in the config.")],
) -> None:
    """Rename a root in state; document ids do not change."""

    def action() -> int:
        with _session(ctx, token_optional=True) as (_, deps):
            rename_root(deps, old, new)
        typer.echo(f"root '{old}' renamed to '{new}'")
        return ExitCode.OK

    _finish(_guarded(action))


@app.command()
def reconcile(
    ctx: typer.Context,
    root: Annotated[str, typer.Argument(help="Name of the root.")],
    delete_excluded: Annotated[
        bool,
        typer.Option("--delete-excluded", help="Also delete documents of filtered-out files."),
    ] = False,
    allow_deletes: Annotated[
        int | None,
        typer.Option(help="One-shot override of a blocked delete guard for exactly N deletes."),
    ] = None,
) -> None:
    """Repair a root after Onyx-side changes or a restored state file."""

    def action() -> int:
        with _session(ctx) as (config, deps):
            report = reconcile_root(
                config, deps, root, delete_excluded=delete_excluded, allow_deletes=allow_deletes
            )
        for line in format_report(report):
            typer.echo(line)
        return int(report.exit_code)

    _finish(_guarded(action))


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the exit code."""
    try:
        result = app(
            args=list(sys.argv[1:] if argv is None else argv),
            prog_name="onyx-folder-sync",
            standalone_mode=False,
        )
    except Exception as exc:
        # typer bundles its own copy of click, so match its exceptions by shape.
        kind = type(exc).__name__
        if kind == "Exit":
            return int(getattr(exc, "exit_code", 0))
        if kind == "Abort":
            return 130
        show = getattr(exc, "show", None)
        if callable(show):  # usage errors
            show()
            return ExitCode.CONFIG_OR_STATE
        raise
    return int(result) if isinstance(result, int) else 0
