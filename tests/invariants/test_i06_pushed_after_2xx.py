"""I6: state marks a document as pushed only after a 2xx response for it."""

from __future__ import annotations

from typing import Any

from onyx_folder_sync.models import DocumentRecord, ExitCode
from tests.invariants.harness import Env, Hook

INGESTION = "/onyx-api/ingestion$"


def _pushed_ok_ids(env: Env) -> set[str]:
    ok: set[str] = set()
    for req in env.fake.requests:
        if req.method == "POST" and req.path == "/api/onyx-api/ingestion" and req.status == 200:
            ok.add(str(req.body["document"]["id"]))
    return ok


def _spy(env: Env, violations: list[str]) -> Any:
    """Wrap the state so every write that sets an onyx_id is checked against the request log."""

    def check(record: DocumentRecord, **_: Any) -> None:
        if record.onyx_id is not None and record.onyx_id not in _pushed_ok_ids(env):
            violations.append(f"{record.rel_path} marked pushed without a 2xx for {record.onyx_id}")

    def wrap(state: Any) -> Hook:
        return Hook(state, "upsert_document", before=check)

    return wrap


def _assert_state_matches_onyx(env: Env) -> None:
    for rel, record in env.documents().items():
        if record.onyx_id is not None:
            assert record.onyx_id in env.fake.documents, f"{rel} marked pushed, Onyx lacks it"


def test_i6_state_is_written_only_after_a_2xx_for_the_document(env: Env) -> None:
    env.tree.files(6)
    violations: list[str] = []
    report = env.run(wrap_state=_spy(env, violations))
    assert report.exit_code == ExitCode.OK
    assert violations == []
    assert all(d.onyx_id for d in env.documents().values())


def test_i6_failed_push_leaves_the_row_without_an_onyx_id(env: Env) -> None:
    env.tree.files(4)
    env.fake.fail_next("POST", INGESTION, 500, times=3)  # all attempts of the first document
    violations: list[str] = []
    report = env.run(wrap_state=_spy(env, violations))
    assert report.exit_code == ExitCode.DOCUMENT_FAILURES
    assert report.roots[0].failed == 1
    assert report.roots[0].pushed == 3
    assert violations == []
    rows = env.documents()
    unpushed = [r for r in rows.values() if r.onyx_id is None]
    assert len(unpushed) == 1
    assert unpushed[0].last_error
    _assert_state_matches_onyx(env)
    assert len(env.fake.documents) == 3


def test_i6_failed_document_is_retried_and_converges(env: Env) -> None:
    env.tree.files(4)
    env.fake.fail_next("POST", INGESTION, 500, times=3)
    env.run()
    report = env.run()
    assert report.exit_code == ExitCode.OK
    assert report.roots[0].pushed == 1
    assert env.onyx_content() == env.disk_content()
    assert all(d.last_error is None for d in env.documents().values())


def test_i6_transient_conflict_is_retried_before_state_is_written(env: Env) -> None:
    env.tree.files(3)
    env.fake.fail_next("POST", INGESTION, 409, times=1)
    violations: list[str] = []
    report = env.run(wrap_state=_spy(env, violations))
    assert report.exit_code == ExitCode.OK
    assert violations == []
    assert env.onyx_content() == env.disk_content()


def test_i6_authorization_failure_during_a_push_marks_nothing(env: Env) -> None:
    env.tree.files(4)
    env.fake.fail_next("POST", INGESTION, 403, times=1)
    violations: list[str] = []
    report = env.run(wrap_state=_spy(env, violations))
    assert report.exit_code == ExitCode.AUTH_FAILURE
    assert violations == []
    assert len(env.pushes()) == 1  # the run stopped at the first rejected push
    assert all(d.onyx_id is None for d in env.documents().values())
    assert env.fake.documents == {}


def test_i6_changed_file_keeps_its_old_onyx_id_when_the_new_push_fails(env: Env) -> None:
    env.tree.file("a.txt", "version one")
    env.run()
    first = env.documents()["a.txt"]
    env.tree.touch_changed("a.txt", "version two")
    env.fake.fail_next("POST", INGESTION, 500, times=3)
    report = env.run()
    assert report.roots[0].failed == 1
    row = env.documents()["a.txt"]
    assert row.onyx_id == first.onyx_id  # still proves ownership of the old document
    assert row.payload_hash == first.payload_hash  # not recorded as pushed
    assert env.onyx_content()["a.txt"] == "version one"
    report = env.run()
    assert report.exit_code == ExitCode.OK
    assert env.onyx_content()["a.txt"] == "version two"


def test_i6_entry_lost_between_preflight_and_push_marks_nothing(env: Env) -> None:
    env.tree.files(3)
    env.run()
    cc = env.cc_pair_id()
    before = env.documents()
    env.tree.touch_changed("doc0000.txt", "changed")
    dropped: list[bool] = []

    def drop(method: str, path: str, body: Any) -> None:
        if method == "POST" and path == "/api/onyx-api/ingestion" and not dropped:
            dropped.append(True)
            env.fake.drop_entry(cc)

    env.fake.on_request.append(drop)
    violations: list[str] = []
    report = env.run(wrap_state=_spy(env, violations))
    assert dropped
    assert report.exit_code != ExitCode.OK
    assert violations == []
    assert env.documents()["doc0000.txt"].payload_hash == before["doc0000.txt"].payload_hash
