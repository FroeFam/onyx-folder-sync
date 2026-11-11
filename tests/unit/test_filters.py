from __future__ import annotations

import builtins
from pathlib import Path
from typing import Any

import pytest

from onyx_folder_sync.filters import build_filter
from onyx_folder_sync.interfaces import PathFilter
from onyx_folder_sync.models import Defaults, RootConfig, SkipReason
from tests.fixtures import TreeBuilder

MB = 1024 * 1024


def make(
    exclude: tuple[str, ...] = (),
    defaults: Defaults | None = None,
) -> PathFilter:
    return build_filter(RootConfig(name="r", path="/r", exclude=exclude), defaults or Defaults())


@pytest.mark.parametrize(
    "rel",
    [
        ".git",
        "a/.GIT",
        "node_modules",
        "x/@eaDir",
        "#recycle",
        "$RECYCLE.BIN",
        "System Volume Information",
        "a/Thumbs.db",
        ".DS_Store",
        ".hidden",
        "a/.hidden",
        "a/~$lock.docx",
    ],
)
def test_builtin_excludes(rel: str) -> None:
    f = make()
    assert f.check_dir(rel) is SkipReason.EXCLUDED
    assert f.check_file(rel, 1) is SkipReason.EXCLUDED


def test_plain_entries_pass() -> None:
    f = make()
    assert f.check_dir("a/b") is None
    assert f.check_file("a/b/report.PDF", 10) is None
    assert f.check_file("notes.txt", 0) is None


def test_config_globs() -> None:
    f = make(exclude=("drafts/", "*.bak.txt", "/top.txt"))
    assert f.check_dir("drafts") is SkipReason.EXCLUDED
    assert f.check_dir("sub/drafts") is SkipReason.EXCLUDED
    assert f.check_file("drafts/a.txt", 1) is SkipReason.EXCLUDED
    assert f.check_file("sub/x.bak.txt", 1) is SkipReason.EXCLUDED
    assert f.check_file("top.txt", 1) is SkipReason.EXCLUDED
    assert f.check_file("sub/top.txt", 1) is None
    assert f.check_dir("other") is None


def test_extension_allowlist() -> None:
    f = make()
    assert f.check_file("a.exe", 1) is SkipReason.EXTENSION
    assert f.check_file("noext", 1) is SkipReason.EXTENSION
    assert f.check_file("a.md", 1) is None
    custom = make(defaults=Defaults(extensions=(".JSON", "txt")))
    assert custom.check_file("a.json", 1) is None
    assert custom.check_file("a.pdf", 1) is SkipReason.EXTENSION


def test_size_cap_boundary() -> None:
    f = make(defaults=Defaults(max_file_mb=2))
    assert f.check_file("a.txt", 2 * MB) is None
    assert f.check_file("a.txt", 2 * MB + 1) is SkipReason.TOO_LARGE


def test_filter_order() -> None:
    f = make(exclude=("*.exe", "big.txt"), defaults=Defaults(max_file_mb=1))
    # excluded wins over extension and size
    assert f.check_file("x.exe", 5 * MB) is SkipReason.EXCLUDED
    assert f.check_file("big.txt", 5 * MB) is SkipReason.EXCLUDED
    # builtin wins over the rest
    assert f.check_file(".hidden.exe", 5 * MB) is SkipReason.EXCLUDED
    # extension wins over size
    assert f.check_file("x.bin", 5 * MB) is SkipReason.EXTENSION
    assert f.check_file("y.txt", 5 * MB) is SkipReason.TOO_LARGE
    assert f.check_file("y.txt", 1) is None


def test_onyxignore_order_over_extension_and_size(tmp_path: Path) -> None:
    tree = TreeBuilder(tmp_path / "root")
    tree.file(".onyxignore", "*.exe\nbig.txt\n")
    f = make(defaults=Defaults(max_file_mb=1))
    f.load_ignore("", str(tree.root))
    assert f.check_file("a.exe", 5 * MB) is SkipReason.EXCLUDED
    assert f.check_file("big.txt", 5 * MB) is SkipReason.EXCLUDED
    assert f.check_file("other.txt", 5 * MB) is SkipReason.TOO_LARGE


def test_missing_onyxignore_is_not_an_error(tmp_path: Path) -> None:
    f = make()
    f.load_ignore("", str(tmp_path))
    assert f.check_file("a.txt", 1) is None


