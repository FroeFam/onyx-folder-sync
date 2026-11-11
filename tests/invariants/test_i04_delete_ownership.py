"""I4: no delete for an id that is not in state for that root, or has no fc_ prefix."""

from __future__ import annotations

from onyx_folder_sync.models import ID_PREFIX, DocumentRecord, ExitCode
from onyx_folder_sync.state import open_state
from tests.fixtures import TreeBuilder, failing_scandir
from tests.invariants.harness import Env


def _owned_ids(env: Env, name: str) -> set[str]:
    return {d.onyx_id for d in env.documents(name).values() if d.onyx_id is not None}


def test_i4_foreign_documents_in_the_entry_are_never_deleted(env: Env) -> None:
    env.tree.files(5)
    env.run()
    cc = env.cc_pair_id()
    env.fake.add_foreign_document("manual_upload_1", cc)
    env.put_document("legacy_api_doc", "legacy", cc)  # API document, but no fc_ prefix
    env.put_document(ID_PREFIX + "0" * 32, "stray fc doc not in state", cc)
    for rel in ("doc0000.txt", "doc0001.txt"):
        env.tree.remove(rel)
    mark = env.mark()
    for _ in range(4):
        assert env.run().exit_code == ExitCode.OK
    sent = env.delete_ids(mark)
    assert sorted(sent) == sorted([env.doc_id("doc0000.txt"), env.doc_id("doc0001.txt")])
    assert "manual_upload_1" in env.fake.documents
    assert "legacy_api_doc" in env.fake.documents
    assert ID_PREFIX + "0" * 32 in env.fake.documents


def test_i4_every_delete_is_an_fc_id_that_state_holds_for_that_root(env: Env) -> None:
    other = env.tmp_path / "other"
    env.tree.files(12)
    other_tree = TreeBuilder(other)
    other_tree.files(12, prefix="o")
    config = env.make_config(roots={"main": env.root, "other": other})
    env.run(config=config)
    owned_before = {"main": _owned_ids(env, "main"), "other": _owned_ids(env, "other")}
    for rel in ("doc0000.txt", "doc0001.txt", "doc0002.txt"):
        env.tree.remove(rel)
    for rel in ("o0000.txt", "o0001.txt"):
        other_tree.remove(rel)
    mark = env.mark()
    for _ in range(3):
        env.run(config=config)
    sent = env.delete_ids(mark)
    assert len(sent) == 5
    assert all(s.startswith(ID_PREFIX) for s in sent)
    assert set(sent) <= owned_before["main"] | owned_before["other"]
    main_sent = {s for s in sent if s in owned_before["main"]}
    assert len(main_sent) == 3
    assert main_sent.isdisjoint(owned_before["other"])


def test_i4_state_row_with_a_foreign_onyx_id_is_never_deleted(env: Env) -> None:
    env.tree.files(4)
    env.run()
    cc = env.cc_pair_id()
    env.fake.add_foreign_document("hand_made_doc", cc)
    state_rows = env.documents()
    template = next(iter(state_rows.values()))
    state = open_state(env.state_path)
    try:
        state.upsert_document(
            DocumentRecord(
                root_id=template.root_id,
                rel_path="never-existed.txt",
                size=1,
                mtime_ns=1,
                payload_hash="h",
                extractor_version="x",
                onyx_id="hand_made_doc",
                pushed_at="2026-01-01T00:00:00Z",
            )
        )
    finally:
        state.close()
    mark = env.mark()
    for _ in range(4):
        env.run()
    assert "hand_made_doc" not in env.delete_ids(mark)
    assert "hand_made_doc" in env.fake.documents


def test_i4_id_belonging_to_another_root_is_not_deleted_through_this_root(env: Env) -> None:
    other = env.tmp_path / "other"
    env.tree.files(3)
    TreeBuilder(other).files(3, prefix="o")
    config = env.make_config(roots={"main": env.root, "other": other})
    env.run(config=config)
    other_ids = _owned_ids(env, "other")
    for rel in env.disk_content():
        env.tree.remove(rel)
    env.tree.file("survivor.txt", "survivor")
    mark = env.mark()
    for _ in range(4):
        env.run(config=config)
    assert set(env.delete_ids(mark)).isdisjoint(other_ids)
    assert len(env.fake.documents_of(env.cc_pair_id("other"))) == 3


def test_i4_reconcile_never_deletes_ids_without_the_fc_prefix(env: Env) -> None:
    env.tree.files(4)
    env.run()
    cc = env.cc_pair_id()
    env.fake.add_foreign_document("manual_upload_1", cc)
    env.put_document("legacy_api_doc", "legacy", cc)
    env.put_document(ID_PREFIX + "f" * 32, "stray", cc)
    mark = env.mark()
    report = env.reconcile()
    sent = env.delete_ids(mark)
    assert env.doc_id("doc0000.txt") not in sent
    assert all(s.startswith(ID_PREFIX) for s in sent)
    assert "manual_upload_1" in env.fake.documents
    assert "legacy_api_doc" in env.fake.documents
    assert report.exit_code == ExitCode.OK
    assert ID_PREFIX + "f" * 32 not in env.fake.documents  # no file for it: removed


def test_i4_reconcile_deletes_only_ids_whose_file_is_not_on_disk(env: Env) -> None:
    env.tree.files(4)
    env.run()
    cc = env.cc_pair_id()
    env.tree.file("excluded/e.txt", "excluded but present")
    excluded_id = env.doc_id("excluded/e.txt")
    env.put_document(excluded_id, "excluded but present", cc, "excluded/e.txt")
    absent_id = env.doc_id("long-gone.txt")
    env.put_document(absent_id, "long gone", cc, "long-gone.txt")
    config = env.make_config(excludes={"main": ("excluded/**",)})

    env.reconcile(config=config)
    assert excluded_id in env.fake.documents  # reported only
    assert absent_id not in env.fake.documents

    env.reconcile(config=config, delete_excluded=True)
    assert excluded_id not in env.fake.documents


def test_i4_reconcile_needs_a_complete_walk(env: Env) -> None:
    env.tree.files(4)
    env.tree.file("sub/inside.txt", "inside")
    env.run()
    cc = env.cc_pair_id()
    stray = env.doc_id("sub/removed.txt")
    env.put_document(stray, "stray", cc, "sub/removed.txt")
    also_stray = env.doc_id("gone-top.txt")
    env.put_document(also_stray, "stray", cc, "gone-top.txt")
    mark = env.mark()
    env.reconcile(scandir=failing_scandir(env.tree.path("sub")))
    assert env.delete_ids(mark) == []
    assert stray in env.fake.documents
    assert also_stray in env.fake.documents
