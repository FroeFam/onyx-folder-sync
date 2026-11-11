"""Exclusive, non-blocking OS lock next to the state file (invariant I7)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import filelock

from onyx_folder_sync.interfaces import LockHeldError


@contextmanager
def acquire_lock(state_path: Path) -> Iterator[None]:
    """Hold "<state_path>.lock" for the duration of the with block.

    Raises LockHeldError at once when another process holds it. The OS drops the
    lock when the holder dies, so a killed run never leaves a stale lock.
    """
    lock_path = Path(str(state_path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = filelock.FileLock(str(lock_path), timeout=0)
    try:
        lock.acquire()
    except filelock.Timeout:
        raise LockHeldError(f"another run holds the lock {lock_path}") from None
    try:
        yield
    finally:
        lock.release()
