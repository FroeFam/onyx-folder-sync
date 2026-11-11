"""I3: a missing, unmounted or empty root causes zero deletes and a non-zero exit."""

from __future__ import annotations

import shutil

from onyx_folder_sync.models import ExitCode
from tests.fixtures import TreeBuilder, failing_scandir
from tests.invariants.harness import Env


def _populated(env: Env, count: int = 6) -> None:
    env.tree.files(count)
    assert env.run().exit_code == ExitCode.OK
    assert len(env.onyx_content()) == count


def test_i3_missing_root_causes_zero_deletes_and_exit_2(env: Env) -> None:
    _populated(env)
    shutil.rmtree(env.root)
    mark = env.mark()
    for _ in range(4):
        report = env.run()
        assert report.exit_code == ExitCode.CONFIG_OR_STATE
        assert report.messages
    assert env.delete_ids(mark) == []
    assert env.pushes(mark) == []
    assert len(env.onyx_content()) == 6


def test_i3_root_that_is_a_file_causes_zero_deletes_and_exit_2(env: Env) -> None:
    _populated(env)
    shutil.rmtree(env.root)
    env.root.write_text("not a directory", encoding="utf-8")
    mark = env.mark()
    report = env.run()
    assert report.exit_code == ExitCode.CONFIG_OR_STATE
    assert env.delete_ids(mark) == []


def test_i3_empty_unmounted_mount_point_causes_zero_deletes_and_exit_2(env: Env) -> None:
    _populated(env)
    off = env.tmp_path / "data.off"
    env.root.rename(off)
    env.root.mkdir()  # the mount point exists but nothing is mounted
    mark = env.mark()
    for _ in range(4):
        report = env.run()
        assert report.exit_code == ExitCode.CONFIG_OR_STATE
    assert env.delete_ids(mark) == []
    assert len(env.onyx_content()) == 6
    # the share comes back: nothing was lost, nothing is deleted afterwards either
    env.root.rmdir()
    off.rename(env.root)
    for _ in range(3):
        report = env.run()
        assert report.exit_code == ExitCode.OK
    assert env.delete_ids(mark) == []
    assert len(env.onyx_content()) == 6


def test_i3_root_with_only_ineligible_files_counts_as_zero_eligible(env: Env) -> None:
    _populated(env)
    for rel in list(env.disk_content()):
        env.tree.remove(rel)
    env.tree.file("photo.jpg", b"\xff\xd8\xff")
    env.tree.file(".hidden.txt", "hidden")
    mark = env.mark()
    for _ in range(3):
        report = env.run()
        assert report.exit_code == ExitCode.CONFIG_OR_STATE
    assert env.delete_ids(mark) == []
    assert len(env.onyx_content()) == 6


def test_i3_root_that_cannot_be_listed_at_all_causes_zero_deletes(env: Env) -> None:
    _populated(env)
    mark = env.mark()
    for _ in range(3):
        report = env.run(scandir=failing_scandir(env.root))
        assert report.exit_code != ExitCode.OK
    assert env.delete_ids(mark) == []
    assert len(env.onyx_content()) == 6


def test_i3_one_unavailable_root_stops_the_whole_run_before_any_push_or_delete(env: Env) -> None:
    other = env.tmp_path / "other"
    env.tree.files(4)
    env.tree.file("old.txt", "old")
    other_tree = TreeBuilder(other)
    other_tree.files(4, prefix="o")
    config = env.make_config(roots={"main": env.root, "other": other})
    assert env.run(config=config).exit_code == ExitCode.OK

    env.tree.remove("old.txt")
    env.run(config=config)  # old.txt is missing once; the next walk would delete it
    env.tree.file("new-in-main.txt", "new")
    shutil.rmtree(other)
    mark = env.mark()
    report = env.run(config=config)
    assert report.exit_code == ExitCode.CONFIG_OR_STATE
    assert env.pushes(mark) == []
    assert env.delete_ids(mark) == []
    assert "new-in-main.txt" not in {
        str(d.metadata["path"]) for d in env.fake.documents_of(env.cc_pair_id("main"))
    }


def test_i3_fresh_state_with_empty_root_is_not_an_error_for_existing_documents(env: Env) -> None:
    """Zero eligible files only aborts when state holds documents (plan 5.2 step 3)."""
    report = env.run()
    assert report.exit_code == ExitCode.OK
    assert env.delete_ids() == []
