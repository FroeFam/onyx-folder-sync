"""Extraction in a persistent spawned worker process with a timeout and a memory limit."""

from __future__ import annotations

import importlib
import multiprocessing
import os
import sys
import time
import warnings
from multiprocessing.context import SpawnContext
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any

from onyx_folder_sync.extract.extractors import build_registry, version_for
from onyx_folder_sync.models import ExtractResult

_READY_TIMEOUT_SECONDS = 60.0
_MAX_ERROR_CHARS = 200


def _one_line(exc: BaseException) -> str:
    text = " ".join(f"{type(exc).__name__}: {exc}".split())
    return text[:_MAX_ERROR_CHARS]


def _limit_memory(memory_mb: int) -> None:
    try:
        resource: Any = importlib.import_module("resource")
    except ModuleNotFoundError:  # Windows: only the timeout applies.
        return
    limit = memory_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def _silence_streams() -> None:
    devnull = os.open(os.devnull, os.O_WRONLY)
    for fd in (1, 2):
        os.dup2(devnull, fd)
    sys.stdout = sys.stderr = open(os.devnull, "w")  # noqa: SIM115
    warnings.simplefilter("ignore")


def _worker_main(conn: Any, memory_mb: int) -> None:
    """Child process loop: receive (path, extension), answer (status, payload)."""
    _silence_streams()
    try:
        _limit_memory(memory_mb)
        registry = build_registry()
        conn.send(("ready", ""))
    except Exception as exc:
        conn.send(("error", _one_line(exc)))
        return
    while True:
        try:
            request = conn.recv()
        except EOFError:
            return
        if request is None:
            return
        path, extension = request
        try:
            extractor = registry.get(extension)
            if extractor is None:
                raise ValueError(f"no extractor for extension {extension!r}")
            reply = ("ok", extractor.extract(Path(path)).strip())
        except Exception as exc:  # MemoryError is an Exception too.
            reply = ("error", _one_line(exc))
        try:
            conn.send(reply)
        except Exception as exc:
            conn.send(("error", _one_line(exc)))


class ProcessExtractionRunner:
    def __init__(self, *, timeout_seconds: float, memory_mb: int) -> None:
        self._timeout = timeout_seconds
        self._memory_mb = memory_mb
        self._ctx: SpawnContext = multiprocessing.get_context("spawn")
        self._process: BaseProcess | None = None
        self._conn: Any = None

    def version_for(self, extension: str) -> str | None:
        return version_for(extension)

    def extract(self, path: Path, extension: str) -> ExtractResult:
        try:
            return self._extract(path, extension.lower().lstrip("."))
        except Exception as exc:
            self._kill()
            return ExtractResult(status="error", error=_one_line(exc))

    def close(self) -> None:
        process, conn = self._process, self._conn
        if process is not None and conn is not None and process.is_alive():
            try:
                conn.send(None)
                process.join(2)
            except (OSError, ValueError):
                pass
        self._kill()

    def _extract(self, path: Path, extension: str) -> ExtractResult:
        error = self._ensure_worker()
        if error is not None:
            return ExtractResult(status="error", error=error)
        conn = self._conn
        assert conn is not None  # noqa: S101
        conn.send((str(path), extension))
        if not conn.poll(self._timeout):
            self._kill()
            return ExtractResult(status="timeout", error=f"timed out after {self._timeout:g}s")
        try:
            status, payload = conn.recv()
        except (EOFError, OSError):
            return ExtractResult(status="error", error=self._crash_message())
        if status == "ok":
            return ExtractResult(status="ok", text=payload)
        return ExtractResult(status="error", error=str(payload)[:_MAX_ERROR_CHARS])

    def _ensure_worker(self) -> str | None:
        """Start a worker when none is alive. Returns an error message on failure."""
        if self._process is not None and self._process.is_alive():
            return None
        self._kill()
        parent_conn, child_conn = self._ctx.Pipe(duplex=True)
        process = self._ctx.Process(
            target=_worker_main, args=(child_conn, self._memory_mb), daemon=True
        )
        process.start()
        child_conn.close()
        self._process, self._conn = process, parent_conn
        deadline = time.monotonic() + max(_READY_TIMEOUT_SECONDS, self._timeout)
        try:
            if not parent_conn.poll(max(0.0, deadline - time.monotonic())):
                self._kill()
                return "worker did not start in time"
            status, payload = parent_conn.recv()
        except (EOFError, OSError):
            return self._crash_message()
        if status != "ready":
            self._kill()
            return f"worker failed to start: {payload}"[:_MAX_ERROR_CHARS]
        return None

    def _crash_message(self) -> str:
        process = self._process
        code = None
        if process is not None:
            process.join(2)
            code = process.exitcode
        self._kill()
        return f"worker process died (exit code {code})"

    def _kill(self) -> None:
        process, conn = self._process, self._conn
        self._process = self._conn = None
        if conn is not None:
            conn.close()
        if process is not None:
            if process.is_alive():
                process.kill()
            process.join(5)
            process.close()


def create_runner(*, timeout_seconds: float, memory_mb: int) -> ProcessExtractionRunner:
    return ProcessExtractionRunner(timeout_seconds=timeout_seconds, memory_mb=memory_mb)
