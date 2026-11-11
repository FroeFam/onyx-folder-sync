from __future__ import annotations

from pathlib import Path

import pytest

from onyx_folder_sync.extract import build_extract_report, build_registry, version_for
from onyx_folder_sync.extract.extractors import decode_text
from onyx_folder_sync.models import (
    DEFAULT_EXTENSIONS,
    ExtractResult,
    ExtractTypeCounts,
    FileEntry,
)

SAMPLES = Path(__file__).parent.parent / "samples"


@pytest.mark.parametrize("ext", DEFAULT_EXTENSIONS)
def test_extractor_reads_sample(ext: str) -> None:
    extractor = build_registry()[ext]
    text = extractor.extract(SAMPLES / f"sample.{ext}")
    assert f"quokka marmalade {ext}" in text
    assert text == text.strip()


def test_eml_has_headers_and_body() -> None:
    text = build_registry()["eml"].extract(SAMPLES / "sample.eml")
    assert text.startswith("Subject: Hello there\nFrom: Alice")
    assert "To: Bob" in text
    assert "Date: Thu, 01 Jan 2026" in text


def test_eml_falls_back_to_stripped_html(tmp_path: Path) -> None:
    path = tmp_path / "h.eml"
    path.write_bytes(
        b"Subject: S\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        b"<html><style>p{}</style><body><p>hello <b>world</b></p></body></html>"
    )
    text = build_registry()["eml"].extract(path)
    assert "hello world" in text
    assert "<" not in text
    assert "p{}" not in text


def test_image_only_pdf_returns_empty_text() -> None:
    assert build_registry()["pdf"].extract(SAMPLES / "empty.pdf") == ""


@pytest.mark.parametrize("ext", ["pdf", "docx", "pptx", "xlsx", "epub"])
def test_corrupt_file_raises(ext: str) -> None:
    with pytest.raises(Exception):  # noqa: B017
        build_registry()[ext].extract(SAMPLES / f"corrupt.{ext}")


SENTENCE = "Café crème brûlée et crêpes à la française, s'il vous plaît."


@pytest.mark.parametrize(
    ("encoding", "prefix"),
    [
        ("utf-8", b""),
        ("utf-16", b""),  # Python writes a BOM
        ("cp1252", b""),
    ],
)
def test_encoding_detection(encoding: str, prefix: bytes) -> None:
    text = (SENTENCE + " ") * 5
    assert decode_text(prefix + text.encode(encoding)) == text


def test_text_extractor_decodes_utf16_with_bom(tmp_path: Path) -> None:
    path = tmp_path / "u16.txt"
    path.write_bytes("﻿hello é\n".encode("utf-16-le"))
    assert build_registry()["txt"].extract(path) == "hello é"


def test_registry_covers_default_extensions() -> None:
    assert set(DEFAULT_EXTENSIONS) <= set(build_registry())
    for ext, extractor in build_registry().items():
        assert ext in extractor.extensions


def test_versions() -> None:
    assert version_for("pdf") == "pdf-1"
    assert version_for(".PDF") == "pdf-1"
    assert version_for("docx") == "markitdown-1"
    assert version_for("txt") == "text-1"
    assert version_for("eml") == "eml-1"
    assert version_for("exe") is None
    assert version_for("xsleep") is None  # test hook is off by default


class FakeRunner:
    def __init__(self, results: dict[str, ExtractResult]) -> None:
        self.results = results

    def version_for(self, extension: str) -> str | None:
        return "fake-1"

    def extract(self, path: Path, extension: str) -> ExtractResult:
        return self.results[path.name]

    def close(self) -> None:
        pass


def _entry(name: str) -> FileEntry:
    return FileEntry(rel_path=name, abs_path=f"/x/{name}", size=1, mtime_ns=1)


def test_build_extract_report_counts() -> None:
    runner = FakeRunner(
        {
            "a.pdf": ExtractResult(status="ok", text="abcd"),
            "b.pdf": ExtractResult(status="ok", text=""),
            "c.pdf": ExtractResult(status="error", error="boom"),
            "d.pdf": ExtractResult(status="timeout"),
            "e.pdf": ExtractResult(status="ok", text="x" * 11),
            "f.txt": ExtractResult(status="ok", text="hello"),
        }
    )
    files = [_entry(n) for n in runner.results]
    report = build_extract_report(files, runner, max_text_chars=10)
    assert report.by_extension["pdf"] == ExtractTypeCounts(ok=1, no_text=1, failed=2, too_large=1)
    assert report.by_extension["txt"] == ExtractTypeCounts(ok=1)
    assert report.total_text_chars == 9


def test_build_extract_report_empty() -> None:
    report = build_extract_report([], FakeRunner({}), max_text_chars=10)
    assert report.by_extension == {}
    assert report.total_text_chars == 0
