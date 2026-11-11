"""Fixture builders shared by all test suites.

Frozen contract: only the orchestrator changes this file.

- ``TreeBuilder`` creates folder trees with old mtimes (so the settle window
  does not skip them), unicode names, symlinks and unreadable folders.
- ``failing_scandir`` makes chosen directories fail on any platform, for the
  walker and engine fault tests.
- ``InProcessRunner`` is an ``ExtractionRunner`` without child processes.
- ``write_config`` writes a config file for a fake Onyx server.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
import unicodedata
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import yaml

from onyx_folder_sync.interfaces import ScandirFn
from onyx_folder_sync.models import ExtractResult

DEFAULT_AGE_SECONDS = 3600.0

# "e" + combining acute (NFD) and the precomposed form (NFC) of the same name.
NFD_NAME = "café.txt"
NFC_NAME = unicodedata.normalize("NFC", NFD_NAME)


class TreeBuilder:
    """Builds a fixture tree under ``root`` (created when missing)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._unreadable: list[Path] = []

    def path(self, rel: str) -> Path:
        return self.root.joinpath(*rel.split("/"))

    def file(
        self,
        rel: str,
        content: str | bytes = "hello",
        *,
        age_seconds: float = DEFAULT_AGE_SECONDS,
    ) -> Path:
        """Write a file whose mtime lies ``age_seconds`` in the past."""
        target = self.path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8", newline="\n")
        self.set_age(rel, age_seconds)
        return target

    def files(self, count: int, *, prefix: str = "doc", folder: str = "") -> list[str]:
        """Write ``count`` distinct text files; returns their relative paths."""
        rels = [f"{folder}/{prefix}{i:04d}.txt".lstrip("/") for i in range(count)]
        for rel in rels:
            self.file(rel, f"content of {rel}")
        return rels

    def directory(self, rel: str) -> Path:
        target = self.path(rel)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def set_age(self, rel: str, age_seconds: float) -> None:
        when = time.time() - age_seconds
        os.utime(self.path(rel), (when, when))

    def touch_changed(self, rel: str, content: str | bytes) -> None:
        """Rewrite a file so that size or mtime differs from before."""
        target = self.path(rel)
        before = target.stat().st_mtime_ns
        self.file(rel, content, age_seconds=DEFAULT_AGE_SECONDS / 2)
        if target.stat().st_mtime_ns == before:
            self.set_age(rel, DEFAULT_AGE_SECONDS / 3)

    def remove(self, rel: str) -> None:
        self.path(rel).unlink()

    def symlink(self, rel: str, target: Path) -> bool:
        """Create a symlink at ``rel``. False when the platform refuses."""
        link = self.path(rel)
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except (OSError, NotImplementedError):
            return False
        return True

    def junction(self, rel: str, target: Path) -> bool:
        """Create a Windows directory junction. False elsewhere or on failure."""
        if sys.platform != "win32":
            return False
        link = self.path(rel)
        link.parent.mkdir(parents=True, exist_ok=True)
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607
            capture_output=True,
            check=False,
        )
        return done.returncode == 0

    def make_unreadable(self, rel: str) -> bool:
        """Deny listing of a directory (or reading of a file) at OS level.

        Returns False when the denial does not take effect (for example when
        running as root). ``restore`` undoes it; call it before cleanup.
        """
        target = self.path(rel)
        if sys.platform == "win32":
            user = os.environ.get("USERNAME", "")
            done = subprocess.run(
                ["icacls", str(target), "/deny", f"{user}:(RD,R)"],  # noqa: S607
                capture_output=True,
                check=False,
            )
            if done.returncode != 0:
                return False
        else:
            target.chmod(0)
        self._unreadable.append(target)
        try:
            if target.is_dir():
                with os.scandir(target):
                    pass
            else:
                target.read_bytes()
        except OSError:
            return True
        self.restore()
        return False

    def restore(self) -> None:
        """Undo every ``make_unreadable``."""
        for target in self._unreadable:
            if sys.platform == "win32":
                user = os.environ.get("USERNAME", "")
                subprocess.run(
                    ["icacls", str(target), "/remove:d", user],  # noqa: S607
                    capture_output=True,
                    check=False,
                )
            else:
                target.chmod(stat.S_IRWXU)
        self._unreadable.clear()


def failing_scandir(*failing_dirs: Path, error: type[OSError] = PermissionError) -> ScandirFn:
    """A scandir that raises for the given directories and works elsewhere.

    Pass it as ``scandir=`` to ``walk_root`` or in ``SyncDeps``. The returned
    function has a mutable ``failing`` set, so a test can change it between or
    during walks (``fn.failing.add(path)``) to imitate a share that drops.
    """
    failing = {os.path.normcase(os.path.abspath(d)) for d in failing_dirs}

    def scandir(path: str) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
        if os.path.normcase(os.path.abspath(path)) in failing:
            raise error(f"simulated failure listing {path}")
        return os.scandir(path)

    scandir.failing = failing  # type: ignore[attr-defined]
    return scandir


class InProcessRunner:
    """An ExtractionRunner that reads files as UTF-8 text in this process.

    ``errors`` and ``timeouts`` hold absolute path strings that fail.
    ``calls`` records every extracted path.
    """

    def __init__(self, version: str = "test-1") -> None:
        self.version = version
        self.errors: set[str] = set()
        self.timeouts: set[str] = set()
        self.calls: list[str] = []

    def version_for(self, extension: str) -> str | None:
        return self.version

    def extract(self, path: Path, extension: str) -> ExtractResult:
        key = str(path)
        self.calls.append(key)
        if key in self.timeouts:
            return ExtractResult(status="timeout", error="timed out")
        if key in self.errors:
            return ExtractResult(status="error", error="simulated extraction error")
        try:
            return ExtractResult(status="ok", text=path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            return ExtractResult(status="error", error=type(exc).__name__)

    def close(self) -> None:
        return


def write_config(
    path: Path,
    *,
    onyx_url: str,
    state_path: Path,
    roots: dict[str, Path],
    defaults: dict[str, Any] | None = None,
    root_options: dict[str, dict[str, Any]] | None = None,
) -> Path:
    """Write a config file. Pause 0 and settle 0 unless ``defaults`` says otherwise."""
    merged: dict[str, Any] = {"settle_minutes": 0}
    merged.update(defaults or {})
    data = {
        "onyx": {"url": onyx_url, "request_pause_seconds": 0},
        "state_path": str(state_path),
        "defaults": merged,
        "roots": [
            {"name": name, "path": str(root), **(root_options or {}).get(name, {})}
            for name, root in roots.items()
        ],
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@contextmanager
def started_fake_onyx() -> Iterator[Any]:
    """Convenience wrapper so suites need a single import."""
    from tests.fake_onyx import FakeOnyx

    with FakeOnyx() as onyx:
        yield onyx
