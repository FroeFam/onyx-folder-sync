"""I2: a file is deleted from Onyx only after two consecutive complete walks without it."""

from __future__ import annotations

from onyx_folder_sync.models import MISSING_WALKS_BEFORE_DELETE, ExitCode, SyncOptions
from tests.invariants.harness import Env


def test_i2_file_is_deleted_only_in_the_run_where_missing_count_reaches_two(env: Env) -> None:
    assert MISSING_WALKS_BEFORE_DELETE == 2
    env.tree.file("keep.txt", "keep")
    env.tree.file("gone.txt", "gone")
    env.run()
    gone_id = env.doc_id("gone.txt")
    env.tree.remove("gone.txt")

    mark = env.mark()
    first = env.run()
    assert first.exit_code == ExitCode.OK
    assert env.delete_ids(mark) == []
    assert env.documents()["gone.txt"].missing_count == 1
    assert "gone.txt" in env.onyx_content()

    second = env.run()
    assert second.exit_code == ExitCode.OK
    assert env.delete_ids(mark) == [gone_id]
    assert second.roots[0].deleted == 1
    assert "gone.txt" not in env.onyx_content()
    assert "keep.txt" in env.onyx_content()


def test_i2_file_that_reappears_resets_the_count(env: Env) -> None:
    env.tree.file("flaky.txt", "flaky")
    env.tree.file("other.txt", "other")
    env.run()
    saved = env.tree.path("flaky.txt").read_text(encoding="utf-8")
    mark = env.mark()

    env.tree.remove("flaky.txt")
    env.run()
    assert env.documents()["flaky.txt"].missing_count == 1

    env.tree.file("flaky.txt", saved)
    env.run()
    assert env.documents()["flaky.txt"].missing_count == 0

    env.tree.remove("flaky.txt")
    env.run()
    # absent once more, but not in two consecutive walks
    assert env.documents()["flaky.txt"].missing_count == 1
    assert env.delete_ids(mark) == []
    assert "flaky.txt" in env.onyx_content()


def test_i2_reappearing_changed_file_resets_the_count_even_when_its_push_fails(env: Env) -> None:
    env.tree.file("flaky.txt", "flaky")
    env.tree.file("other.txt", "other")
    env.run()
    mark = env.mark()

    env.tree.remove("flaky.txt")
    env.run()
    assert env.documents()["flaky.txt"].missing_count == 1

    # Back with new content, and every attempt to push it fails.
    env.tree.file("flaky.txt", "flaky, edited")
    env.fake.fail_next("POST", "/onyx-api/ingestion$", 500, times=3)
    report = env.run()
    assert report.roots[0].failed == 1
    assert env.documents()["flaky.txt"].missing_count == 0

    env.tree.remove("flaky.txt")
    env.run()
    assert env.documents()["flaky.txt"].missing_count == 1
    assert env.delete_ids(mark) == []
    assert "flaky.txt" in env.onyx_content()


def test_i2_one_walk_without_the_file_never_deletes_however_many_files(env: Env) -> None:
    rels = env.tree.files(8)
    env.tree.file("anchor.txt", "anchor")
    env.run()
    for rel in rels:
        env.tree.remove(rel)
    mark = env.mark()
    env.run()
    assert env.delete_ids(mark) == []
    assert len(env.onyx_content()) == 9


def test_i2_missing_file_is_never_deleted_by_a_dry_run(env: Env) -> None:
    env.tree.file("keep.txt", "keep")
    env.tree.file("gone.txt", "gone")
    env.run()
    env.tree.remove("gone.txt")
    env.run()
    mark = env.mark()
    env.run(options=SyncOptions(dry_run=True))
    assert env.delete_ids(mark) == []
    assert "gone.txt" in env.onyx_content()
    assert env.documents()["gone.txt"].missing_count == 1
