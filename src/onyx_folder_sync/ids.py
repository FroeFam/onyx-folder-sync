"""Document ids (decision 3)."""

import hashlib
import unicodedata

from onyx_folder_sync.models import ID_PREFIX


def normalize_rel_path(rel_path: str) -> str:
    """NFC, forward slashes, no leading or trailing slash."""
    return unicodedata.normalize("NFC", rel_path.replace("\\", "/")).strip("/")


def document_id(root_id: str, rel_path: str) -> str:
    """``fc_`` plus the first 32 hex characters of SHA-256 over root id and path."""
    data = f"{root_id}\n{normalize_rel_path(rel_path)}".encode()
    return ID_PREFIX + hashlib.sha256(data).hexdigest()[:32]
