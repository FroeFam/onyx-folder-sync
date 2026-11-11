"""End-to-end fault scenarios from plan section 7.2, row 8."""

from __future__ import annotations

import shutil
import sys
from typing import Any

import pytest

from onyx_folder_sync.models import ExitCode, SkipReason, SyncOptions
from onyx_folder_sync.roots import forget_root
from tests.fixtures import failing_scandir
from tests.invariants.harness import Env, dead_url, dropping_scandir

# -- unreadable subfolder ------------------------------------------------------------------------


def test_scenario_unreadable_subfolder_at_os_level(env: Env) -> None:
    if sys.platform == "win32":
        pytest.skip(
            "icacls cannot undo the deny ACL on private temp folders (access denied), "
            "which would leave undeletable folders behind; failing_scandir covers the logic"
        )
    env.tree.file("open/a.txt", "a")
    env.tree.file("locked/b.txt", "b")
    env.tree.file("locked/deeper/c.txt", "c")
    env.tree.file("top.txt", "top")
    assert env.run().exit_code == ExitCode.OK
    if not env.tree.make_unreadable("locked"):
        pytest.skip("the OS did not deny listing of the folder (for example running as root)")
    env.tree.file("open/new.txt", "new")
    mark = env.mark()
    for _ in range(3):
        report = env.run()
        assert report.exit_code == ExitCode.OK
        assert "locked" in report.roots[0].unknown_subtrees
    assert env.delete_ids(mark) == []
    assert {"locked/b.txt", "locked/deeper/c.txt"} <= set(env.onyx_content())
    assert env.onyx_content()["open/new.txt"] == "new"
    env.tree.restore()
    assert env.run().exit_code == ExitCode.OK
    assert env.onyx_content() == env.disk_content()


def test_scenario_unreadable_subfolder_on_a_first_run_pushes_the_rest(env: Env) -> None:
    env.tree.file("open/a.txt", "a")
    env.tree.file("locked/b.txt", "b")
    report = env.run(scandir=failing_scandir(env.tree.path("locked")))
    assert report.exit_code == ExitCode.OK
    assert report.roots[0].unknown_subtrees == ["locked"]
    assert set(env.onyx_content()) == {"open/a.txt"}


# -- share dropped mid-walk ----------------------------------------------------------------------


def test_scenario_share_drops_during_the_walk(env: Env) -> None:
    for d in range(4):
        env.tree.file(f"d{d}/one.txt", f"d{d} one")
        env.tree.file(f"d{d}/two.txt", f"d{d} two")
    assert env.run().exit_code == ExitCode.OK
    mark = env.mark()
    # the root listing and the first directory work, then the share is gone
    for _ in range(4):
        report = env.run(scandir=dropping_scandir(2))
        assert report.exit_code == ExitCode.OK
        assert report.roots[0].deleted == 0
    assert env.delete_ids(mark) == []
    assert len(env.onyx_content()) == 8
    # the share is back
    assert env.run().exit_code == ExitCode.OK
    assert env.delete_ids(mark) == []
    assert env.onyx_content() == env.disk_content()


def test_scenario_share_drops_before_the_root_listing_completes(env: Env) -> None:
    env.tree.files(6)
    env.run()
    mark = env.mark()
    for _ in range(3):
        report = env.run(scandir=dropping_scandir(0))
        assert report.exit_code == ExitCode.CONFIG_OR_STATE
    assert env.delete_ids(mark) == []


def test_scenario_share_drops_between_two_walks_of_a_missing_file(env: Env) -> None:
    env.tree.file("d0/a.txt", "a")
    env.tree.file("d1/b.txt", "b")
    env.tree.file("d1/c.txt", "c")
    env.run()
    env.tree.remove("d1/c.txt")
    env.run()  # c.txt missing once
    mark = env.mark()
    env.run(scandir=failing_scandir(env.tree.path("d1")))  # d1 is unreadable in this walk
    assert env.delete_ids(mark) == []
    assert "d1/c.txt" in env.onyx_content()


# -- unmounted root ------------------------------------------------------------------------------


def test_scenario_unmounted_root_then_remounted(env: Env) -> None:
    env.tree.files(8)
    env.run()
    saved = env.tmp_path / "mounted"
    env.root.rename(saved)
    mark = env.mark()
    for _ in range(3):
        report = env.run()
        assert report.exit_code == ExitCode.CONFIG_OR_STATE
    assert env.delete_ids(mark) == []
    saved.rename(env.root)
    report = env.run()
    assert report.exit_code == ExitCode.OK
    assert report.roots[0].unchanged == 8
    assert env.delete_ids(mark) == []


