"""Path filters 1 to 5 of section 5.3 for one root."""

from __future__ import annotations

import os

from pathspec import GitIgnoreSpec

from onyx_folder_sync.interfaces import PathFilter
from onyx_folder_sync.models import (
    BUILTIN_EXCLUDED_NAMES,
    IGNORE_FILE_NAME,
    Defaults,
    RootConfig,
    SkipReason,
)


def _gitignore_spec(lines: list[str]) -> GitIgnoreSpec:
    return GitIgnoreSpec.from_lines(lines)


class _RootFilter:
    def __init__(self, root: RootConfig, defaults: Defaults) -> None:
        self._exclude = _gitignore_spec(list(root.exclude))
        self._extensions = defaults.effective_extensions()
        self._max_bytes = defaults.max_file_mb * 1024 * 1024
        # rel_dir ("" for the root) -> spec of that folder's .onyxignore
        self._ignores: dict[str, GitIgnoreSpec] = {}

    def load_ignore(self, rel_dir: str, abs_dir: str) -> None:
        # A missing file is fine; a file that exists but cannot be read raises OSError.
        target = os.path.join(abs_dir, IGNORE_FILE_NAME)
        try:
            with open(target, encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except FileNotFoundError:
            self._ignores.pop(rel_dir, None)
            return
        self._ignores[rel_dir] = _gitignore_spec(lines)

    @staticmethod
    def _builtin_excluded(rel_path: str) -> bool:
        name = rel_path.rsplit("/", 1)[-1]
        return (
            name.casefold() in BUILTIN_EXCLUDED_NAMES
            or name.startswith(".")
            or name.startswith("~$")
        )

    @staticmethod
    def _matches(spec: GitIgnoreSpec, rel_path: str, is_dir: bool) -> bool:
        if spec.match_file(rel_path):
            return True
        return is_dir and spec.match_file(rel_path + "/")

    def _excluded(self, rel_path: str, is_dir: bool) -> bool:
        if self._builtin_excluded(rel_path):
            return True
        if self._matches(self._exclude, rel_path, is_dir):
            return True
        # The .onyxignore of every ancestor folder applies to its subtree.
        parts = rel_path.split("/")
        for depth in range(len(parts)):
            spec = self._ignores.get("/".join(parts[:depth]))
            if spec is not None and self._matches(spec, "/".join(parts[depth:]), is_dir):
                return True
        return False

    def check_dir(self, rel_path: str) -> SkipReason | None:
        return SkipReason.EXCLUDED if self._excluded(rel_path, True) else None

    def check_file(self, rel_path: str, size: int) -> SkipReason | None:
        if self._excluded(rel_path, False):
            return SkipReason.EXCLUDED
        name = rel_path.rsplit("/", 1)[-1]
        extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if extension not in self._extensions:
            return SkipReason.EXTENSION
        if size > self._max_bytes:
            return SkipReason.TOO_LARGE
        return None


def build_filter(root: RootConfig, defaults: Defaults) -> PathFilter:
    """Build the filter chain for one root."""
    return _RootFilter(root, defaults)
