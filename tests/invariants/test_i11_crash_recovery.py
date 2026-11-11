"""I11: a crash at any point leaves state such that the next run converges without wrong deletes."""

from __future__ import annotations

from typing import Any

import pytest

from tests.invariants.harness import Crash, Env, SimulatedCrash, crashing_scandir

MAX_CONVERGE_RUNS = 6


def _has_onyx_id(*args: Any, **kwargs: Any) -> bool:
    record = args[0] if args else kwargs["record"]
    return bool(record.onyx_id is not None)


def _prepare_changes(env: Env) -> None:
    """Disk and Onyx differ: one changed file, one new file, two removed files."""
    env.tree.touch_changed("d0/f0.txt", "f0 changed")
    env.tree.file("d1/f6.txt", "f6 new")
    env.tree.remove("d1/f4.txt")
    env.tree.remove("d1/f5.txt")


def _seed(env: Env) -> None:
    for i in range(6):
        env.tree.file(f"d{i // 3}/f{i}.txt", f"f{i} original")
    assert env.run().exit_code == 0


def _converge(env: Env) -> None:
    for _ in range(MAX_CONVERGE_RUNS):
        env.run()
        if env.onyx_content() == env.disk_content():
            break
    assert env.onyx_content() == env.disk_content()
    env.assert_no_wrong_deletes()


# (label, target, crash, run index in which the crash happens). Runs are counted from the
# first run after _prepare_changes: the removed files reach missing_count 2, and are
# deleted, in run 2.
CASES: list[tuple[str, str, Crash, int]] = [
    (
        "push-then-crash-before-state-write",
        "state",
        Crash("upsert_document", 1, when=_has_onyx_id),
        1,
    ),
    (
        "push-then-crash-before-state-write-2nd",
        "state",
        Crash("upsert_document", 2, when=_has_onyx_id),
        1,
    ),
    ("crash-right-after-push-request-returned", "onyx", Crash("upsert_document", 2, after=True), 1),
    ("crash-before-push-request", "onyx", Crash("upsert_document", 2), 1),
    (
        "crash-after-delete-request-before-state-write",
        "onyx",
        Crash("delete_document", 1, after=True),
        2,
    ),
    ("crash-after-second-delete-request", "onyx", Crash("delete_document", 2, after=True), 2),
    ("crash-before-delete-request", "onyx", Crash("delete_document", 1), 2),
    (
        "crash-between-bump-missing-and-deletes-run-1",
        "state",
        Crash("bump_missing", 1, after=True),
        1,
    ),
    (
        "crash-between-bump-missing-and-deletes-run-2",
        "state",
        Crash("bump_missing", 1, after=True),
        2,
    ),
    ("crash-before-bump-missing-run-2", "state", Crash("bump_missing", 1), 2),
]


@pytest.mark.parametrize(
    ("label", "target", "crash", "crash_run"), CASES, ids=[c[0] for c in CASES]
)
def test_i11_crash_point_converges_without_wrong_deletes(
    env: Env, label: str, target: str, crash: Crash, crash_run: int
) -> None:
    _seed(env)
    _prepare_changes(env)
    for _ in range(1, crash_run):
        env.run()
    kwargs: dict[str, Any] = (
        {"wrap_state": crash.wrap} if target == "state" else {"wrap_onyx": crash.wrap}
    )
    with pytest.raises(SimulatedCrash):
        env.run(**kwargs)
    assert crash.fired
    # the file system never changed in a way that justifies deleting an existing file
    env.assert_no_wrong_deletes()
    _converge(env)


@pytest.mark.parametrize("nth", [1, 2, 3])
def test_i11_crash_mid_walk_during_the_first_run_converges(env: Env, nth: int) -> None:
    for i in range(6):
        env.tree.file(f"d{i // 3}/f{i}.txt", f"f{i} original")
    scandir, counter = crashing_scandir(nth)
    with pytest.raises(SimulatedCrash):
        env.run(scandir=scandir)
    assert counter[0] == nth
    _converge(env)


@pytest.mark.parametrize("nth", [1, 2, 3])
def test_i11_crash_mid_walk_while_files_are_missing_converges(env: Env, nth: int) -> None:
    _seed(env)
    _prepare_changes(env)
    env.run()  # the removed files are missing once
    scandir, _ = crashing_scandir(nth)
    with pytest.raises(SimulatedCrash):
        env.run(scandir=scandir)
    env.assert_no_wrong_deletes()
    _converge(env)


def test_i11_crash_after_the_entry_was_created_but_before_state_saved_it(env: Env) -> None:
    for i in range(4):
        env.tree.file(f"d0/f{i}.txt", f"f{i} original")
    crash = Crash("create_entry", 1, after=True)
    with pytest.raises(SimulatedCrash):
        env.run(wrap_onyx=crash.wrap)
    assert crash.fired
    _converge(env)


def test_i11_repeated_crashes_at_every_state_write_still_converge(env: Env) -> None:
    """Crash at the nth state write for growing n: each partial run is only a prefix."""
    _seed(env)
    _prepare_changes(env)
    survivors = 0
    for nth in range(1, 8):
        crash = Crash("upsert_document", nth)
        try:
            env.run(wrap_state=crash.wrap)
        except SimulatedCrash:
            survivors += 1
        env.assert_no_wrong_deletes()
    assert survivors >= 1
    _converge(env)
