"""Root management. Stub with the final signatures; implemented in work package 7."""

from onyx_folder_sync.interfaces import SyncDeps


def forget_root(deps: SyncDeps, root_name: str) -> None:
    raise NotImplementedError


def rename_root(deps: SyncDeps, old: str, new: str) -> None:
    raise NotImplementedError