# -- state restored from an old copy -------------------------------------------------------------


def test_scenario_state_restored_from_an_old_copy(env: Env) -> None:
    for name in ("a", "b", "c"):
        env.tree.file(f"{name}.txt", f"{name} original")
    assert env.run().exit_code == ExitCode.OK
    backup = env.tmp_path / "state-backup.db"
    shutil.copyfile(env.state_path, backup)

    env.tree.file("d.txt", "d added later")
    env.tree.remove("a.txt")
    env.run()
    env.run()
    assert "a.txt" not in env.onyx_content()
    assert env.onyx_content() == env.disk_content()

    # restore the stale state: it knows a.txt (deleted in Onyx) and not d.txt
    for leftover in env.state_path.parent.glob(env.state_path.name + "-*"):
        leftover.unlink()
    shutil.copyfile(backup, env.state_path)
    mark = env.mark()
    for _ in range(3):
        report = env.run()
        assert report.exit_code == ExitCode.OK
        env.assert_no_wrong_deletes()
    sent = env.delete_ids(mark)
    assert env.doc_id("d.txt") not in sent
    assert env.doc_id("b.txt") not in sent
    assert env.doc_id("c.txt") not in sent

    report = env.reconcile()
    assert report.exit_code == ExitCode.OK
    env.assert_no_wrong_deletes()
    assert env.onyx_content() == env.disk_content()
    assert set(env.documents()) == set(env.disk_content())


# -- entry deleted in Onyx -----------------------------------------------------------------------


def test_scenario_entry_deleted_in_onyx_is_refused_and_recoverable(env: Env) -> None:
    env.tree.files(5)
    env.run()
    cc = env.cc_pair_id()
    env.fake.drop_entry(cc)
    mark = env.mark()
    for _ in range(2):
        report = env.run()
        assert report.exit_code == ExitCode.CONFIG_OR_STATE
    assert env.writes(mark) == []
    # recovery: drop the stale state, the next run creates a fresh entry and pushes everything
    with env.deps() as deps:
        forget_root(deps, "main", state_only=True)
    report = env.run()
    assert report.exit_code == ExitCode.OK
    assert env.cc_pair_id() != cc
    assert env.onyx_content() == env.disk_content()


# -- pruning switched on -------------------------------------------------------------------------


def test_scenario_pruning_switched_on_is_detected_and_reconcile_repairs(env: Env) -> None:
    env.tree.files(6)
    env.run()
    cc = env.cc_pair_id()
    env.fake.set_prune_freq(cc, 604800)
    env.fake.run_prune(cc)  # Onyx prunes every pushed document
    assert env.fake.documents_of(cc) == []
    mark = env.mark()
    for _ in range(2):
        report = env.run()
        assert report.exit_code == ExitCode.CONFIG_OR_STATE
    assert env.writes(mark) == []
    env.fake.set_prune_freq(cc, None)
    assert env.run().exit_code == ExitCode.OK
    # a normal run never asks Onyx what it holds: state still believes the documents exist
    assert env.fake.documents_of(cc) == []
    report = env.reconcile()
    assert report.exit_code == ExitCode.OK
    assert env.onyx_content() == env.disk_content()
    assert env.delete_ids() == []


# -- behaviours the plan states outside the invariants -------------------------------------------


def test_scenario_extraction_error_keeps_the_document_and_is_retried(env: Env) -> None:
    env.tree.file("a.txt", "version one")
    env.tree.file("b.txt", "b")
    env.run()
    env.tree.touch_changed("a.txt", "version two")
    env.runner.errors.add(str(env.tree.path("a.txt")))
    report = env.run()
    assert report.exit_code == ExitCode.OK
    assert report.roots[0].failed == 0
    assert report.roots[0].skipped.get(SkipReason.EXTRACT_ERROR) == 1
    assert env.onyx_content()["a.txt"] == "version one"
    assert env.documents()["a.txt"].onyx_id is not None
    assert env.documents()["a.txt"].last_error
    calls_before = len(env.runner.calls)
    env.run()
    assert len(env.runner.calls) == calls_before + 1  # retried although nothing changed
    env.runner.errors.clear()
    report = env.run()
    assert report.roots[0].pushed == 1
    assert env.onyx_content()["a.txt"] == "version two"
    assert env.documents()["a.txt"].last_error is None


