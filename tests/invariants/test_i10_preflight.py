"""I10: preflight refuses entries with pruning, a foreign source or a foreign instance id."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from onyx_folder_sync.models import ExitCode
from tests.fixtures import TreeBuilder
from tests.invariants.harness import Env


def _seed(env: Env) -> int:
    env.tree.files(4)
    assert env.run().exit_code == ExitCode.OK
    return env.cc_pair_id()


def _set_prune(env: Env, cc: int) -> None:
    env.fake.set_prune_freq(cc, 604800)


def _set_source(env: Env, cc: int) -> None:
    env.fake.entries[cc].source = "web"


def _set_foreign_name(env: Env, cc: int) -> None:
    env.fake.entries[cc].name = "folder: main [0123456789abcdef]"


def _set_unsuffixed_name(env: Env, cc: int) -> None:
    env.fake.entries[cc].name = "folder: main"


def _drop(env: Env, cc: int) -> None:
    env.fake.drop_entry(cc)


BREAKERS: list[tuple[str, Callable[[Env, int], None]]] = [
    ("prune_freq", _set_prune),
    ("source", _set_source),
    ("foreign-instance-name", _set_foreign_name),
    ("unsuffixed-name", _set_unsuffixed_name),
    ("entry-deleted", _drop),
]


@pytest.mark.parametrize(("label", "breaker"), BREAKERS, ids=[b[0] for b in BREAKERS])
def test_i10_preflight_refuses_a_bad_entry_with_exit_2_and_no_writes(
    env: Env, label: str, breaker: Callable[[Env, int], None]
) -> None:
    cc = _seed(env)
    env.tree.file("new.txt", "new")
    env.tree.remove("doc0000.txt")
    env.run()
    breaker(env, cc)
    mark = env.mark()
    for _ in range(3):
        report = env.run()
        assert report.exit_code == ExitCode.CONFIG_OR_STATE, label
        assert report.messages
    assert env.writes(mark) == []
    assert env.delete_ids(mark) == []


@pytest.mark.parametrize(("label", "breaker"), BREAKERS, ids=[b[0] for b in BREAKERS])
def test_i10_one_bad_entry_stops_the_run_for_every_root(
    env: Env, label: str, breaker: Callable[[Env, int], None]
) -> None:
    other = env.tmp_path / "other"
    other_tree = TreeBuilder(other)
    env.tree.files(3)
    other_tree.files(3, prefix="o")
    config = env.make_config(roots={"main": env.root, "other": other})
    assert env.run(config=config).exit_code == ExitCode.OK
    other_tree.file("fresh.txt", "fresh")
    env.tree.file("fresh-main.txt", "fresh")
    breaker(env, env.cc_pair_id("main"))
    mark = env.mark()
    report = env.run(config=config)
    assert report.exit_code == ExitCode.CONFIG_OR_STATE, label
    assert env.writes(mark) == []


def test_i10_the_entry_the_tool_creates_passes_its_own_preflight(env: Env) -> None:
    cc = _seed(env)
    entry = env.fake.entries[cc]
    assert entry.prune_freq is None
    assert entry.source == "ingestion_api"
    assert entry.name.endswith(f"[{env.instance_id()}]")
    for _ in range(2):
        assert env.run().exit_code == ExitCode.OK
    assert len(env.fake.entries) == 1


def test_i10_pruning_switched_on_after_a_run_is_refused_before_documents_are_pruned(
    env: Env,
) -> None:
    cc = _seed(env)
    env.fake.set_prune_freq(cc, 86400)
    for _ in range(2):
        report = env.run()
        assert report.exit_code == ExitCode.CONFIG_OR_STATE
    assert env.fake.documents_of(cc)  # nothing pushed or deleted by the refused runs
    env.fake.set_prune_freq(cc, None)
    assert env.run().exit_code == ExitCode.OK
