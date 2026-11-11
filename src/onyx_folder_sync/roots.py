"""Root management: forget and rename-root (plan section 5.1)."""

from onyx_folder_sync.engine import ENTRY_SOURCE
from onyx_folder_sync.interfaces import StateError, SyncDeps


def forget_root(deps: SyncDeps, root_name: str, *, state_only: bool = False) -> None:
    """Remove a root's Onyx entry with its documents, then its state.

    The entry is removed only when it is this installation's own
    ``ingestion_api`` entry. ``state_only`` skips Onyx altogether.
    """
    record = deps.state.get_root(root_name)
    if record is None:
        raise StateError(f"root '{root_name}' is not in state")
    if not state_only and record.cc_pair_id is not None:
        info = deps.onyx.get_entry(record.cc_pair_id)
        if info is not None:
            if info.source != ENTRY_SOURCE or not info.name.endswith(
                f"[{deps.state.instance_id()}]"
            ):
                raise StateError(
                    f"Onyx entry {record.cc_pair_id} was not created by this installation; "
                    "it is left alone. Use 'forget --state-only' to drop the local state."
                )
            deps.onyx.remove_entry(record.cc_pair_id, info.connector_id, info.credential_id)
    deps.state.remove_root(record.root_id)


def rename_root(deps: SyncDeps, old: str, new: str) -> None:
    """Rename a root in state. Document ids and the Onyx entry do not change."""
    deps.state.rename_root(old, new)
