"""Reconcile. Stub with the final signature; implemented in work package 7."""

from onyx_folder_sync.interfaces import SyncDeps
from onyx_folder_sync.models import Config, SyncReport


def reconcile_root(
    config: Config,
    deps: SyncDeps,
    root_name: str,
    *,
    delete_excluded: bool = False,
    allow_deletes: int | None = None,
) -> SyncReport:
    raise NotImplementedError
