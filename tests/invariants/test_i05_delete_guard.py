"""I5: the delete guard covers every delete path, and a blocked run performs zero deletes."""

from __future__ import annotations

from typing import Any

from onyx_folder_sync.models import FREE_DELETES_PER_ROOT, ExitCode, SkipReason, SyncOptions
from onyx_folder_sync.state import open_state
from tests.fixtures import TreeBuilder
from tests.invariants.harness import Env


def _seed(env: Env, total: int, **config: Any) -> list[str]:
    rels = env.tree.files(total)
    report = env.run(config=env.make_config(**config) if config else None)
    assert report.exit_code == ExitCode.OK
    assert len(env.onyx_content()) == total
    return rels


def _remove_and_confirm_missing(env: Env, rels: list[str]) -> None:
    for rel in rels:
        env.tree.remove(rel)
    mark = env.mark()
    report = env.run()  # first missing walk: nothing due yet
    assert report.exit_code == ExitCode.OK
    assert env.delete_ids(mark) == []


# -- missing files -------------------------------------------------------------------------------


def test_i5_up_to_ten_deletes_per_root_always_pass(env: Env) -> None:
    assert FREE_DELETES_PER_ROOT == 10
    rels = _seed(env, 12)  # 10 of 12 is far above the fraction
    _remove_and_confirm_missing(env, rels[:10])
    mark = env.mark()
    report = env.run()
    assert report.exit_code == ExitCode.OK
    assert report.roots[0].deleted == 10
    assert len(env.delete_ids(mark)) == 10


def test_i5_blocked_run_performs_zero_deletes_but_still_pushes(env: Env) -> None:
    rels = _seed(env, 20)
    _remove_and_confirm_missing(env, rels[:15])
    env.tree.file("brand-new.txt", "brand new")
    mark = env.mark()
    for _ in range(3):
        report = env.run()
        assert report.exit_code == ExitCode.DELETE_GUARD_BLOCKED
        assert report.blocked_deletes == 15
        assert report.roots[0].blocked_deletes == 15
        assert report.roots[0].deleted == 0
    assert env.delete_ids(mark) == []
    assert len(env.onyx_content()) == 21  # the 20 old documents plus the pushed new file
    assert "brand-new.txt" in env.onyx_content()


def test_i5_run_record_keeps_the_blocked_delete_count(env: Env) -> None:
    rels = _seed(env, 20)
    _remove_and_confirm_missing(env, rels[:15])
    env.run()
    state = open_state(env.state_path)
    try:
        record = state.last_run()
    finally:
        state.close()
    assert record is not None
    assert record.exit_code == ExitCode.DELETE_GUARD_BLOCKED
    assert record.blocked_deletes == 15


def test_i5_fraction_decides_above_ten(env: Env) -> None:
    # 11 of 50 is 22 percent: passes with the default 0.25
    rels = _seed(env, 50)
    _remove_and_confirm_missing(env, rels[:11])
    report = env.run()
    assert report.exit_code == ExitCode.OK
    assert report.roots[0].deleted == 11


def test_i5_fraction_above_the_limit_blocks(env: Env) -> None:
    # 11 of 40 is 27.5 percent: blocked with the default 0.25
    rels = _seed(env, 40)
    _remove_and_confirm_missing(env, rels[:11])
    mark = env.mark()
    report = env.run()
    assert report.exit_code == ExitCode.DELETE_GUARD_BLOCKED
    assert env.delete_ids(mark) == []


def test_i5_max_delete_fraction_is_configurable(env: Env) -> None:
    rels = _seed(env, 24, max_delete_fraction=0.5)  # 11 of 24 is 46 percent
    _remove_and_confirm_missing(env, rels[:11])
    config = env.make_config(max_delete_fraction=0.5)
    report = env.run(config=config)
    assert report.exit_code == ExitCode.OK
    assert report.roots[0].deleted == 11


def test_i5_allow_deletes_must_equal_the_pending_count(env: Env) -> None:
    rels = _seed(env, 20)
    _remove_and_confirm_missing(env, rels[:15])
    mark = env.mark()
    for wrong in (0, 10, 14, 16, 20):
        report = env.run(options=SyncOptions(allow_deletes=wrong))
        assert report.exit_code == ExitCode.DELETE_GUARD_BLOCKED, wrong
    assert env.delete_ids(mark) == []
    report = env.run(options=SyncOptions(allow_deletes=15))
    assert report.exit_code == ExitCode.OK
    assert report.roots[0].deleted == 15
    assert len(env.delete_ids(mark)) == 15
    assert len(env.onyx_content()) == 5


