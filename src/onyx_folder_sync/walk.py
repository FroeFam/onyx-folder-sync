"""Metadata-only directory walker. Never opens a file and never follows links."""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

from onyx_folder_sync.interfaces import PathFilter, RootUnavailableError, ScandirFn
from onyx_folder_sync.models import (
    FileEntry,
    SkippedEntry,
    SkipReason,
    WalkResult,
)

FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_PLACEHOLDER_MASK = (
    FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)


def is_placeholder_attributes(file_attributes: int) -> bool:
    """True when Windows file attributes mark a cloud placeholder."""
    return bool(file_attributes & _PLACEHOLDER_MASK)


def _rel(rel_dir: str, name: str) -> str:
    joined = name if rel_dir == "" else f"{rel_dir}/{name}"
    return unicodedata.normalize("NFC", joined)


def walk_root(
    root_path: Path,
    path_filter: PathFilter,
    *,
    descend_excluded: bool = False,
    scandir: ScandirFn = os.scandir,
) -> WalkResult:
    """Walk root_path and classify every regular file by metadata only."""
    if not root_path.is_dir():
        raise RootUnavailableError(f"root is missing or not a directory: {root_path}")

    eligible: list[FileEntry] = []
    skipped: list[SkippedEntry] = []
    errored: list[str] = []
    # (rel_dir, abs_dir, excluded)
    stack: list[tuple[str, str, bool]] = [("", str(root_path), False)]

    while stack:
        rel_dir, abs_dir, excluded = stack.pop()
        if not excluded:
            try:
                path_filter.load_ignore(rel_dir, abs_dir)
            except OSError:
                errored.append(rel_dir)
                continue

        found_files: list[FileEntry] = []
        found_skips: list[SkippedEntry] = []
        found_dirs: list[tuple[str, str, bool]] = []
        try:
            with scandir(abs_dir) as entries:
                for entry in entries:
                    rel = _rel(rel_dir, entry.name)
                    if entry.is_symlink() or entry.is_junction():
                        found_skips.append(SkippedEntry(rel, SkipReason.SYMLINK))
                    elif entry.is_dir(follow_symlinks=False):
                        if excluded:
                            found_dirs.append((rel, entry.path, True))
                        elif path_filter.check_dir(rel) is None:
                            found_dirs.append((rel, entry.path, False))
                        elif descend_excluded:
                            found_dirs.append((rel, entry.path, True))
                    elif entry.is_file(follow_symlinks=False):
                        if excluded:
                            found_skips.append(SkippedEntry(rel, SkipReason.EXCLUDED))
                            continue
                        st = entry.stat(follow_symlinks=False)
                        reason = path_filter.check_file(rel, st.st_size)
                        if reason is not None:
                            found_skips.append(SkippedEntry(rel, reason))
                        elif is_placeholder_attributes(getattr(st, "st_file_attributes", 0)):
                            found_skips.append(SkippedEntry(rel, SkipReason.PLACEHOLDER))
                        else:
                            found_files.append(
                                FileEntry(rel, entry.path, st.st_size, st.st_mtime_ns)
                            )
        except OSError as exc:
            if rel_dir == "":
                raise RootUnavailableError(f"cannot list root {root_path}: {exc}") from exc
            errored.append(rel_dir)
            continue

        eligible.extend(found_files)
        skipped.extend(found_skips)
        stack.extend(found_dirs)

    eligible.sort(key=lambda f: f.rel_path)
    skipped.sort(key=lambda s: s.rel_path)
    return WalkResult(tuple(eligible), tuple(skipped), tuple(sorted(errored)))
