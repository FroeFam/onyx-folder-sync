"""Delete guard (section 5.6)."""

from onyx_folder_sync.models import FREE_DELETES_PER_ROOT


def evaluate_guard(pending: int, total_documents: int, *, max_delete_fraction: float) -> bool:
    """True when the pending deletes of one root may proceed.

    ``total_documents`` is the number of documents the root has in Onyx
    according to state, before any delete of this run.
    """
    if pending <= FREE_DELETES_PER_ROOT:
        return True
    if total_documents <= 0:
        return False
    return pending / total_documents <= max_delete_fraction