def test_i5_one_blocked_root_blocks_the_deletes_of_every_root(env: Env) -> None:
    other = env.tmp_path / "other"
    other_tree = TreeBuilder(other)
    rels_main = env.tree.files(20)
    rels_other = other_tree.files(20, prefix="o")
    config = env.make_config(roots={"main": env.root, "other": other})
    assert env.run(config=config).exit_code == ExitCode.OK
    for rel in rels_main[:15]:  # blocked: 75 percent
        env.tree.remove(rel)
    other_tree.remove(rels_other[0])  # a single delete would pass on its own
    env.run(config=config)
    mark = env.mark()
    report = env.run(config=config)
    assert report.exit_code == ExitCode.DELETE_GUARD_BLOCKED
    assert env.delete_ids(mark) == []
    assert report.roots[0].deleted == 0
    assert report.roots[1].deleted == 0
    assert len(env.fake.documents_of(env.cc_pair_id("other"))) == 20
    # the override counts the pending deletes of the whole run: 15 + 1
    report = env.run(config=config, options=SyncOptions(allow_deletes=15))
    assert report.exit_code == ExitCode.DELETE_GUARD_BLOCKED
    report = env.run(config=config, options=SyncOptions(allow_deletes=16))
    assert report.exit_code == ExitCode.OK
    assert len(env.delete_ids(mark)) == 16


# -- deliberate skips ----------------------------------------------------------------------------


def test_i5_deliberate_skip_deletes_at_once_when_the_guard_allows(env: Env) -> None:
    for i in range(5):
        env.tree.file(f"drop/d{i}.txt", f"drop {i}")
    for i in range(20):
        env.tree.file(f"keep/k{i}.txt", f"keep {i}")
    assert env.run().exit_code == ExitCode.OK
    config = env.make_config(excludes={"main": ("drop/**",)})
    mark = env.mark()
    report = env.run(config=config)  # one walk is enough: the file is proven present
    assert report.exit_code == ExitCode.OK
    assert report.roots[0].deleted == 5
    assert len(env.delete_ids(mark)) == 5
    assert report.roots[0].skipped.get(SkipReason.EXCLUDED, 0) >= 5


def test_i5_config_change_that_excludes_most_files_is_blocked(env: Env) -> None:
    for i in range(15):
        env.tree.file(f"drop/d{i}.txt", f"drop {i}")
    for i in range(5):
        env.tree.file(f"keep/k{i}.txt", f"keep {i}")
    assert env.run().exit_code == ExitCode.OK
    config = env.make_config(excludes={"main": ("drop/**",)})
    mark = env.mark()
    report = env.run(config=config)
    assert report.exit_code == ExitCode.DELETE_GUARD_BLOCKED
    assert report.blocked_deletes == 15
    assert env.delete_ids(mark) == []
    assert len(env.onyx_content()) == 20
    report = env.run(config=config, options=SyncOptions(allow_deletes=15))
    assert report.exit_code == ExitCode.OK
    assert len(env.onyx_content()) == 5


def test_i5_dir_glob_exclusion_never_deletes_over_repeated_runs_when_blocked(env: Env) -> None:
    for i in range(15):
        env.tree.file(f"drop/d{i}.txt", f"drop {i}")
    for i in range(5):
        env.tree.file(f"keep/k{i}.txt", f"keep {i}")
    assert env.run().exit_code == ExitCode.OK
    config = env.make_config(excludes={"main": ("drop/**",)})
    mark = env.mark()
    reports = [env.run(config=config) for _ in range(3)]
    assert env.delete_ids(mark) == []
    assert reports[-1].exit_code == ExitCode.DELETE_GUARD_BLOCKED
    assert len(env.onyx_content()) == 20


def test_i5_file_glob_exclusion_of_most_files_is_blocked_at_once(env: Env) -> None:
    for i in range(15):
        env.tree.file(f"drop/d{i}.txt", f"drop {i}")
    for i in range(5):
        env.tree.file(f"keep/k{i}.txt", f"keep {i}")
    assert env.run().exit_code == ExitCode.OK
    config = env.make_config(excludes={"main": ("drop/d*.txt",)})
    mark = env.mark()
    report = env.run(config=config)
    assert report.exit_code == ExitCode.DELETE_GUARD_BLOCKED
    assert report.blocked_deletes == 15
    assert env.delete_ids(mark) == []


