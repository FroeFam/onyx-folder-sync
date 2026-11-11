from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from onyx_folder_sync.interfaces import LockHeldError
from onyx_folder_sync.lock import acquire_lock

_CHILD = """
import os
import sys
from pathlib import Path
from onyx_folder_sync.lock import acquire_lock

with acquire_lock(Path(sys.argv[1])):
    print("locked", os.getpid(), flush=True)
    sys.stdin.readline()
"""


def _start_child(state_path: Path) -> tuple[subprocess.Popen[str], int]:
    """Returns the Popen and the pid of the interpreter holding the lock.

    On Windows a venv python.exe is a launcher that spawns the real interpreter,
    so Popen.pid is not the lock holder; the child reports its own pid.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(state_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    word, pid = proc.stdout.readline().split()
    assert word == "locked"
    return proc, int(pid)


def test_second_process_gets_lock_held_then_can_acquire(tmp_path: Path) -> None:
    state_path = tmp_path / "nested" / "state.db"
    child, _ = _start_child(state_path)
    try:
        with pytest.raises(LockHeldError), acquire_lock(state_path):
            pytest.fail("lock must not be granted while the child holds it")
    finally:
        assert child.stdin is not None
        child.stdin.write("\n")
        child.stdin.flush()
        child.wait(timeout=30)
        child.stdin.close()
        assert child.stdout is not None
        child.stdout.close()
    with acquire_lock(state_path):
        pass


def test_lock_is_released_on_exit_and_on_error(tmp_path: Path) -> None:
    state_path = tmp_path / "state.db"
    with acquire_lock(state_path), pytest.raises(LockHeldError), acquire_lock(state_path):
        pass
    with pytest.raises(RuntimeError), acquire_lock(state_path):
        raise RuntimeError("boom")
    with acquire_lock(state_path):
        pass


def test_killed_holder_leaves_lock_acquirable(tmp_path: Path) -> None:
    state_path = tmp_path / "state.db"
    child, holder_pid = _start_child(state_path)
    with pytest.raises(LockHeldError), acquire_lock(state_path):
        pass
    os.kill(holder_pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    child.wait(timeout=30)
    assert child.stdin is not None
    child.stdin.close()
    assert child.stdout is not None
    child.stdout.close()
    with acquire_lock(state_path):
        pass
