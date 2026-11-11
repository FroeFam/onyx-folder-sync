from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from onyx_folder_sync import config as config_module
from onyx_folder_sync.config import (
    is_network_mount,
    is_network_path,
    load_config,
    parse_mounts,
    read_token,
)
from onyx_folder_sync.interfaces import ConfigError


def write(tmp_path: Path, data: Any) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data), encoding="utf-8")
    return path


def base(tmp_path: Path, roots: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "onyx": {"url": "http://localhost:3000"},
        "state_path": str(tmp_path / "state" / "state.db"),
        "roots": roots if roots is not None else [{"name": "docs", "path": str(tmp_path / "docs")}],
    }


def test_valid_config(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, base(tmp_path)))
    assert config.roots[0].name == "docs"
    assert config.defaults.max_file_mb == 20


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(write(tmp_path, "a: [1, 2\nb: {"))


def test_not_a_mapping(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="mapping"):
        load_config(write(tmp_path, "- a\n- b\n"))


def test_unknown_field_and_missing_field(tmp_path: Path) -> None:
    data = base(tmp_path)
    data["surprise"] = 1
    with pytest.raises(ConfigError, match="surprise"):
        load_config(write(tmp_path, data))
    del data["surprise"]
    del data["state_path"]
    with pytest.raises(ConfigError, match="state_path"):
        load_config(write(tmp_path, data))


def test_empty_roots(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="roots"):
        load_config(write(tmp_path, base(tmp_path, roots=[])))


def test_duplicate_names_case_insensitive(tmp_path: Path) -> None:
    roots = [
        {"name": "Docs", "path": str(tmp_path / "a")},
        {"name": "dOCS", "path": str(tmp_path / "b")},
    ]
    with pytest.raises(ConfigError, match="duplicate root name"):
        load_config(write(tmp_path, base(tmp_path, roots)))


@pytest.mark.parametrize(
    ("a", "b"),
    [("a", "a"), ("a", "a/b"), ("a/b", "a"), ("a/b/../c", "a/c/d")],
)
def test_overlapping_roots_rejected(tmp_path: Path, a: str, b: str) -> None:
    roots = [
        {"name": "one", "path": str(tmp_path / a)},
        {"name": "two", "path": str(tmp_path / b)},
    ]
    with pytest.raises(ConfigError, match="nested or overlapping"):
        load_config(write(tmp_path, base(tmp_path, roots)))


def test_sibling_prefix_roots_allowed(tmp_path: Path) -> None:
    roots = [
        {"name": "one", "path": str(tmp_path / "b")},
        {"name": "two", "path": str(tmp_path / "bc")},
    ]
    assert len(load_config(write(tmp_path, base(tmp_path, roots))).roots) == 2


@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitive paths are Windows only")
def test_overlap_case_insensitive_on_windows(tmp_path: Path) -> None:
    roots = [
        {"name": "one", "path": str(tmp_path / "Data")},
        {"name": "two", "path": str(tmp_path / "data" / "sub")},
    ]
    with pytest.raises(ConfigError, match="nested or overlapping"):
        load_config(write(tmp_path, base(tmp_path, roots)))


@pytest.mark.skipif(sys.platform != "win32", reason="needs a case-insensitive filesystem")
def test_overlap_found_by_directory_identity_when_spelling_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stands in for a case-insensitive share mounted on a case-sensitive host,
    # where the path text gives no hint that both spellings are one directory.
    (tmp_path / "Data" / "sub").mkdir(parents=True)
    monkeypatch.setattr("os.path.normcase", lambda p: p)
    roots = [
        {"name": "one", "path": str(tmp_path / "Data")},
        {"name": "two", "path": str(tmp_path / "data" / "sub")},
    ]
    with pytest.raises(ConfigError, match="nested or overlapping"):
        load_config(write(tmp_path, base(tmp_path, roots)))


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_overlap_through_a_symlink_rejected(tmp_path: Path) -> None:
    (tmp_path / "real" / "sub").mkdir(parents=True)
    (tmp_path / "alias").symlink_to(tmp_path / "real", target_is_directory=True)
    roots = [
        {"name": "one", "path": str(tmp_path / "real")},
        {"name": "two", "path": str(tmp_path / "alias" / "sub")},
    ]
    with pytest.raises(ConfigError, match="nested or overlapping"):
        load_config(write(tmp_path, base(tmp_path, roots)))


def test_token_in_file_rejected(tmp_path: Path) -> None:
    data = base(tmp_path)
    data["onyx"]["token"] = "secret-value-123"  # noqa: S105
    with pytest.raises(ConfigError, match="ONYX_TOKEN") as info:
        load_config(write(tmp_path, data))
    assert "secret-value-123" not in str(info.value)


def test_nested_token_key_rejected(tmp_path: Path) -> None:
    data = base(tmp_path)
    data["onyx"]["auth"] = {"api_token": "secret-value-123"}
    with pytest.raises(ConfigError, match="ONYX_TOKEN") as info:
        load_config(write(tmp_path, data))
    assert "secret-value-123" not in str(info.value)


def test_network_state_path_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "is_network_path", lambda p: True)
    with pytest.raises(ConfigError, match="network filesystem"):
        load_config(write(tmp_path, base(tmp_path)))


def test_read_token_env_wins_and_is_stripped() -> None:
    assert read_token({"ONYX_TOKEN": "  abc \n", "ONYX_TOKEN_FILE": "x"}) == "abc"


def test_read_token_from_file(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("  file-token\n", encoding="utf-8")
    assert read_token({"ONYX_TOKEN_FILE": str(token_file)}) == "file-token"
    assert read_token({"ONYX_TOKEN": "  ", "ONYX_TOKEN_FILE": str(token_file)}) == "file-token"


def test_read_token_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no API token"):
        read_token({})
    empty = tmp_path / "empty"
    empty.write_text(" \n", encoding="utf-8")
    with pytest.raises(ConfigError, match="empty"):
        read_token({"ONYX_TOKEN_FILE": str(empty)})
    with pytest.raises(ConfigError, match="cannot read"):
        read_token({"ONYX_TOKEN_FILE": str(tmp_path / "missing")})


MOUNTS = (
    "rootfs / ext4 rw 0 0\n"
    "srv:/export /mnt/nas nfs4 rw 0 0\n"
    "//nas/share /mnt/nas/local\\040dir cifs rw 0 0\n"
    "tmpfs /mnt/nas/cache tmpfs rw 0 0\n"
    "garbage\n"
)


def test_parse_mounts() -> None:
    mounts = parse_mounts(MOUNTS)
    assert ("/", "ext4") in mounts
    assert ("/mnt/nas/local dir", "cifs") in mounts
    assert len(mounts) == 4


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/home/user/state.db", False),
        ("/mnt/nas", True),
        ("/mnt/nas/sub/state.db", True),
        ("/mnt/nas/cache/state.db", False),
        ("/mnt/nas/local dir/x", True),
        ("/mnt/nasty/x", False),
    ],
)
def test_is_network_mount(path: str, expected: bool) -> None:
    assert is_network_mount(path, parse_mounts(MOUNTS)) is expected


def test_is_network_path_local(tmp_path: Path) -> None:
    assert is_network_path(tmp_path / "does" / "not" / "exist.db") is False


@pytest.mark.skipif(sys.platform != "win32", reason="UNC paths are Windows only")
def test_is_network_path_unc() -> None:
    assert is_network_path(Path("\\\\server\\share\\state.db")) is True
