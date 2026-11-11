from __future__ import annotations

import sys
from pathlib import Path

import pytest

from onyx_folder_sync.interfaces import RootUnavailableError
from onyx_folder_sync.models import SkipReason
from onyx_folder_sync.walk import is_placeholder_attributes, walk_root
from tests.fixtures import NFC_NAME, NFD_NAME, TreeBuilder, failing_scandir


class FakeFilter:
    def __init__(
        self,
        *,
        excluded: set[str] | None = None,
        extensions: set[str] | None = None,
        max_size: int | None = None,
        ignore_errors: set[str] | None = None,
    ) -> None:
        self.excluded = excluded or set()
        self.extensions = extensions
        self.max_size = max_size
        self.ignore_errors = ignore_errors or set()
        self.ignore_calls: list[str] = []
        self.file_checks: list[str] = []

    def load_ignore(self, rel_dir: str, abs_dir: str) -> None:
        self.ignore_calls.append(rel_dir)
        if rel_dir in self.ignore_errors:
            raise PermissionError(abs_dir)

    def check_dir(self, rel_path: str) -> SkipReason | None:
        return SkipReason.EXCLUDED if rel_path.rsplit("/", 1)[-1] in self.excluded else None

    def check_file(self, rel_path: str, size: int) -> SkipReason | None:
        self.file_checks.append(rel_path)
        name = rel_path.rsplit("/", 1)[-1]
        if name in self.excluded:
            return SkipReason.EXCLUDED
        if self.extensions is not None and Path(name).suffix not in self.extensions:
            return SkipReason.EXTENSION
        if self.max_size is not None and size > self.max_size:
            return SkipReason.TOO_LARGE
        return None


def test_nested_tree_sorted(tmp_path: Path) -> None:
    tree = TreeBuilder(tmp_path / "root")
    tree.file("b.txt", "bb")
    tree.file("a/z.txt", "z")
    tree.file("a/b/c.txt", "ccc")
    tree.file("a.txt", "a")
    result = walk_root(tree.root, FakeFilter())
    assert [f.rel_path for f in result.eligible] == ["a.txt", "a/b/c.txt", "a/z.txt", "b.txt"]
    by_rel = {f.rel_path: f for f in result.eligible}
    assert by_rel["a/b/c.txt"].size == 3
    assert by_rel["a/b/c.txt"].abs_path == str(tree.path("a/b/c.txt"))
    assert by_rel["a/b/c.txt"].mtime_ns == tree.path("a/b/c.txt").stat().st_mtime_ns
    assert result.skipped == ()
    assert result.errored_dirs == ()
    assert result.complete


def test_filter_reasons_passed_through(tmp_path: Path) -> None:
    tree = TreeBuilder(tmp_path / "root")
    tree.file("keep.txt", "x")
    tree.file("img.png", "x")
    tree.file("big.txt", "x" * 50)
    tree.file("skipme.txt", "x")
    tree.file("skipdir/inner.txt", "x")
    flt = FakeFilter(excluded={"skipme.txt", "skipdir"}, extensions={".txt"}, max_size=10)
    result = walk_root(tree.root, flt)
    assert [f.rel_path for f in result.eligible] == ["keep.txt"]
    assert {s.rel_path: s.reason for s in result.skipped} == {
        "big.txt": SkipReason.TOO_LARGE,
        "img.png": SkipReason.EXTENSION,
        "skipme.txt": SkipReason.EXCLUDED,
    }
    assert "skipdir/inner.txt" not in flt.file_checks
    assert "skipdir" not in flt.ignore_calls


def test_load_ignore_order_and_errors(tmp_path: Path) -> None:
    tree = TreeBuilder(tmp_path / "root")
    tree.file("a/b/f.txt")
    tree.file("c/g.txt")
    flt = FakeFilter()
    walk_root(tree.root, flt)
    assert flt.ignore_calls[0] == ""
    assert flt.ignore_calls.index("a") < flt.ignore_calls.index("a/b")
    assert sorted(flt.ignore_calls) == ["", "a", "a/b", "c"]


def test_load_ignore_error_marks_dir_errored(tmp_path: Path) -> None:
    tree = TreeBuilder(tmp_path / "root")
    tree.file("ok.txt")
    tree.file("a/f.txt")
    tree.file("a/b/g.txt")
    result = walk_root(tree.root, FakeFilter(ignore_errors={"a"}))
    assert result.errored_dirs == ("a",)
    assert [f.rel_path for f in result.eligible] == ["ok.txt"]
    root_error = walk_root(tree.root, FakeFilter(ignore_errors={""}))
    assert root_error.errored_dirs == ("",)
    assert root_error.eligible == ()


def test_errored_directory_via_failing_scandir(tmp_path: Path) -> None:
    tree = TreeBuilder(tmp_path / "root")
    tree.file("ok.txt")
    tree.file("bad/f.txt")
    tree.file("bad/deep/g.txt")
    tree.file("good/h.txt")
    result = walk_root(tree.root, FakeFilter(), scandir=failing_scandir(tree.path("bad")))
    assert result.errored_dirs == ("bad",)
    assert [f.rel_path for f in result.eligible] == ["good/h.txt", "ok.txt"]
    assert not any(s.rel_path.startswith("bad") for s in result.skipped)
    assert result.is_unknown("bad/f.txt")
    assert result.is_unknown("bad/deep/g.txt")
    assert not result.is_unknown("good/h.txt")
    assert not result.complete


def test_nested_error_discards_nothing_elsewhere(tmp_path: Path) -> None:
    tree = TreeBuilder(tmp_path / "root")
    tree.file("a/f.txt")
    tree.file("a/deep/g.txt")
    result = walk_root(tree.root, FakeFilter(), scandir=failing_scandir(tree.path("a/deep")))
    assert result.errored_dirs == ("a/deep",)
    assert [f.rel_path for f in result.eligible] == ["a/f.txt"]


