from __future__ import annotations

import multiprocessing
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from onyx_folder_sync.extract import build_extract_report, create_runner
from onyx_folder_sync.extract.extractors import TEST_HOOK_ENV
from onyx_folder_sync.interfaces import ExtractionRunner
from onyx_folder_sync.models import DEFAULT_EXTENSIONS, FileEntry

SAMPLES = Path(__file__).parent.parent / "samples"


@pytest.fixture
def hook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TEST_HOOK_ENV, "1")


@pytest.fixture
def runner() -> Iterator[ExtractionRunner]:
    created = create_runner(timeout_seconds=60, memory_mb=1024)
    yield created
    created.close()


def test_runner_extracts_every_sample(runner: ExtractionRunner) -> None:
    for ext in DEFAULT_EXTENSIONS:
        result = runner.extract(SAMPLES / f"sample.{ext}", ext)
        assert result.status == "ok", (ext, result.error)
        assert f"quokka marmalade {ext}" in result.text


def test_image_only_pdf_is_ok_with_empty_text(runner: ExtractionRunner) -> None:
    result = runner.extract(SAMPLES / "empty.pdf", "pdf")
    assert result.status == "ok"
    assert result.text == ""


@pytest.mark.parametrize("ext", ["pdf", "docx", "pptx", "xlsx", "epub"])
def test_corrupt_file_is_error_and_worker_survives(runner: ExtractionRunner, ext: str) -> None:
    result = runner.extract(SAMPLES / f"corrupt.{ext}", ext)
    assert result.status == "error"
    assert result.error
    assert "\n" not in result.error
    assert len(result.error) <= 200
    assert runner.extract(SAMPLES / "sample.txt", "txt").status == "ok"


def test_missing_file_and_unknown_extension_are_errors(
    runner: ExtractionRunner, tmp_path: Path
) -> None:
    assert runner.extract(tmp_path / "nope.txt", "txt").status == "error"
    assert runner.extract(SAMPLES / "sample.txt", "exe").status == "error"


def test_timeout_then_next_call_works(hook: None, tmp_path: Path) -> None:
    path = tmp_path / "slow.xsleep"
    path.write_text("x", encoding="ascii")
    runner = create_runner(timeout_seconds=1.5, memory_mb=1024)
    try:
        assert runner.extract(SAMPLES / "sample.txt", "txt").status == "ok"  # warm up
        start = time.monotonic()
        result = runner.extract(path, "xsleep")
        elapsed = time.monotonic() - start
        assert result.status == "timeout"
        assert 1.4 <= elapsed < 10
        again = runner.extract(SAMPLES / "sample.txt", "txt")
        assert again.status == "ok"
    finally:
        runner.close()


def test_worker_crash_recovery(hook: None, tmp_path: Path) -> None:
    path = tmp_path / "boom.xcrash"
    path.write_text("x", encoding="ascii")
    runner = create_runner(timeout_seconds=30, memory_mb=1024)
    try:
        result = runner.extract(path, "xcrash")
        assert result.status == "error"
        assert result.error
        assert runner.extract(SAMPLES / "sample.txt", "txt").status == "ok"
    finally:
        runner.close()


def test_close_leaves_no_child_process() -> None:
    runner = create_runner(timeout_seconds=30, memory_mb=1024)
    assert runner.extract(SAMPLES / "sample.txt", "txt").status == "ok"
    assert multiprocessing.active_children()
    runner.close()
    assert multiprocessing.active_children() == []
    runner.close()  # idempotent
    assert runner.extract(SAMPLES / "sample.txt", "txt").status == "ok"  # restarts
    runner.close()


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS is enforced on Linux only")
def test_memory_limit_stops_allocation(hook: None, tmp_path: Path) -> None:
    path = tmp_path / "big.xalloc"
    path.write_text("1024", encoding="ascii")  # twice the 512 MB limit
    runner = create_runner(timeout_seconds=60, memory_mb=512)
    try:
        result = runner.extract(path, "xalloc")
        assert result.status == "error"
        assert result.error is not None
        assert "MemoryError" in result.error
    finally:
        runner.close()


def test_extract_report_with_real_runner(runner: ExtractionRunner) -> None:
    files = [
        FileEntry(rel_path=name, abs_path=str(SAMPLES / name), size=1, mtime_ns=1)
        for name in ("sample.pdf", "empty.pdf", "corrupt.pdf", "sample.txt")
    ]
    report = build_extract_report(files, runner, max_text_chars=1000)
    pdf = report.by_extension["pdf"]
    assert (pdf.ok, pdf.no_text, pdf.failed) == (1, 1, 1)
    assert report.by_extension["txt"].ok == 1
    assert report.total_text_chars > 0
