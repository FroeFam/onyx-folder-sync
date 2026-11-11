"""I1: a directory that raised an error during the walk causes zero deletes beneath it."""

from __future__ import annotations

import os

from onyx_folder_sync.models import ExitCode
from tests.fixtures import failing_scandir
from tests.invariants.harness import Env


def _populate(env: Env) -> None:
    env.tree.file("a/x1.txt", "x1")
    env.tree.file("a/x2.txt", "x2")
    env.tree.file("a/deep/z.txt", "z")
    env.tree.file("b/y.txt", "y")
    env.tree.file("top.txt", "top")
    report = env.run()
    assert report.exit_code == ExitCode.OK
    assert len(env.onyx_content()) == 5


def test_i1_errored_directory_causes_no_deletes_beneath_it(env: Env) -> None:
    _populate(env)
    env.tree.remove("a/x1.txt")
    env.tree.remove("a/x2.txt")
    env.tree.remove("a/deep/z.txt")
    scandir = failing_scandir(env.tree.path("a"))
    mark = env.mark()
    for _ in range(5):
        report = env.run(scandir=scandir)
        assert report.exit_code == ExitCode.OK
        assert "a" in report.roots[0].unknown_subtrees
    assert env.delete_ids(mark) == []
    assert set(env.onyx_content()) == {"a/x1.txt", "a/x2.txt", "a/deep/z.txt", "b/y.txt", "top.txt"}
    for rel in ("a/x1.txt", "a/x2.txt", "a/deep/z.txt"):
        assert env.documents()[rel].missing_count == 0


def test_i1_errored_directory_does_not_raise_missing_count_of_files_beneath(env: Env) -> None:
    _populate(env)
    env.tree.remove("a/x1.txt")
    env.run()  # complete walk: x1 is missing once
    assert env.documents()["a/x1.txt"].missing_count == 1
    mark = env.mark()
    report = env.run(scandir=failing_scandir(env.tree.path("a")))
    assert report.exit_code == ExitCode.OK
    assert env.delete_ids(mark) == []
    assert env.documents()["a/x1.txt"].missing_count <= 1
    assert "a/x1.txt" in env.onyx_content()


def test_i1_files_beside_an_errored_directory_are_still_processed(env: Env) -> None:
    _populate(env)
    env.tree.remove("b/y.txt")
    env.tree.touch_changed("top.txt", "top changed")
    scandir = failing_scandir(env.tree.path("a"))
    env.run(scandir=scandir)
    mark = env.mark()
    report = env.run(scandir=scandir)
    # the sibling b/y.txt is proven absent in two walks and goes; nothing beneath a/ does
    assert env.delete_ids(mark) == [env.doc_id("b/y.txt")]
    assert report.roots[0].deleted == 1
    assert env.onyx_content()["top.txt"] == "top changed"
    assert {"a/x1.txt", "a/x2.txt", "a/deep/z.txt"} <= set(env.onyx_content())


def test_i1_error_below_the_top_of_a_subtree_protects_only_that_subtree(env: Env) -> None:
    _populate(env)
    env.tree.remove("a/deep/z.txt")
    env.tree.remove("a/x1.txt")
    scandir = failing_scandir(env.tree.path("a/deep"))
    env.run(scandir=scandir)
    env.run(scandir=scandir)
    assert "a/deep/z.txt" in env.onyx_content()
    assert "a/x1.txt" not in env.onyx_content()


def test_i1_directory_that_fails_only_during_the_second_walk(env: Env) -> None:
    _populate(env)
    env.tree.remove("a/x2.txt")
    env.run()
    scandir = failing_scandir(env.tree.path("a"))
    mark = env.mark()
    env.run(scandir=scandir)
    assert env.delete_ids(mark) == []
    assert "a/x2.txt" in env.onyx_content()


def test_i1_unreadable_directory_error_kind_does_not_matter(env: Env) -> None:
    _populate(env)
    env.tree.remove("a/x1.txt")
    mark = env.mark()
    for error in (PermissionError, FileNotFoundError, OSError):
        scandir = failing_scandir(env.tree.path("a"), error=error)
        for _ in range(2):
            env.run(scandir=scandir)
    assert env.delete_ids(mark) == []
    assert os.path.exists(env.tree.path("a"))
