"""Logic of the extract-report command."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from onyx_folder_sync.interfaces import ExtractionRunner
from onyx_folder_sync.models import ExtractReport, ExtractTypeCounts, FileEntry


def build_extract_report(
    files: Iterable[FileEntry], runner: ExtractionRunner, *, max_text_chars: int
) -> ExtractReport:
    """Run the runner over files and count outcomes per extension.

    The categories are disjoint: ``ok`` is non-empty text within the limit,
    ``no_text`` is an empty result, ``too_large`` is text over ``max_text_chars``
    and ``failed`` is an error or timeout. ``total_text_chars`` sums the ``ok``
    texts.
    """
    counts: dict[str, ExtractTypeCounts] = {}
    total = 0
    for entry in files:
        ext = entry.extension
        current = counts.get(ext, ExtractTypeCounts())
        result = runner.extract(Path(entry.abs_path), ext)
        if result.status != "ok":
            current = replace(current, failed=current.failed + 1)
        elif not result.text:
            current = replace(current, no_text=current.no_text + 1)
        elif len(result.text) > max_text_chars:
            current = replace(current, too_large=current.too_large + 1)
        else:
            total += len(result.text)
            current = replace(current, ok=current.ok + 1)
        counts[ext] = current
    return ExtractReport(by_extension=counts, total_text_chars=total)