def test_scenario_extraction_timeout_keeps_the_document_and_is_retried(env: Env) -> None:
    env.tree.file("a.txt", "version one")
    env.tree.file("b.txt", "b")
    env.run()
    env.tree.touch_changed("a.txt", "version two")
    env.runner.timeouts.add(str(env.tree.path("a.txt")))
    for _ in range(2):
        report = env.run()
        assert report.exit_code == ExitCode.OK
        assert report.roots[0].failed == 0
        assert report.roots[0].skipped.get(SkipReason.EXTRACT_TIMEOUT) == 1
    assert env.onyx_content()["a.txt"] == "version one"
    env.runner.timeouts.clear()
    env.run()
    assert env.onyx_content()["a.txt"] == "version two"


def test_scenario_extraction_errors_never_cause_deletes(env: Env) -> None:
    env.tree.files(15)
    env.run()
    for rel in list(env.disk_content()):
        env.tree.touch_changed(rel, "changed " + rel)
        env.runner.errors.add(str(env.tree.path(rel)))
    mark = env.mark()
    report = env.run()
    assert report.exit_code == ExitCode.OK
    assert env.delete_ids(mark) == []
    assert len(env.onyx_content()) == 15


def test_scenario_dry_run_writes_nothing_to_onyx_or_state(env: Env) -> None:
    env.tree.files(4)
    env.run()
    env.tree.file("new.txt", "new")
    env.tree.remove("doc0000.txt")
    env.tree.touch_changed("doc0001.txt", "changed")
    docs_before = env.documents()
    onyx_before = {k: v.text for k, v in env.fake.documents.items()}
    calls_before = len(env.runner.calls)
    mark = env.mark()
    report = env.run(options=SyncOptions(dry_run=True))
    assert report.exit_code == ExitCode.OK
    assert env.writes(mark) == []
    assert env.documents() == docs_before
    assert {k: v.text for k, v in env.fake.documents.items()} == onyx_before
    assert len(env.runner.calls) == calls_before
    assert "new.txt" in report.roots[0].would_push


def test_scenario_dry_run_on_fresh_state_creates_no_entry(env: Env) -> None:
    env.tree.files(3)
    report = env.run(options=SyncOptions(dry_run=True))
    assert report.exit_code == ExitCode.OK
    assert env.writes() == []
    assert env.fake.entries == {}
    assert env.fake.documents == {}


def test_scenario_onyx_unreachable_at_preflight_exits_0_without_writes(env: Env) -> None:
    env.tree.files(3)
    env.run()
    env.tree.file("new.txt", "new")
    env.tree.remove("doc0000.txt")
    docs_before = env.documents()
    report = env.run(url=dead_url())
    assert report.exit_code == ExitCode.OK
    assert env.documents() == docs_before
    assert report.counts.pushed == 0
    assert report.counts.deleted == 0


def test_scenario_authentication_failure_exits_4_and_writes_nothing(env: Env) -> None:
    env.tree.files(3)
    mark = env.mark()
    report = env.run(token="not-the-token")  # noqa: S106
    assert report.exit_code == ExitCode.AUTH_FAILURE
    assert env.writes(mark) == []
    assert env.documents() == {}


def test_scenario_failed_pushes_exit_1(env: Env) -> None:
    env.tree.files(3)
    env.fake.fail_next("POST", "/onyx-api/ingestion$", 500, times=3)
    report = env.run()
    assert report.exit_code == ExitCode.DOCUMENT_FAILURES
    assert report.roots[0].failed == 1


def test_scenario_repush_sends_everything_again(env: Env) -> None:
    env.tree.files(4)
    env.run()
    mark = env.mark()
    report = env.run()
    assert env.pushes(mark) == []
    assert report.roots[0].unchanged == 4
    report = env.run(options=SyncOptions(repush=True))
    assert len(env.pushes(mark)) == 4
    assert report.exit_code == ExitCode.OK


def test_scenario_many_runs_with_random_faults_never_delete_existing_files(env: Env) -> None:
    """A fixed sequence of mixed faults; no document of an existing file is ever deleted."""
    for i in range(12):
        env.tree.file(f"d{i % 3}/f{i}.txt", f"f{i}")
    env.run()
    faults: list[dict[str, Any]] = [
        {"scandir": failing_scandir(env.tree.path("d0"))},
        {"scandir": dropping_scandir(1)},
        {"scandir": failing_scandir(env.tree.path("d1"), error=FileNotFoundError)},
        {},
        {"scandir": dropping_scandir(3)},
        {},
    ]
    env.tree.remove("d0/f0.txt")
    env.tree.remove("d2/f2.txt")
    for fault in faults:
        env.run(**fault)
        env.assert_no_wrong_deletes()
    for _ in range(3):
        env.run()
    assert env.onyx_content() == env.disk_content()
