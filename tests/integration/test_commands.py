"""End-to-end runs of the commands against the fake Onyx server."""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from onyx_folder_sync.cli import main
from onyx_folder_sync.lock import acquire_lock
from onyx_folder_sync.models import ExitCode
from onyx_folder_sync.state import open_state
from tests.fake_onyx import FakeOnyx
from tests.fixtures import TreeBuilder, write_config


@dataclass
class Env:
    onyx: FakeOnyx
    tree: TreeBuilder
    config: Path
    state_path: Path

    def run(self, *args: str) -> int:
        return main(["--config", str(self.config), *args])

    def texts(self) -> dict[str, str]:
        return {d.metadata.get("path", d.document_id): d.text for d in self.onyx.documents.values()}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    with FakeOnyx() as onyx:
        monkeypatch.setenv("ONYX_TOKEN", onyx.token)
        tree = TreeBuilder(tmp_path / "docs")
        state_path = tmp_path / "state" / "sync.db"
        config = write_config(
            tmp_path / "config.yml",
            onyx_url=onyx.url,
            state_path=state_path,
            roots={"docs": tree.root},
            root_options={"docs": {"link_template": "file:///mnt/docs/{relpath}"}},
        )
        yield Env(onyx, tree, config, state_path)


def test_sync_pushes_updates_and_deletes(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.tree.file("a/one.txt", "first")
    env.tree.file("two words.md", "second")
    env.tree.file("skip.bin", "binary")
    env.tree.file("empty.txt", "")
    assert env.run("sync") == ExitCode.OK
    assert env.texts() == {"a/one.txt": "first", "two words.md": "second"}
    entry = next(iter(env.onyx.entries.values()))
    assert entry.source == "ingestion_api"
    assert entry.prune_freq is None
    assert entry.name.startswith("folder: docs [")
    doc = next(d for d in env.onyx.documents.values() if d.metadata["path"] == "two words.md")
    assert doc.document_id.startswith("fc_")
    assert doc.link == "file:///mnt/docs/two%20words.md"
    assert doc.metadata == {"root": "docs", "path": "two words.md", "extension": "md"}
    assert "pushed 2" in capsys.readouterr().out

    # Second pass: nothing to do, no request to the ingestion endpoint.
    before = len(env.onyx.requests_matching("POST", "/onyx-api/ingestion"))
    assert env.run("sync") == ExitCode.OK
    assert len(env.onyx.requests_matching("POST", "/onyx-api/ingestion")) == before
    assert "unchanged 2" in capsys.readouterr().out

    env.tree.touch_changed("a/one.txt", "first, edited")
    env.tree.remove("two words.md")
    assert env.run("sync") == ExitCode.OK
    assert env.texts() == {"a/one.txt": "first, edited", "two words.md": "second"}
    # Absent in a second consecutive walk: now it goes.
    assert env.run("sync") == ExitCode.OK
    assert env.texts() == {"a/one.txt": "first, edited"}


def test_dry_run_writes_nothing(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.tree.file("one.txt", "first")
    assert env.run("sync", "--dry-run") == ExitCode.OK
    out = capsys.readouterr().out
    assert "would push 1" in out
    assert "push   one.txt" in out
    assert env.onyx.documents == {}
    assert env.onyx.entries == {}
    assert [r.method for r in env.onyx.requests] == ["GET"]
    assert not env.state_path.parent.exists()

    # With existing state, a dry run leaves it untouched.
    assert env.run("sync") == ExitCode.OK
    env.tree.file("two.txt", "second")
    env.tree.remove("one.txt")
    state = open_state(env.state_path)
    before = (state.list_documents(state.list_roots()[0].root_id), state.last_run())
    state.close()
    requests = len(env.onyx.requests)
    assert env.run("sync", "--dry-run") == ExitCode.OK
    assert "push   two.txt" in capsys.readouterr().out
    state = open_state(env.state_path)
    assert (state.list_documents(state.list_roots()[0].root_id), state.last_run()) == before
    state.close()
    assert [r.method for r in env.onyx.requests[requests:]] == ["GET", "GET"]


def test_delete_guard_blocks_then_allows(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    rels = env.tree.files(20)
    assert env.run("sync") == ExitCode.OK
    for rel in rels[:12]:
        env.tree.remove(rel)
    assert env.run("sync") == ExitCode.OK  # first walk without them
    assert env.run("sync") == ExitCode.DELETE_GUARD_BLOCKED
    assert len(env.onyx.documents) == 20
    assert env.onyx.requests_matching("DELETE", "") == []
    assert env.run("status") == ExitCode.OK
    assert "BLOCKED" in capsys.readouterr().out
    assert env.run("sync", "--allow-deletes", "11") == ExitCode.DELETE_GUARD_BLOCKED
    assert len(env.onyx.documents) == 20
    assert env.run("sync", "--allow-deletes", "12") == ExitCode.OK
    assert len(env.onyx.documents) == 8


def test_exit_codes(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.tree.file("one.txt", "first")
    monkeypatch.setenv("ONYX_TOKEN", "wrong-token")
    assert env.run("sync") == ExitCode.AUTH_FAILURE
    monkeypatch.setenv("ONYX_TOKEN", env.onyx.token)
    with acquire_lock(env.state_path):
        assert env.run("sync") == ExitCode.LOCK_HELD
    env.onyx.fail_next("POST", "/onyx-api/ingestion", 400, body={"error_code": "INVALID_INPUT"})
    assert env.run("sync") == ExitCode.DOCUMENT_FAILURES
    assert env.run("sync") == ExitCode.OK  # the stored error is retried
    assert len(env.onyx.documents) == 1
    env.onyx.stop()
    assert env.run("sync") == ExitCode.OK  # unreachable at preflight
    env.onyx.start()


def test_missing_root_and_stale_state_root(env: Env, tmp_path: Path) -> None:
    env.tree.file("one.txt", "first")
    assert env.run("sync") == ExitCode.OK
    other = TreeBuilder(tmp_path / "other")
    other.file("x.txt", "x")
    write_config(
        env.config, onyx_url=env.onyx.url, state_path=env.state_path, roots={"other": other.root}
    )
    assert env.run("sync") == ExitCode.CONFIG_OR_STATE  # 'docs' is in state, not in config
    assert env.run("rename-root", "docs", "other") == ExitCode.OK
    assert env.run("sync") == ExitCode.OK
    write_config(
        env.config,
        onyx_url=env.onyx.url,
        state_path=env.state_path,
        roots={"other": tmp_path / "gone"},
    )
    assert env.run("sync") == ExitCode.CONFIG_OR_STATE
    assert env.onyx.requests_matching("DELETE", "") == []


def test_preflight_refuses_a_pruned_entry(env: Env) -> None:
    env.tree.file("one.txt", "first")
    assert env.run("sync") == ExitCode.OK
    cc_pair = next(iter(env.onyx.entries))
    env.onyx.set_prune_freq(cc_pair, 86400)
    env.tree.file("two.txt", "second")
    assert env.run("sync") == ExitCode.CONFIG_OR_STATE
    assert len(env.onyx.documents) == 1


def test_reconcile_and_forget(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.tree.file("keep.txt", "keep")
    env.tree.file("lost.txt", "lost")
    assert env.run("sync") == ExitCode.OK
    cc_pair = next(iter(env.onyx.entries))
    lost_id = next(
        d.document_id for d in env.onyx.documents.values() if d.metadata["path"] == "lost.txt"
    )
    del env.onyx.documents[lost_id]
    # An fc_ id that state does not know and that has no file, and a foreign id.
    env.onyx.add_foreign_document("fc_ffffffffffffffffffffffffffffffff", cc_pair)
    env.onyx.documents["fc_ffffffffffffffffffffffffffffffff"].via_ingestion_api = True
    env.onyx.add_foreign_document("https://example.com/page", cc_pair)
    assert env.run("reconcile", "docs") == ExitCode.OK
    assert {env.texts().get(p) for p in ("keep.txt", "lost.txt")} == {"keep", "lost"}
    assert "fc_ffffffffffffffffffffffffffffffff" not in env.onyx.documents
    assert "https://example.com/page" in env.onyx.documents
    capsys.readouterr()

    assert env.run("forget", "docs") == ExitCode.OK
    assert env.onyx.entries == {}
    assert env.onyx.documents == {}
    assert env.run("status") == ExitCode.OK
    assert "root 'docs' (" not in capsys.readouterr().out


def test_extract_report(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.tree.file("one.txt", "first")
    env.tree.file("empty.md", "")
    assert env.run("extract-report") == ExitCode.OK
    out = capsys.readouterr().out
    assert "files: 2, total text: 5 characters" in out
    assert env.onyx.requests == []
