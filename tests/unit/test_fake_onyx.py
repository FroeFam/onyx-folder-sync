"""The fake server reproduces every response of plan Appendix A."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from onyx_folder_sync.guard import evaluate_guard
from onyx_folder_sync.ids import document_id
from tests.fake_onyx import FakeOnyx
from tests.fixtures import NFC_NAME, NFD_NAME, TreeBuilder, failing_scandir

ENTRY: dict[str, Any] = {
    "name": "folder: docs [abc]",
    "source": "ingestion_api",
    "input_type": "load_state",
    "connector_specific_config": {},
    "refresh_freq": None,
    "prune_freq": None,
    "access_type": "public",
    "groups": [],
}


def doc(doc_id: str = "fc_0123456789abcdef0123456789abcdef", text: str = "hello") -> dict[str, Any]:
    return {
        "id": doc_id,
        "semantic_identifier": "report.pdf",
        "title": "report.pdf",
        "source": "file",
        "sections": [{"text": text, "link": "file:///data/a/report.pdf"}],
        "metadata": {"root": "docs", "path": "a/report.pdf", "extension": "pdf"},
        "doc_updated_at": "2026-01-01T00:00:00Z",
    }


@pytest.fixture
def onyx() -> Iterator[FakeOnyx]:
    with FakeOnyx() as fake:
        yield fake


@pytest.fixture
def http(onyx: FakeOnyx) -> Iterator[httpx.Client]:
    headers = {"Authorization": f"Bearer {onyx.token}"}
    with httpx.Client(base_url=f"{onyx.url}/api", headers=headers) as client:
        yield client


def create_entry(http: httpx.Client, **overrides: Any) -> int:
    response = http.post("/manage/admin/connector-with-mock-credential", json=ENTRY | overrides)
    assert response.status_code == 200
    return int(response.json()["data"])


def test_auth_check(onyx: FakeOnyx, http: httpx.Client) -> None:
    assert http.get("/me").status_code == 200
    assert httpx.get(f"{onyx.url}/api/me").status_code == 401
    onyx.auth_failure_status = 403
    bad = httpx.get(f"{onyx.url}/api/me", headers={"Authorization": "Bearer wrong"})
    assert bad.status_code == 403


def test_create_and_read_entry(http: httpx.Client) -> None:
    response = http.post("/manage/admin/connector-with-mock-credential", json=ENTRY)
    body = response.json()
    assert body == {
        "success": True,
        "message": "Creating new association between Connector 4 and Credential 4",
        "data": 5,
    }
    entry = http.get("/manage/admin/cc-pair/5").json()
    assert entry["name"] == ENTRY["name"]
    assert entry["status"] == "SCHEDULED"
    assert entry["num_docs_indexed"] == 0
    assert entry["connector"] == entry["connector"] | {
        "id": 4,
        "source": "ingestion_api",
        "prune_freq": None,
    }
    assert entry["credential"]["id"] == 4
    missing = http.get("/manage/admin/cc-pair/99")
    assert missing.status_code == 404
    assert missing.json() == {"error_code": "NOT_FOUND"}


def test_omitted_prune_freq_defaults_to_seven_days(http: httpx.Client) -> None:
    body = {k: v for k, v in ENTRY.items() if k != "prune_freq"}
    cc_pair = int(
        http.post("/manage/admin/connector-with-mock-credential", json=body).json()["data"]
    )
    assert http.get(f"/manage/admin/cc-pair/{cc_pair}").json()["connector"]["prune_freq"] == 604800


def test_upsert_list_delete(onyx: FakeOnyx, http: httpx.Client) -> None:
    cc_pair = create_entry(http)
    first = http.post("/onyx-api/ingestion", json={"document": doc(), "cc_pair_id": cc_pair})
    assert first.json() == {"document_id": doc()["id"], "already_existed": False}
    assert http.get(f"/manage/admin/cc-pair/{cc_pair}").json()["num_docs_indexed"] == 1
    listed = http.get(f"/onyx-api/connector-docs/{cc_pair}").json()
    assert listed == [
        {
            "document_id": doc()["id"],
            "semantic_id": "report.pdf",
            "link": "file:///data/a/report.pdf",
        }
    ]
    assert http.delete(f"/onyx-api/ingestion/{doc()['id']}").status_code == 200
    again = http.delete(f"/onyx-api/ingestion/{doc()['id']}")
    assert again.status_code == 404
    assert again.json() == {"error_code": "DOCUMENT_NOT_FOUND"}
    assert onyx.documents == {}


def test_unchanged_repush_is_not_reindexed_and_changed_text_is(
    onyx: FakeOnyx, http: httpx.Client
) -> None:
    cc_pair = create_entry(http)
    http.post("/onyx-api/ingestion", json={"document": doc(), "cc_pair_id": cc_pair})
    second = http.post("/onyx-api/ingestion", json={"document": doc(), "cc_pair_id": cc_pair})
    assert second.json()["already_existed"] is True
    assert onyx.documents[doc()["id"]].index_count == 1
    older = doc(text="changed") | {"doc_updated_at": "2020-01-01T00:00:00Z"}
    http.post("/onyx-api/ingestion", json={"document": older, "cc_pair_id": cc_pair})
    assert onyx.documents[doc()["id"]].index_count == 2
    assert onyx.documents[doc()["id"]].text == "changed"


def test_document_ids_are_rewritten(onyx: FakeOnyx, http: httpx.Client) -> None:
    cc_pair = create_entry(http)
    response = http.post(
        "/onyx-api/ingestion", json={"document": doc("a:b/c d"), "cc_pair_id": cc_pair}
    )
    assert response.json()["document_id"] == "a%3Ab%2Fc_d"
    assert "a%3Ab%2Fc_d" in onyx.documents


def test_rejected_payloads(http: httpx.Client) -> None:
    cc_pair = create_entry(http)
    unknown = http.post("/onyx-api/ingestion", json={"document": doc(), "cc_pair_id": 99})
    assert unknown.json()["error_code"] == "CONNECTOR_NOT_FOUND"
    with_file = doc() | {"sections": [{"file_id": "x"}]}
    assert (
        http.post(
            "/onyx-api/ingestion", json={"document": with_file, "cc_pair_id": cc_pair}
        ).status_code
        == 400
    )
    tabular = doc() | {"sections": [{"text": "a", "rows": [[1]]}]}
    assert (
        http.post(
            "/onyx-api/ingestion", json={"document": tabular, "cc_pair_id": cc_pair}
        ).status_code
        == 400
    )


def test_fault_injection_and_request_log(onyx: FakeOnyx, http: httpx.Client) -> None:
    cc_pair = create_entry(http)
    http.post("/onyx-api/ingestion", json={"document": doc(), "cc_pair_id": cc_pair})
    onyx.fail_next("DELETE", r"/onyx-api/ingestion/", 409)
    conflict = http.delete(f"/onyx-api/ingestion/{doc()['id']}")
    assert conflict.status_code == 409
    assert conflict.json() == {"error_code": "CONFLICT"}
    assert http.delete(f"/onyx-api/ingestion/{doc()['id']}").status_code == 200
    onyx.fail_next("GET", r"/me$", 503, times=2)
    assert [http.get("/me").status_code for _ in range(3)] == [503, 503, 200]
    onyx.delay_next("GET", r"/me$", 0.5)
    with pytest.raises(httpx.TimeoutException):
        http.get("/me", timeout=0.1)
    assert len(onyx.requests_matching("DELETE", r"/onyx-api/ingestion/")) == 2
    seen: list[str] = []
    onyx.on_request.append(lambda method, path, body: seen.append(f"{method} {path}"))
    http.get("/me")
    assert seen == ["GET /api/me"]


def test_prune_switch_and_teardown(onyx: FakeOnyx, http: httpx.Client) -> None:
    cc_pair = create_entry(http)
    http.post("/onyx-api/ingestion", json={"document": doc(), "cc_pair_id": cc_pair})
    onyx.set_prune_freq(cc_pair, 86400)
    assert http.get(f"/manage/admin/cc-pair/{cc_pair}").json()["connector"]["prune_freq"] == 86400
    assert onyx.run_prune(cc_pair) == 1
    assert onyx.documents == {}

    http.post("/onyx-api/ingestion", json={"document": doc(), "cc_pair_id": cc_pair})
    attempt = {"connector_id": 4, "credential_id": 4}
    assert http.post("/manage/admin/deletion-attempt", json=attempt).status_code == 400
    paused = http.put(f"/manage/admin/cc-pair/{cc_pair}/status", json={"status": "PAUSED"})
    assert paused.status_code == 200
    assert http.post("/manage/admin/deletion-attempt", json=attempt).status_code == 200
    assert onyx.entries == {}
    assert onyx.documents == {}


def test_foreign_documents_cannot_be_deleted(onyx: FakeOnyx, http: httpx.Client) -> None:
    cc_pair = create_entry(http)
    onyx.add_foreign_document("https://example.com/page", cc_pair)
    assert http.delete("/onyx-api/ingestion/https%3A%2F%2Fexample.com%2Fpage").status_code == 400
    assert len(onyx.documents) == 1


def test_tree_builder(tmp_path: Path) -> None:
    tree = TreeBuilder(tmp_path / "root")
    tree.file("a/b.txt", "x")
    tree.file(NFD_NAME, "unicode")
    assert len(tree.files(3, folder="many")) == 3
    assert tree.path("a/b.txt").stat().st_mtime < __import__("time").time() - 3000
    assert NFC_NAME != NFD_NAME
    scandir = failing_scandir(tree.path("a"))
    with pytest.raises(PermissionError):
        scandir(str(tree.path("a")))
    with scandir(str(tree.root)) as entries:
        assert len(list(entries)) == 3
    tree.directory("locked")
    tree.file("locked/secret.txt")
    if tree.make_unreadable("locked"):
        with pytest.raises(OSError):
            list(tree.path("locked").iterdir())
        tree.restore()
    assert tree.path("locked/secret.txt").read_text(encoding="utf-8") == "hello"


def test_document_id_and_guard() -> None:
    first = document_id("root-1", "a/b.txt")
    assert first.startswith("fc_")
    assert len(first) == 35
    assert first == document_id("root-1", "a\\b.txt")
    assert document_id("root-1", NFD_NAME) == document_id("root-1", NFC_NAME)
    assert first != document_id("root-2", "a/b.txt")
    assert evaluate_guard(10, 10, max_delete_fraction=0.25)
    assert not evaluate_guard(11, 20, max_delete_fraction=0.25)
    assert evaluate_guard(11, 44, max_delete_fraction=0.25)
    assert not evaluate_guard(11, 0, max_delete_fraction=0.25)
