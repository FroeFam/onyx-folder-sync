"""Harness for the invariant suite.

Everything here uses the public surface only: ``run_sync``, ``reconcile_root``,
``open_state``, ``create_client`` and the fake Onyx server. The suite is written
from plan section 6 and the contracts, not from the engine code.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from onyx_folder_sync.engine import run_sync
from onyx_folder_sync.ids import document_id
from onyx_folder_sync.interfaces import ScandirFn, SyncDeps
from onyx_folder_sync.models import (
    Config,
    Defaults,
    DocumentRecord,
    OnyxDocument,
    OnyxSection,
    OnyxSettings,
    RootConfig,
    SyncOptions,
    SyncReport,
)
from onyx_folder_sync.onyx_client import create_client
from onyx_folder_sync.reconcile import reconcile_root
from onyx_folder_sync.state import open_state
from tests.fake_onyx import FakeOnyx
from tests.fixtures import InProcessRunner, TreeBuilder

DELETE_PATH = re.compile(r"^/api/onyx-api/ingestion/(.+)$")


class SimulatedCrash(BaseException):
    """Stands in for a process kill. Not an Exception, so nothing swallows it."""


class Hook:
    """Proxy around a state store or Onyx client that hooks one method.

    ``before`` runs ahead of the real call with the call's arguments; ``after`` runs once
    the real call returned, with the same arguments plus ``result=``. ``calls`` counts only
    the calls for which ``when`` is true (all calls when ``when`` is None).
    """

    def __init__(
        self,
        inner: Any,
        method: str,
        *,
        before: Callable[..., None] | None = None,
        after: Callable[..., None] | None = None,
        when: Callable[..., bool] | None = None,
    ) -> None:
        self._inner = inner
        self._method = method
        self._before = before
        self._after = after
        self._when = when
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name != self._method:
            return attr

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            counted = self._when is None or self._when(*args, **kwargs)
            if counted:
                self.calls += 1
            if counted and self._before is not None:
                self._before(*args, **kwargs)
            result = attr(*args, **kwargs)
            if counted and self._after is not None:
                self._after(*args, result=result, **kwargs)
            return result

        return wrapper


class Crash:
    """Raises SimulatedCrash at the nth counted call of a hooked method."""

    def __init__(
        self,
        method: str,
        nth: int = 1,
        *,
        after: bool = False,
        when: Callable[..., bool] | None = None,
    ) -> None:
        self.method = method
        self.nth = nth
        self.after = after
        self.when = when
        self.fired = False
        self._hook: Hook | None = None

    def wrap(self, inner: Any) -> Hook:
        def trip(*_args: Any, **_kwargs: Any) -> None:
            assert self._hook is not None
            if self._hook.calls == self.nth:
                self.fired = True
                raise SimulatedCrash(f"crash {'after' if self.after else 'before'} {self.method}")

        self._hook = Hook(
            inner,
            self.method,
            before=None if self.after else trip,
            after=trip if self.after else None,
            when=self.when,
        )
        return self._hook


def crashing_scandir(nth: int) -> tuple[ScandirFn, list[int]]:
    """A scandir that raises SimulatedCrash on its nth call (1-based)."""
    counter = [0]

    def scandir(path: str) -> Any:
        counter[0] += 1
        if counter[0] == nth:
            raise SimulatedCrash(f"crash during walk at call {nth}")
        return os.scandir(path)

    return scandir, counter


def dropping_scandir(ok_calls: int, error: type[OSError] = OSError) -> ScandirFn:
    """A scandir that works for ``ok_calls`` calls and fails for every later one.

    Imitates a share that drops during a walk.
    """
    counter = [0]

    def scandir(path: str) -> Any:
        counter[0] += 1
        if counter[0] > ok_calls:
            raise error(f"simulated share drop listing {path}")
        return os.scandir(path)

    return scandir


def recording_scandir(seen: list[str]) -> ScandirFn:
    def scandir(path: str) -> Any:
        seen.append(path)
        return os.scandir(path)

    return scandir


def dead_url() -> str:
    """The URL of a server that has just been stopped: connections are refused."""
    with FakeOnyx() as throwaway:
        return throwaway.url


def reset_fake(fake: FakeOnyx) -> None:
    """Return a shared fake server to its initial state between tests."""
    fake.documents.clear()
    fake.entries.clear()
    fake.requests.clear()
    fake.on_request.clear()
    fake.clear_faults()
    fake.auth_failure_status = 401


class Env:
    """One fake Onyx, one state file, one default root, helpers to run and inspect."""

    def __init__(self, tmp_path: Path, fake: FakeOnyx) -> None:
        self.tmp_path = tmp_path
        self.fake = fake
        self.root = tmp_path / "data"
        self.tree = TreeBuilder(self.root)
        self.state_path = tmp_path / "state" / "sync.db"
        self.runner = InProcessRunner()
        self.log_lines: list[str] = []

    # -- configuration -----------------------------------------------------

    def make_config(
        self,
        *,
        roots: Mapping[str, Path] | None = None,
        excludes: Mapping[str, tuple[str, ...]] | None = None,
        url: str | None = None,
        **defaults: Any,
    ) -> Config:
        chosen = dict(roots) if roots is not None else {"main": self.root}
        merged: dict[str, Any] = {"settle_minutes": 0}
        merged.update(defaults)
        return Config(
            onyx=OnyxSettings(url=url or self.fake.url, request_pause_seconds=0),
            state_path=str(self.state_path),
            defaults=Defaults(**merged),
            roots=tuple(
                RootConfig(name=n, path=str(p), exclude=(excludes or {}).get(n, ()))
                for n, p in chosen.items()
            ),
        )

    # -- running -----------------------------------------------------------

    @contextmanager
    def deps(
        self,
        *,
        scandir: ScandirFn | None = None,
        wrap_state: Callable[[Any], Any] | None = None,
        wrap_onyx: Callable[[Any], Any] | None = None,
        token: str | None = None,
        url: str | None = None,
    ) -> Iterator[SyncDeps]:
        state = open_state(self.state_path)
        onyx = create_client(url or self.fake.url, token or self.fake.token, sleep=lambda s: None)
        try:
            yield SyncDeps(
                state=wrap_state(state) if wrap_state else state,
                onyx=wrap_onyx(onyx) if wrap_onyx else onyx,
                extractor=self.runner,
                scandir=scandir or os.scandir,
                sleep=lambda s: None,
                log=self.log_lines.append,
            )
        finally:
            onyx.close()
            state.close()

    def run(
        self,
        *,
        config: Config | None = None,
        options: SyncOptions | None = None,
        scandir: ScandirFn | None = None,
        wrap_state: Callable[[Any], Any] | None = None,
        wrap_onyx: Callable[[Any], Any] | None = None,
        token: str | None = None,
        url: str | None = None,
    ) -> SyncReport:
        cfg = config or self.make_config(url=url)
        with self.deps(
            scandir=scandir, wrap_state=wrap_state, wrap_onyx=wrap_onyx, token=token, url=url
        ) as deps:
            return run_sync(cfg, deps, options or SyncOptions())

    def reconcile(
        self,
        name: str = "main",
        *,
        config: Config | None = None,
        scandir: ScandirFn | None = None,
        delete_excluded: bool = False,
        allow_deletes: int | None = None,
    ) -> SyncReport:
        cfg = config or self.make_config()
        with self.deps(scandir=scandir) as deps:
            return reconcile_root(
                cfg, deps, name, delete_excluded=delete_excluded, allow_deletes=allow_deletes
            )

    # -- state inspection --------------------------------------------------

    def documents(self, name: str = "main") -> dict[str, DocumentRecord]:
        state = open_state(self.state_path)
        try:
            root = state.get_root(name)
            if root is None:
                return {}
            return {d.rel_path: d for d in state.list_documents(root.root_id)}
        finally:
            state.close()

    def root_id(self, name: str = "main") -> str:
        state = open_state(self.state_path)
        try:
            root = state.get_root(name)
            assert root is not None
            return root.root_id
        finally:
            state.close()

    def cc_pair_id(self, name: str = "main") -> int:
        state = open_state(self.state_path)
        try:
            root = state.get_root(name)
            assert root is not None
            assert root.cc_pair_id is not None
            return root.cc_pair_id
        finally:
            state.close()

    def instance_id(self) -> str:
        state = open_state(self.state_path)
        try:
            return state.instance_id()
        finally:
            state.close()

    def doc_id(self, rel_path: str, name: str = "main") -> str:
        return document_id(self.root_id(name), rel_path)

    def put_document(self, doc_id: str, text: str, cc_pair_id: int, path: str = "x.txt") -> None:
        """Push a document straight to Onyx, bypassing state (imitates other writers)."""
        client = create_client(self.fake.url, self.fake.token, sleep=lambda s: None)
        try:
            client.upsert_document(
                OnyxDocument(
                    id=doc_id,
                    semantic_identifier=path,
                    title=path,
                    sections=[OnyxSection(text=text)],
                    metadata={"root": "main", "path": path, "extension": "txt"},
                    doc_updated_at="2026-01-01T00:00:00Z",
                ),
                cc_pair_id,
            )
        finally:
            client.close()

    # -- disk and Onyx content ---------------------------------------------

    def disk_content(self, root: Path | None = None) -> dict[str, str]:
        """rel path -> text of every visible .txt file under root, without following links."""
        base = root or self.root
        found: dict[str, str] = {}
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                full = Path(dirpath) / name
                if name.startswith(".") or not name.endswith(".txt") or full.is_symlink():
                    continue
                rel = full.relative_to(base).as_posix()
                found[rel] = full.read_text(encoding="utf-8")
        return found

    def onyx_content(self, name: str = "main") -> dict[str, str]:
        """rel path -> text of the fake Onyx documents in the root's entry."""
        cc = self.cc_pair_id(name)
        return {str(d.metadata["path"]): d.text for d in self.fake.documents_of(cc)}

    # -- request log -------------------------------------------------------

    def delete_ids(self, since: int = 0) -> list[str]:
        out: list[str] = []
        for req in self.fake.requests[since:]:
            m = DELETE_PATH.match(req.path)
            if req.method == "DELETE" and m:
                out.append(unquote(m.group(1)))
        return out

    def writes(self, since: int = 0) -> list[str]:
        """Mutating requests (everything but GET) as 'METHOD path'."""
        return [f"{r.method} {r.path}" for r in self.fake.requests[since:] if r.method != "GET"]

    def pushes(self, since: int = 0) -> list[str]:
        return [
            r.path
            for r in self.fake.requests[since:]
            if r.method == "POST" and r.path.startswith("/api/onyx-api/ingestion")
        ]

    def mark(self) -> int:
        return len(self.fake.requests)

    def assert_no_wrong_deletes(self, name: str = "main", root: Path | None = None) -> None:
        """No DELETE ever hit the id of a file that exists on disk right now."""
        rid = self.root_id(name)
        existing = {document_id(rid, rel) for rel in self.disk_content(root)}
        wrong = existing & set(self.delete_ids())
        assert not wrong, f"documents of existing files were deleted: {sorted(wrong)}"

    def run_count(self) -> int:
        return state_run_count(self.state_path)


def state_run_count(state_path: Path) -> int:
    conn = sqlite3.connect(str(state_path), timeout=30)
    try:
        return int(conn.execute("SELECT count(*) FROM run").fetchone()[0])
    finally:
        conn.close()


def state_bytes(state_path: Path) -> bytes:
    """Concatenated bytes of the state database and every sibling file (wal, shm, lock)."""
    blob = b""
    for p in sorted(state_path.parent.iterdir()):
        if p.name.startswith(state_path.name) and p.is_file():
            blob += p.read_bytes()
    return blob
