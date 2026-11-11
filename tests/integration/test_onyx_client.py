"""Onyx client against the fake server (plan 5.5, Appendix A)."""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator
from urllib.parse import quote

import pytest

from onyx_folder_sync.interfaces import (
    OnyxApi,
    OnyxAuthError,
    OnyxEntryNotFoundError,
    OnyxError,
    OnyxRequestError,
    OnyxTimeoutError,
    OnyxTransportError,
)
from onyx_folder_sync.models import OnyxDocument, OnyxSection
from onyx_folder_sync.onyx_client import create_client
from tests.fake_onyx import FakeOnyx

TOKEN = "SECRET-token-0123456789"  # noqa: S105


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def onyx() -> Iterator[FakeOnyx]:
    with FakeOnyx(token=TOKEN) as server:
        yield server


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def client(onyx: FakeOnyx, sleeps: Sleeps) -> Iterator[OnyxApi]:
    api = create_client(onyx.url, TOKEN, sleep=sleeps)
    yield api
    api.close()


def make_document(doc_id: str = "fc_0123456789abcdef0123456789abcdef") -> OnyxDocument:
    return OnyxDocument(
        id=doc_id,
        semantic_identifier="report.pdf",
        title="report.pdf",
        sections=[OnyxSection(text="hello", link="file:///data/a/report.pdf")],
        metadata={"root": "docs", "path": "a/report.pdf", "extension": "pdf"},
        doc_updated_at="2026-01-01T00:00:00Z",
    )


# -- happy paths -------------------------------------------------------------


def test_check_auth(onyx: FakeOnyx, client: OnyxApi) -> None:
    client.check_auth()
    assert [(r.method, r.path, r.status) for r in onyx.requests] == [("GET", "/api/me", 200)]


def test_base_url_trailing_slash(onyx: FakeOnyx, sleeps: Sleeps) -> None:
    api = create_client(onyx.url + "/", TOKEN, sleep=sleeps)
    try:
        api.check_auth()
    finally:
        api.close()
    assert onyx.requests[0].path == "/api/me"


def test_create_entry(onyx: FakeOnyx, client: OnyxApi) -> None:
    cc_pair_id = client.create_entry("folder: docs [abc]")
    assert cc_pair_id == 5
    assert onyx.requests[0].body == {
        "name": "folder: docs [abc]",
        "source": "ingestion_api",
        "input_type": "load_state",
        "connector_specific_config": {},
        "refresh_freq": None,
        "prune_freq": None,
        "access_type": "public",
        "groups": [],
    }
    assert onyx.entries[cc_pair_id].prune_freq is None


def test_get_entry(onyx: FakeOnyx, client: OnyxApi) -> None:
    cc_pair_id = client.create_entry("e")
    info = client.get_entry(cc_pair_id)
    assert info is not None
    entry = onyx.entries[cc_pair_id]
    assert info.cc_pair_id == cc_pair_id
    assert info.name == "e"
    assert info.status == "SCHEDULED"
    assert info.num_docs_indexed == 0
    assert info.source == "ingestion_api"
    assert info.prune_freq is None
    assert info.connector_id == entry.connector_id
    assert info.credential_id == entry.credential_id


def test_get_entry_unknown_is_none(client: OnyxApi) -> None:
    assert client.get_entry(999) is None


def test_upsert_document(onyx: FakeOnyx, client: OnyxApi) -> None:
    cc_pair_id = client.create_entry("e")
    doc = make_document()
    client.upsert_document(doc, cc_pair_id)
    request = onyx.requests_matching("POST", "/api/onyx-api/ingestion")[0]
    assert request.body == {"document": doc.model_dump(exclude_none=True), "cc_pair_id": cc_pair_id}
    assert onyx.documents[doc.id].text == "hello"
    # Re-push of an existing document (already_existed true) is not an error.
    client.upsert_document(doc, cc_pair_id, timeout=30.0)
    assert len(onyx.documents) == 1


