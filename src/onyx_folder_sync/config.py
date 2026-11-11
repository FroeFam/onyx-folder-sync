"""Configuration loading and validation (section 5.8), token lookup, network paths."""

from __future__ import annotations

import ctypes
import os
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from onyx_folder_sync.interfaces import ConfigError
from onyx_folder_sync.models import Config

NETWORK_FS_TYPES: frozenset[str] = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smb3",
        "smbfs",
        "9p",
        "fuse.sshfs",
        "ncpfs",
        "afs",
        "ceph",
        "glusterfs",
    }
)

_DRIVE_REMOTE = 4
_TOKEN_MESSAGE = (
    "the config file must not contain an API token (found key '{key}'); "  # noqa: S105
    "provide it through the ONYX_TOKEN or ONYX_TOKEN_FILE environment variable"
)


def _find_token_key(data: Any) -> str | None:
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(key, str) and "token" in key.lower():
                return key
            found = _find_token_key(value)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_token_key(item)
            if found is not None:
                return found
    return None


def _format_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors(include_input=False, include_url=False):
        loc = ".".join(str(p) for p in err["loc"]) or "(top level)"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def _normalized_parts(path: str) -> tuple[str, ...]:
    return Path(os.path.normcase(os.path.abspath(path))).parts


def _check_roots(config: Config) -> None:
    seen: dict[str, str] = {}
    for root in config.roots:
        key = root.name.casefold()
        if key in seen:
            raise ConfigError(
                f"duplicate root name '{root.name}' (also '{seen[key]}'); "
                "root names are compared case-insensitively"
            )
        seen[key] = root.name
    parts = [
        (root.name, _normalized_parts(root.path), _identity_chain(root.path))
        for root in config.roots
    ]
    for i, (name_a, a, ids_a) in enumerate(parts):
        for name_b, b, ids_b in parts[i + 1 :]:
            n = min(len(a), len(b))
            same_dir = (ids_a[0] is not None and ids_a[0] in ids_b) or (
                ids_b[0] is not None and ids_b[0] in ids_a
            )
            if a[:n] == b[:n] or same_dir:
                raise ConfigError(
                    f"roots '{name_a}' and '{name_b}' are equal, nested or overlapping"
                )


def _identity_chain(path: str) -> list[tuple[int, int] | None]:
    """(device, inode) of the directory and of each of its ancestors.

    Path text alone misses two spellings of one directory: a case-insensitive
    mount on a case-sensitive host, a symlink, a second mount of one share.
    None stands for a directory that cannot be read or has no stable identity.
    """
    absolute = Path(os.path.abspath(path))
    chain: list[tuple[int, int] | None] = []
    for directory in (absolute, *absolute.parents):
        try:
            st = os.stat(directory)
        except OSError:
            chain.append(None)
            continue
        chain.append((st.st_dev, st.st_ino) if st.st_ino else None)
    return chain


def load_config(path: Path) -> Config:
    """Read and validate the YAML config. Raises ConfigError for anything wrong."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must contain a mapping at the top level")
    token_key = _find_token_key(data)
    if token_key is not None:
        raise ConfigError(_TOKEN_MESSAGE.format(key=token_key))
    try:
        config = Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"invalid config: {_format_validation_error(exc)}") from exc
    _check_roots(config)
    if is_network_path(Path(config.state_path)):
        raise ConfigError(
            f"state_path '{config.state_path}' is on a network filesystem; "
            "the state database needs a local disk"
        )
    return config


def read_token(env: Mapping[str, str]) -> str:
    """ONYX_TOKEN, else the stripped content of the file named by ONYX_TOKEN_FILE."""
    token = env.get("ONYX_TOKEN", "").strip()
    if token:
        return token
    file_name = env.get("ONYX_TOKEN_FILE", "").strip()
    if file_name:
        try:
            token = Path(file_name).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(
                f"cannot read the token file named by ONYX_TOKEN_FILE ({file_name}): "
                f"{type(exc).__name__}"
            ) from exc
        if token:
            return token
        raise ConfigError(f"the token file named by ONYX_TOKEN_FILE ({file_name}) is empty")
    raise ConfigError("no API token: set ONYX_TOKEN or ONYX_TOKEN_FILE")


def _unescape_mount_field(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)


def parse_mounts(text: str) -> list[tuple[str, str]]:
    """Parse /proc/mounts text into (mount_point, fs_type) pairs."""
    mounts = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 3:
            mounts.append((_unescape_mount_field(fields[1]), fields[2]))
    return mounts


def is_network_mount(path: str, mounts: Iterable[tuple[str, str]]) -> bool:
    """True when the longest mount point containing path has a network fs type."""
    path_parts = tuple(p for p in path.split("/") if p)
    best_len = -1
    best_type = ""
    for mount_point, fs_type in mounts:
        mp_parts = tuple(p for p in mount_point.split("/") if p)
        if path_parts[: len(mp_parts)] == mp_parts and len(mp_parts) > best_len:
            best_len = len(mp_parts)
            best_type = fs_type
    return best_type in NETWORK_FS_TYPES


def _nearest_existing(path: Path) -> Path:
    current = Path(os.path.abspath(path))
    while not current.exists():
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current


def _is_unc(path: str) -> bool:
    return path.startswith(("\\\\", "//")) and not path.startswith(("\\\\?\\", "\\\\.\\"))


def is_network_path(path: Path) -> bool:
    """Whether the nearest existing ancestor of path lies on a network filesystem."""
    if sys.platform == "win32":
        raw = str(path)
        if _is_unc(raw) or raw.upper().startswith("\\\\?\\UNC\\"):
            return True
        existing = str(_nearest_existing(path))
        if _is_unc(existing) or existing.upper().startswith("\\\\?\\UNC\\"):
            return True
        drive = os.path.splitdrive(existing)[0]
        if not drive:
            return False
        get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
        return bool(get_drive_type(drive + "\\") == _DRIVE_REMOTE)
    if sys.platform.startswith("linux"):
        try:
            mounts_text = Path("/proc/mounts").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        existing = os.path.realpath(_nearest_existing(path))
        return is_network_mount(existing, parse_mounts(mounts_text))
    return False
