"""Keeps local or mounted folders in sync with an Onyx knowledge base."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("onyx-folder-sync")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0.0.0"

__all__ = ["__version__"]