def test_nested_onyxignore_files(tmp_path: Path) -> None:
    tree = TreeBuilder(tmp_path / "root")
    tree.file(".onyxignore", "root-only.txt\n/anchored.txt\n")
    tree.file("sub/.onyxignore", "secret.txt\n/local.txt\ndrafts/\n")
    tree.directory("other")
    f = make()
    f.load_ignore("", str(tree.root))
    f.load_ignore("sub", str(tree.path("sub")))
    f.load_ignore("other", str(tree.path("other")))

    # root file applies everywhere below it
    assert f.check_file("root-only.txt", 1) is SkipReason.EXCLUDED
    assert f.check_file("sub/root-only.txt", 1) is SkipReason.EXCLUDED
    assert f.check_file("anchored.txt", 1) is SkipReason.EXCLUDED
    assert f.check_file("sub/anchored.txt", 1) is None
    # sub file applies to its subtree only
    assert f.check_file("sub/secret.txt", 1) is SkipReason.EXCLUDED
    assert f.check_file("sub/deep/secret.txt", 1) is SkipReason.EXCLUDED
    assert f.check_file("secret.txt", 1) is None
    assert f.check_file("other/secret.txt", 1) is None
    # anchored pattern is relative to the folder holding the ignore file
    assert f.check_file("sub/local.txt", 1) is SkipReason.EXCLUDED
    assert f.check_file("sub/deep/local.txt", 1) is None
    assert f.check_file("local.txt", 1) is None
    # directory patterns
    assert f.check_dir("sub/drafts") is SkipReason.EXCLUDED
    assert f.check_dir("sub/x/drafts") is SkipReason.EXCLUDED
    assert f.check_dir("drafts") is None
    assert f.check_file("sub/drafts/a.txt", 1) is SkipReason.EXCLUDED


def test_directory_pattern_matches_with_trailing_slash(tmp_path: Path) -> None:
    tree = TreeBuilder(tmp_path / "root")
    tree.file(".onyxignore", "build/\n")
    f = make()
    f.load_ignore("", str(tree.root))
    assert f.check_dir("build") is SkipReason.EXCLUDED
    assert f.check_dir("src/build") is SkipReason.EXCLUDED
    assert f.check_dir("builder") is None
    # a file named like the directory pattern is not matched by a dir-only pattern
    assert f.check_file("build", 1) is SkipReason.EXTENSION


def test_negation_inside_one_file(tmp_path: Path) -> None:
    tree = TreeBuilder(tmp_path / "root")
    tree.file(".onyxignore", "*.txt\n!keep.txt\n")
    f = make()
    f.load_ignore("", str(tree.root))
    assert f.check_file("a.txt", 1) is SkipReason.EXCLUDED
    assert f.check_file("keep.txt", 1) is None
    assert f.check_file("sub/keep.txt", 1) is None
    assert f.check_file("sub/a.txt", 1) is SkipReason.EXCLUDED


def test_negation_in_config_globs() -> None:
    f = make(exclude=("*.txt", "!keep.txt"))
    assert f.check_file("a.txt", 1) is SkipReason.EXCLUDED
    assert f.check_file("keep.txt", 1) is None


def test_reload_picks_up_change_and_removal(tmp_path: Path) -> None:
    tree = TreeBuilder(tmp_path / "root")
    ignore = tree.file(".onyxignore", "a.txt\n")
    f = make()
    f.load_ignore("", str(tree.root))
    assert f.check_file("a.txt", 1) is SkipReason.EXCLUDED
    ignore.unlink()
    f.load_ignore("", str(tree.root))
    assert f.check_file("a.txt", 1) is None


def test_unreadable_onyxignore_os_level(tmp_path: Path) -> None:
    tree = TreeBuilder(tmp_path / "root")
    tree.file(".onyxignore", "a.txt\n")
    if not tree.make_unreadable(".onyxignore"):
        pytest.skip("cannot make a file unreadable in this environment")
    try:
        f = make()
        with pytest.raises(OSError):
            f.load_ignore("", str(tree.root))
    finally:
        tree.restore()


def test_unreadable_onyxignore_monkeypatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = TreeBuilder(tmp_path / "root")
    tree.file(".onyxignore", "a.txt\n")
    real_open = builtins.open

    def fake_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if str(file).endswith(".onyxignore"):
            raise PermissionError("denied")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    f = make()
    with pytest.raises(PermissionError):
        f.load_ignore("", str(tree.root))