def test_upsert_omits_none_fields(onyx: FakeOnyx, client: OnyxApi) -> None:
    cc_pair_id = client.create_entry("e")
    doc = make_document()
    doc.sections = [OnyxSection(text="no link")]
    client.upsert_document(doc, cc_pair_id)
    request = onyx.requests_matching("POST", "/api/onyx-api/ingestion")[0]
    assert request.body["document"]["sections"] == [{"text": "no link"}]


def test_list_document_ids(onyx: FakeOnyx, client: OnyxApi) -> None:
    cc_pair_id = client.create_entry("e")
    client.upsert_document(make_document("fc_a"), cc_pair_id)
    client.upsert_document(make_document("fc_b"), cc_pair_id)
    assert sorted(client.list_document_ids(cc_pair_id)) == ["fc_a", "fc_b"]


def test_delete_document_percent_encodes(onyx: FakeOnyx, client: OnyxApi) -> None:
    cc_pair_id = client.create_entry("e")
    onyx.add_foreign_document("x", cc_pair_id)
    client.upsert_document(make_document("fc_a"), cc_pair_id)
    client.delete_document("fc_a")
    assert "fc_a" not in onyx.documents
    client.delete_document("a/b c:d")
    path = onyx.requests[-1].path
    assert path == "/api/onyx-api/ingestion/" + quote("a/b c:d", safe="")
    assert "/" not in path.removeprefix("/api/onyx-api/ingestion/")


def test_delete_unknown_document_succeeds(onyx: FakeOnyx, client: OnyxApi) -> None:
    client.delete_document("fc_unknown")
    assert onyx.requests[-1].status == 404


def test_remove_entry(onyx: FakeOnyx, client: OnyxApi) -> None:
    cc_pair_id = client.create_entry("e")
    client.upsert_document(make_document(), cc_pair_id)
    entry = onyx.entries[cc_pair_id]
    client.remove_entry(cc_pair_id, entry.connector_id, entry.credential_id)
    sent = [(r.method, r.path, r.body) for r in onyx.requests[-2:]]
    assert sent == [
        ("PUT", f"/api/manage/admin/cc-pair/{cc_pair_id}/status", {"status": "PAUSED"}),
        (
            "POST",
            "/api/manage/admin/deletion-attempt",
            {"connector_id": entry.connector_id, "credential_id": entry.credential_id},
        ),
    ]
    assert cc_pair_id not in onyx.entries
    assert not onyx.documents


# -- failure rules -----------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_status_not_retried(
    onyx: FakeOnyx, client: OnyxApi, sleeps: Sleeps, status: int
) -> None:
    onyx.auth_failure_status = status
    bad = create_client(onyx.url, "wrong-token", sleep=sleeps)
    try:
        with pytest.raises(OnyxAuthError):
            bad.check_auth()
    finally:
        bad.close()
    assert len(onyx.requests) == 1
    assert sleeps.calls == []


@pytest.mark.parametrize("status", [401, 403])
def test_injected_auth_failure(
    onyx: FakeOnyx, client: OnyxApi, sleeps: Sleeps, status: int
) -> None:
    onyx.fail_next("POST", "/ingestion", status)
    with pytest.raises(OnyxAuthError):
        client.upsert_document(make_document(), 5)
    assert len(onyx.requests) == 1
    assert sleeps.calls == []


@pytest.mark.parametrize("status", [409, 503])
def test_retry_succeeds_on_third_attempt(
    onyx: FakeOnyx, client: OnyxApi, sleeps: Sleeps, status: int
) -> None:
    onyx.fail_next("GET", "/api/me", status, times=2)
    client.check_auth()
    assert [r.status for r in onyx.requests] == [status, status, 200]
    assert sleeps.calls == [2.0, 4.0]


@pytest.mark.parametrize("status", [409, 500, 503])
def test_three_failures_raise(onyx: FakeOnyx, client: OnyxApi, sleeps: Sleeps, status: int) -> None:
    onyx.fail_next("GET", "/api/me", status, times=3)
    with pytest.raises(OnyxRequestError) as info:
        client.check_auth()
    assert info.value.status_code == status
    assert len(onyx.requests) == 3
    assert sleeps.calls == [2.0, 4.0]
    if status == 409:
        assert info.value.error_code == "CONFLICT"