def test_i5_file_glob_exclusion_within_the_allowance_deletes_at_once(env: Env) -> None:
    for i in range(5):
        env.tree.file(f"drop/d{i}.txt", f"drop {i}")
    for i in range(20):
        env.tree.file(f"keep/k{i}.txt", f"keep {i}")
    assert env.run().exit_code == ExitCode.OK
    config = env.make_config(excludes={"main": ("drop/d*.txt",)})
    mark = env.mark()
    report = env.run(config=config)
    assert report.exit_code == ExitCode.OK
    assert len(env.delete_ids(mark)) == 5


def test_i5_files_that_become_too_large_are_deleted_under_the_guard(env: Env) -> None:
    rels = _seed(env, 20)
    for rel in rels[:15]:
        env.tree.touch_changed(rel, "x" * 3000)
    config = env.make_config(max_file_mb=0.001)  # about 1 KB
    mark = env.mark()
    report = env.run(config=config)
    assert report.exit_code == ExitCode.DELETE_GUARD_BLOCKED
    assert env.delete_ids(mark) == []
    for rel in rels[15:]:
        assert rel in env.onyx_content()

    # five oversized files stay under the free allowance of ten
    remaining_oversized = rels[:5]
    for rel in rels[5:15]:
        env.tree.touch_changed(rel, "small again")
    report = env.run(config=config)
    assert report.exit_code == ExitCode.OK
    assert report.roots[0].deleted == len(remaining_oversized)


def test_i5_files_that_extract_to_no_text_are_deleted_under_the_guard(env: Env) -> None:
    rels = _seed(env, 20)
    for rel in rels[:15]:
        env.tree.touch_changed(rel, "")
    mark = env.mark()
    report = env.run()
    assert report.exit_code == ExitCode.DELETE_GUARD_BLOCKED
    assert env.delete_ids(mark) == []
    assert len(env.onyx_content()) == 20


def test_i5_no_text_files_within_the_free_allowance_are_deleted(env: Env) -> None:
    rels = _seed(env, 20)
    for rel in rels[:6]:
        env.tree.touch_changed(rel, "")
    mark = env.mark()
    report = env.run()
    assert report.exit_code == ExitCode.OK
    assert len(env.delete_ids(mark)) == 6
    assert len(env.onyx_content()) == 14


def test_i5_text_too_large_is_a_deliberate_skip_under_the_guard(env: Env) -> None:
    rels = _seed(env, 20)
    for rel in rels[:15]:
        env.tree.touch_changed(rel, "y" * 500)
    config = env.make_config(max_text_chars=100)
    mark = env.mark()
    report = env.run(config=config)
    assert report.exit_code == ExitCode.DELETE_GUARD_BLOCKED
    assert env.delete_ids(mark) == []
    assert len(env.onyx_content()) == 20


# -- reconcile -----------------------------------------------------------------------------------


def test_i5_reconcile_deletes_are_guarded(env: Env) -> None:
    env.tree.files(5)
    env.run()
    cc = env.cc_pair_id()
    strays = [env.doc_id(f"stray{i}.txt") for i in range(15)]
    for i, stray in enumerate(strays):
        env.put_document(stray, f"stray {i}", cc, f"stray{i}.txt")
    mark = env.mark()
    report = env.reconcile()
    assert report.exit_code == ExitCode.DELETE_GUARD_BLOCKED
    assert env.delete_ids(mark) == []
    report = env.reconcile(allow_deletes=15)
    assert report.exit_code == ExitCode.OK
    assert sorted(env.delete_ids(mark)) == sorted(strays)
    assert len(env.onyx_content()) == 5


def test_i5_reconcile_within_the_free_allowance_deletes(env: Env) -> None:
    env.tree.files(30)
    env.run()
    cc = env.cc_pair_id()
    strays = [env.doc_id(f"stray{i}.txt") for i in range(11)]
    for i, stray in enumerate(strays):
        env.put_document(stray, f"stray {i}", cc, f"stray{i}.txt")
    config = env.make_config(max_delete_fraction=0.5)
    report = env.reconcile(config=config)  # 11 of 30 (or 41): under the 50 percent fraction
    assert report.exit_code == ExitCode.OK
    assert sorted(env.delete_ids()) == sorted(strays)
