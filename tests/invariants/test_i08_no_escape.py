"""I8: no file outside the configured roots is opened, including through links."""

from __future__ import annotations

from pathlib import Path

import pytest

from onyx_folder_sync.models import ExitCode
from tests.invariants.harness import Env, recording_scandir

SECRET = "TOP-SECRET-OUTSIDE-CONTENT-7731"  # noqa: S105


def _outside_tree(env: Env) -> Path:
    outside = env.tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET, encoding="utf-8")
    (outside / "nested").mkdir()
    (outside / "nested" / "deep.txt").write_text(SECRET + " deep", encoding="utf-8")
    return outside


def _link_to_outside(env: Env, outside: Path) -> int:
    """Create every kind of link the platform allows; returns how many were created."""
    made = 0
    if env.tree.symlink("link-to-secret.txt", outside / "secret.txt"):
        made += 1
    if env.tree.symlink("dir-link", outside):
        made += 1
    if env.tree.junction("junction", outside):
        made += 1
    return made


def _inside(path: str, root: Path) -> bool:
    real_root = root.resolve()
    return Path(path).resolve().is_relative_to(real_root) and Path(path).absolute().is_relative_to(
        root.absolute()
    )


def test_i8_links_pointing_outside_the_root_are_never_read(env: Env) -> None:
    outside = _outside_tree(env)
    env.tree.file("inside.txt", "inside content")
    env.tree.file("sub/also-inside.txt", "also inside")
    if _link_to_outside(env, outside) == 0:
        pytest.skip("this platform refuses both symlinks and junctions for this user")
    scanned: list[str] = []
    for _ in range(2):
        report = env.run(scandir=recording_scandir(scanned))
        assert report.exit_code == ExitCode.OK
    assert all(SECRET not in d.text for d in env.fake.documents.values())
    assert all(SECRET not in line for line in env.log_lines)
    assert env.runner.calls, "the run extracted nothing"
    for call in env.runner.calls:
        assert _inside(call, env.root), f"extractor was handed a path outside the root: {call}"
    for listed in scanned:
        assert _inside(listed, env.root), f"directory outside the root was listed: {listed}"
    assert set(env.onyx_content()) == {"inside.txt", "sub/also-inside.txt"}


def test_i8_link_added_after_the_first_run_is_not_followed(env: Env) -> None:
    outside = _outside_tree(env)
    env.tree.file("inside.txt", "inside content")
    assert env.run().exit_code == ExitCode.OK
    if _link_to_outside(env, outside) == 0:
        pytest.skip("this platform refuses both symlinks and junctions for this user")
    env.runner.calls.clear()
    assert env.run().exit_code == ExitCode.OK
    assert all(SECRET not in d.text for d in env.fake.documents.values())
    assert all(_inside(c, env.root) for c in env.runner.calls)


def test_i8_link_that_replaces_an_indexed_file_causes_no_read(
    env: Env,
) -> None:
    outside = _outside_tree(env)
    env.tree.file("doc.txt", "original")
    env.tree.file("anchor.txt", "anchor")
    assert env.run().exit_code == ExitCode.OK
    env.tree.remove("doc.txt")
    if not env.tree.symlink("doc.txt", outside / "secret.txt"):
        pytest.skip("this platform refuses file symlinks for this user")
    env.runner.calls.clear()
    for _ in range(3):
        env.run()
    assert all(SECRET not in d.text for d in env.fake.documents.values())
    assert all(_inside(c, env.root) for c in env.runner.calls)
    for call in env.runner.calls:
        assert Path(call).resolve() != (outside / "secret.txt").resolve()


def test_i8_unrelated_sibling_directory_with_a_similar_name_is_not_touched(env: Env) -> None:
    sibling = env.tmp_path / "data-private"
    sibling.mkdir()
    (sibling / "private.txt").write_text(SECRET, encoding="utf-8")
    env.tree.file("a.txt", "a")
    scanned: list[str] = []
    env.run(scandir=recording_scandir(scanned))
    assert all(_inside(s, env.root) for s in scanned)
    assert all(SECRET not in d.text for d in env.fake.documents.values())
