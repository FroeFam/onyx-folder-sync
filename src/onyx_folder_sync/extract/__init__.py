"""File text extraction (work package 4)."""

from onyx_folder_sync.extract.extractors import build_registry, version_for
from onyx_folder_sync.extract.report import build_extract_report
from onyx_folder_sync.extract.runner import create_runner

__all__ = ["build_extract_report", "build_registry", "create_runner", "version_for"]