def test_delete_retries_on_conflict(onyx: FakeOnyx, client: OnyxApi, sleeps: Sleeps) -> None:
    onyx.fail_next("DELETE", "/ingestion/", 409, times=1)
    client.delete_document("fc_unknown")
    assert [r.status for r in onyx.requests] == [409, 404]
    assert sleeps.calls == [2.0]


def test_other_status_fails_at_once(onyx: FakeOnyx, client: OnyxApi, sleeps: Sleeps) -> None:
    onyx.fail_next("GET", "/api/me", 422, body={"error_code": "BAD"})
    with pytest.raises(OnyxRequestError) as info:
        client.check_auth()
    assert info.value.status_code == 422
    assert info.value.error_code == "BAD"
    assert not isinstance(info.value, OnyxEntryNotFoundError)
    assert len(onyx.requests) == 1
    assert sleeps.calls == []


def test_connector_not_found(onyx: FakeOnyx, client: OnyxApi, sleeps: Sleeps) -> None:
    with pytest.raises(OnyxEntryNotFoundError) as info:
        client.upsert_document(make_document(), 999)
    assert info.value.error_code == "CONNECTOR_NOT_FOUND"
    assert info.value.status_code == 404
    assert len(onyx.requests) == 1
    assert sleeps.calls == []


def test_timeout_not_retried(onyx: FakeOnyx, client: OnyxApi, sleeps: Sleeps) -> None:
    cc_pair_id = client.create_entry("e")
    onyx.delay_next("POST", "/api/onyx-api/ingestion", 1.0)
    with pytest.raises(OnyxTimeoutError):
        client.upsert_document(make_document(), cc_pair_id, timeout=0.2)
    # The fake records a request only after its delay has passed.
    deadline = time.monotonic() + 5.0
    while len(onyx.requests_matching("POST", "/api/onyx-api/ingestion")) < 1:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    time.sleep(0.3)
    assert len(onyx.requests_matching("POST", "/api/onyx-api/ingestion")) == 1
    assert sleeps.calls == []


def test_transport_error_not_retried(sleeps: Sleeps) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    api = create_client(f"http://127.0.0.1:{port}", TOKEN, sleep=sleeps)
    try:
        with pytest.raises(OnyxTransportError):
            api.check_auth()
    finally:
        api.close()
    assert sleeps.calls == []


# -- invariant I9 ------------------------------------------------------------


def test_token_never_in_exceptions_or_repr(onyx: FakeOnyx, client: OnyxApi, sleeps: Sleeps) -> None:
    errors: list[BaseException] = []

    def capture(action: object) -> None:
        try:
            action()  # type: ignore[operator]
        except OnyxError as exc:
            errors.append(exc)
        else:
            pytest.fail("expected an OnyxError")

    # Auth failure.
    bad = create_client(onyx.url, TOKEN + "x", sleep=sleeps)
    capture(bad.check_auth)
    bad.close()
    # Retried status failure, other status failure, entry not found.
    onyx.fail_next("GET", "/api/me", 503, times=3)
    capture(client.check_auth)
    onyx.fail_next("GET", "/api/me", 422, body={"error_code": "BAD", "detail": "x"})
    capture(client.check_auth)
    capture(lambda: client.upsert_document(make_document(), 999))
    # Timeout.
    cc_pair_id = client.create_entry("e")
    onyx.delay_next("POST", "/api/onyx-api/ingestion", 0.6)
    capture(lambda: client.upsert_document(make_document(), cc_pair_id, timeout=0.2))
    # Transport.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    dead = create_client(f"http://127.0.0.1:{port}", TOKEN, sleep=sleeps)
    capture(dead.check_auth)
    dead.close()

    assert len(errors) == 6
    for exc in errors:
        assert TOKEN not in str(exc)
        assert TOKEN not in repr(exc)
        assert "Bearer" not in str(exc)
        assert exc.__cause__ is None
    for api in (client, bad, dead):
        assert TOKEN not in repr(api)
        assert TOKEN not in str(api)
