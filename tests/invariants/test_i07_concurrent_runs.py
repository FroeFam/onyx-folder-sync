"""I7: a second concurrent run performs no writes to Onyx or state."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from onyx_folder_sync.lock import acquire_lock
from onyx_folder_sync.models import ExitCode
from onyx_folder_sync.state import open_state
from tests.fixtures import write_config
from tests.invariants.harness import Env, state_run_count

SYNC_TIMEOUT = 120


def _cli(env: Env, config: Path, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "onyx_folder_sync", "--config", str(config), *args],
        env={**os.environ, "ONYX_TOKEN": env.fake.token},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _config(env: Env) -> Path:
    return write_config(
        env.tmp_path / "config.yaml",
        onyx_url=env.fake.url,
        state_path=env.state_path,
        roots={"main": env.root},
    )


def test_i7_run_against_a_held_lock_exits_5_without_any_write(env: Env) -> None:
    env.tree.files(5)
    assert env.run().exit_code == ExitCode.OK
    env.tree.touch_changed("doc0000.txt", "changed while locked")
    env.tree.remove("doc0001.txt")
    runs_before = state_run_count(env.state_path)
    docs_before = env.documents()
    requests_before = env.mark()
    config = _config(env)
    with acquire_lock(env.state_path):
        proc = _cli(env, config, "sync")
        out, err = proc.communicate(timeout=SYNC_TIMEOUT)
    assert proc.returncode == ExitCode.LOCK_HELD, (out, err)
    assert env.writes(requests_before) == []
    assert state_run_count(env.state_path) == runs_before
    assert env.documents() == docs_before
    # once the lock is free the run works
    proc = _cli(env, config, "sync")
    proc.communicate(timeout=SYNC_TIMEOUT)
    assert proc.returncode == ExitCode.OK


def test_i7_held_lock_blocks_every_write_command(env: Env) -> None:
    env.tree.files(3)
    assert env.run().exit_code == ExitCode.OK
    runs_before = state_run_count(env.state_path)
    requests_before = env.mark()
    config = _config(env)
    with acquire_lock(env.state_path):
        for command in (["sync"], ["sync", "--repush"], ["reconcile", "main"]):
            proc = _cli(env, config, *command)
            proc.communicate(timeout=SYNC_TIMEOUT)
            assert proc.returncode == ExitCode.LOCK_HELD, command
    assert env.writes(requests_before) == []
    assert state_run_count(env.state_path) == runs_before


def test_i7_second_run_started_while_the_first_is_busy_writes_nothing(env: Env) -> None:
    env.tree.file("only.txt", "only file")
    config = _config(env)
    first_push_seen = threading.Event()

    def on_request(method: str, path: str, body: Any) -> None:
        if method == "POST" and path.endswith("/onyx-api/ingestion"):
            first_push_seen.set()

    env.fake.on_request.append(on_request)
    env.fake.delay_next("POST", "/onyx-api/ingestion", 5)
    first = _cli(env, config, "sync")
    try:
        assert first_push_seen.wait(60), "the first run never reached its push"
        writes_before = env.writes()
        second = _cli(env, config, "sync")
        second.communicate(timeout=SYNC_TIMEOUT)
        assert second.returncode == ExitCode.LOCK_HELD
        assert env.writes() == writes_before
    finally:
        out, err = first.communicate(timeout=SYNC_TIMEOUT)
    assert first.returncode == ExitCode.OK, (out, err)
    assert state_run_count(env.state_path) == 1
    assert env.onyx_content() == env.disk_content()
    state = open_state(env.state_path)
    try:
        run = state.last_run()
    finally:
        state.close()
    assert run is not None
    assert run.exit_code == ExitCode.OK
