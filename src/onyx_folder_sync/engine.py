"""Sync engine. Stub with the final signature; implemented in work package 6."""

from onyx_folder_sync.interfaces import SyncDeps
from onyx_folder_sync.models import Config, SyncOptions, SyncReport


def run_sync(config: Config, deps: SyncDeps, options: SyncOptions) -> SyncReport:
    raise NotImplementedError