def test_real_unreadable_directory(tmp_path: Path) -> None:
    tree = TreeBuilder(tmp_path / "root")
    tree.file("ok.txt")
    tree.file("locked/f.txt")
    try:
        if not tree.make_unreadable("locked"):
            pytest.skip("cannot deny directory listing on this platform or user")
        result = walk_root(tree.root, FakeFilter())
    finally:
        tree.restore()
    assert result.errored_dirs == ("locked",)
    assert [f.rel_path for f in result.eligible] == ["ok.txt"]


def test_symlinks_never_followed(tmp_path: Path) -> None:
    outside = TreeBuilder(tmp_path / "outside")
    outside.file("secret.txt", "secret")
    outside.file("dir/inner.txt", "inner")
    tree = TreeBuilder(tmp_path / "root")
    tree.file("real.txt")
    if not tree.symlink("file_link.txt", outside.path("secret.txt")):
        pytest.skip("symlinks not permitted here")
    assert tree.symlink("dir_link", outside.path("dir"))
    flt = FakeFilter()
    result = walk_root(tree.root, flt)
    assert [f.rel_path for f in result.eligible] == ["real.txt"]
    assert {s.rel_path: s.reason for s in result.skipped} == {
        "dir_link": SkipReason.SYMLINK,
        "file_link.txt": SkipReason.SYMLINK,
    }
    assert flt.file_checks == ["real.txt"]
    assert flt.ignore_calls == [""]


@pytest.mark.skipif(sys.platform != "win32", reason="junctions exist only on Windows")
def test_junction_never_followed(tmp_path: Path) -> None:
    outside = TreeBuilder(tmp_path / "outside")
    outside.file("inner.txt", "inner")
    tree = TreeBuilder(tmp_path / "root")
    tree.file("real.txt")
    if not tree.junction("junc", outside.root):
        pytest.skip("cannot create a junction here")
    flt = FakeFilter()
    result = walk_root(tree.root, flt)
    assert [f.rel_path for f in result.eligible] == ["real.txt"]
    assert [(s.rel_path, s.reason) for s in result.skipped] == [("junc", SkipReason.SYMLINK)]
    assert flt.ignore_calls == [""]


def test_unicode_nfd_name(tmp_path: Path) -> None:
    tree = TreeBuilder(tmp_path / "root")
    tree.file(f"dir/{NFD_NAME}", "unicode body")
    result = walk_root(tree.root, FakeFilter())
    assert len(result.eligible) == 1
    entry = result.eligible[0]
    assert entry.rel_path == f"dir/{NFC_NAME}"
    assert Path(entry.abs_path).read_text(encoding="utf-8") == "unicode body"
    assert entry.abs_path.endswith(NFD_NAME) or sys.platform == "darwin"


def test_placeholder_attributes() -> None:
    assert not is_placeholder_attributes(0)
    assert not is_placeholder_attributes(0x20 | 0x2)
    assert is_placeholder_attributes(0x1000)
    assert is_placeholder_attributes(0x40000)
    assert is_placeholder_attributes(0x400000)
    assert is_placeholder_attributes(0x20 | 0x400000)


def test_descend_excluded(tmp_path: Path) -> None:
    tree = TreeBuilder(tmp_path / "root")
    tree.file("keep.txt")
    tree.file("skipdir/a.txt")
    tree.file("skipdir/sub/b.txt")
    flt = FakeFilter(excluded={"skipdir"})
    result = walk_root(tree.root, flt, descend_excluded=True)
    assert [f.rel_path for f in result.eligible] == ["keep.txt"]
    assert {s.rel_path: s.reason for s in result.skipped} == {
        "skipdir/a.txt": SkipReason.EXCLUDED,
        "skipdir/sub/b.txt": SkipReason.EXCLUDED,
    }
    assert flt.ignore_calls == [""]
    assert flt.file_checks == ["keep.txt"]


def test_descend_excluded_listing_error(tmp_path: Path) -> None:
    tree = TreeBuilder(tmp_path / "root")
    tree.file("skipdir/sub/b.txt")
    result = walk_root(
        tree.root,
        FakeFilter(excluded={"skipdir"}),
        descend_excluded=True,
        scandir=failing_scandir(tree.path("skipdir/sub")),
    )
    assert result.errored_dirs == ("skipdir/sub",)


def test_descend_excluded_does_not_follow_links(tmp_path: Path) -> None:
    outside = TreeBuilder(tmp_path / "outside")
    outside.file("x.txt")
    tree = TreeBuilder(tmp_path / "root")
    tree.directory("skipdir")
    if not tree.symlink("skipdir/link", outside.root):
        pytest.skip("symlinks not permitted here")
    result = walk_root(tree.root, FakeFilter(excluded={"skipdir"}), descend_excluded=True)
    assert [(s.rel_path, s.reason) for s in result.skipped] == [
        ("skipdir/link", SkipReason.SYMLINK)
    ]


def test_missing_root(tmp_path: Path) -> None:
    with pytest.raises(RootUnavailableError):
        walk_root(tmp_path / "nope", FakeFilter())


def test_root_is_a_file(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(RootUnavailableError):
        walk_root(target, FakeFilter())


def test_root_listing_failure(tmp_path: Path) -> None:
    tree = TreeBuilder(tmp_path / "root")
    tree.file("a.txt")
    with pytest.raises(RootUnavailableError):
        walk_root(tree.root, FakeFilter(), scandir=failing_scandir(tree.root))
