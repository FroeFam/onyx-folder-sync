"""Fake Onyx server (behaviour observed on Onyx v5.1.0, plan Appendix A).

Frozen contract: only the orchestrator changes this file.

Usage::

    with FakeOnyx() as onyx:
        client = create_client(onyx.url, onyx.token)
        ...
        assert onyx.documents["fc_..."].text == "hello"

It is a real HTTP server on 127.0.0.1 with a random port, so a CLI started as
a separate process can talk to it too.

Fault injection:

- ``fail_next(method, path_regex, status, ...)`` answers matching requests
  with the given status instead of handling them.
- ``delay_next(method, path_regex, seconds, ...)`` sleeps before handling; use
  a delay longer than the client timeout to produce a timeout.
- ``on_request`` hooks run before each request is handled (for example to
  kill a process at an exact point).
- ``set_prune_freq`` and ``run_prune`` are the "pruned" switch.
- ``drop_entry`` removes an entry and its documents, as an admin would.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any, Self
from urllib.parse import unquote

DEFAULT_PRUNE_FREQ = 604800  # what Onyx applies when prune_freq is omitted: 7 days


def rewrite_document_id(document_id: str) -> str:
    """Onyx percent-encodes ':' and '/' and turns spaces into underscores."""
    return document_id.replace(":", "%3A").replace("/", "%2F").replace(" ", "_")


@dataclass
class FakeDocument:
    document_id: str  # as stored, after rewriting
    cc_pair_id: int
    semantic_identifier: str
    title: str | None
    source: str
    text: str
    link: str | None
    metadata: dict[str, Any]
    doc_updated_at: str | None
    content_hash: str
    index_count: int = 1  # times Onyx chunked and embedded this document
    via_ingestion_api: bool = True


@dataclass
class FakeEntry:
    cc_pair_id: int
    connector_id: int
    credential_id: int
    name: str
    source: str
    prune_freq: int | None
    status: str = "SCHEDULED"
    access_type: str = "public"


@dataclass(frozen=True)
class RecordedRequest:
    """One handled request. Headers are deliberately not recorded."""

    method: str
    path: str
    body: Any
    status: int


@dataclass
class _Fault:
    method: str
    pattern: re.Pattern[str]
    times: int
    status: int = 0
    body: Any = None
    delay: float = 0.0


@dataclass
class _Response:
    status: int
    body: Any = field(default=None)


class FakeOnyx:
    def __init__(self, token: str = "fake-onyx-token") -> None:  # noqa: S107
        self.token = token
        # 401 for a wrong token; set to 403 to imitate a token without the
        # manage-connectors permission.
        self.auth_failure_status = 401
        self.documents: dict[str, FakeDocument] = {}
        self.entries: dict[int, FakeEntry] = {}
        self.requests: list[RecordedRequest] = []
        self.on_request: list[Callable[[str, str, Any], None]] = []
        self._faults: list[_Fault] = []
        self._lock = threading.RLock()
        # Onyx ids start above 1 on a real install; mirror the plan's example.
        self._next_connector_id = 4
        self._next_cc_pair_id = 5
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _run(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                auth = self.headers.get("Authorization")
                response = fake._dispatch(method, self.path, raw, auth)
                payload = b"" if response.body is None else json.dumps(response.body).encode()
                try:
                    self.send_response(response.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass  # the client gave up (timeout tests)

            def do_GET(self) -> None:
                self._run("GET")

            def do_POST(self) -> None:
                self._run("POST")

            def do_PUT(self) -> None:
                self._run("PUT")

            def do_DELETE(self) -> None:
                self._run("DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    @property
    def url(self) -> str:
        """Base URL to put in ``onyx.url`` (without the /api suffix)."""
        if self._server is None:
            raise RuntimeError("FakeOnyx is not started")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    # -- fault injection and admin-side actions ----------------------------

    def fail_next(
        self, method: str, path_regex: str, status: int, *, body: Any = None, times: int = 1
    ) -> None:
        """Answer the next ``times`` matching requests with ``status``.

        ``path_regex`` is searched in the request path (which starts with
        /api). A 409 defaults to the body ``{"error_code": "CONFLICT"}``.
        """
        if body is None and status == 409:
            body = {"error_code": "CONFLICT"}
        with self._lock:
            self._faults.append(
                _Fault(method.upper(), re.compile(path_regex), times, status=status, body=body)
            )

    def delay_next(self, method: str, path_regex: str, seconds: float, *, times: int = 1) -> None:
        """Sleep ``seconds`` before handling the next ``times`` matching requests."""
        with self._lock:
            self._faults.append(
                _Fault(method.upper(), re.compile(path_regex), times, delay=seconds)
            )

    def clear_faults(self) -> None:
        with self._lock:
            self._faults.clear()

    def set_prune_freq(self, cc_pair_id: int, prune_freq: int | None) -> None:
        """An admin switches pruning on (seconds) or off (None) for an entry."""
        with self._lock:
            self.entries[cc_pair_id].prune_freq = prune_freq

    def run_prune(self, cc_pair_id: int) -> int:
        """What an Onyx prune does to a pushed-only entry: delete every document."""
        with self._lock:
            doomed = [k for k, d in self.documents.items() if d.cc_pair_id == cc_pair_id]
            for key in doomed:
                del self.documents[key]
            return len(doomed)

    def drop_entry(self, cc_pair_id: int) -> None:
        """Remove an entry and its documents, as if deleted in the admin page."""
        with self._lock:
            self.entries.pop(cc_pair_id, None)
            self.run_prune(cc_pair_id)

    def add_foreign_document(self, document_id: str, cc_pair_id: int) -> None:
        """A document that did not come through the Ingestion API."""
        with self._lock:
            self.documents[document_id] = FakeDocument(
                document_id=document_id,
                cc_pair_id=cc_pair_id,
                semantic_identifier=document_id,
                title=None,
                source="web",
                text="foreign",
                link=None,
                metadata={},
                doc_updated_at=None,
                content_hash="",
                via_ingestion_api=False,
            )

    def documents_of(self, cc_pair_id: int) -> list[FakeDocument]:
        with self._lock:
            return [d for d in self.documents.values() if d.cc_pair_id == cc_pair_id]

    def requests_matching(self, method: str, path_regex: str) -> list[RecordedRequest]:
        pattern = re.compile(path_regex)
        with self._lock:
            return [
                r for r in self.requests if r.method == method.upper() and pattern.search(r.path)
            ]

    # -- request handling --------------------------------------------------

    def _dispatch(self, method: str, path: str, raw: bytes, auth: str | None) -> _Response:
        try:
            body: Any = json.loads(raw) if raw else None
        except ValueError:
            body = None
        for hook in list(self.on_request):
            hook(method, path, body)
        fault = self._take_fault(method, path)
        if fault is not None and fault.delay:
            time.sleep(fault.delay)
        with self._lock:
            if fault is not None and fault.status:
                response = _Response(fault.status, fault.body)
            elif auth != f"Bearer {self.token}":
                response = _Response(self.auth_failure_status, {"detail": "Access denied"})
            else:
                response = self._route(method, path.split("?", 1)[0], body)
            self.requests.append(RecordedRequest(method, path, body, response.status))
        return response

    def _take_fault(self, method: str, path: str) -> _Fault | None:
        with self._lock:
            for fault in self._faults:
                if fault.method == method and fault.pattern.search(path):
                    fault.times -= 1
                    if fault.times <= 0:
                        self._faults.remove(fault)
                    return fault
        return None

    def _route(self, method: str, path: str, body: Any) -> _Response:
        if method == "GET" and path == "/api/me":
            return _Response(200, {"id": "user-1", "email": "admin@example.com", "role": "admin"})
        if method == "POST" and path == "/api/manage/admin/connector-with-mock-credential":
            return self._create_entry(body)
        m = re.fullmatch(r"/api/manage/admin/cc-pair/(\d+)", path)
        if method == "GET" and m:
            return self._read_entry(int(m.group(1)))
        m = re.fullmatch(r"/api/manage/admin/cc-pair/(\d+)/status", path)
        if method == "PUT" and m:
            return self._set_status(int(m.group(1)), body)
        if method == "POST" and path == "/api/manage/admin/deletion-attempt":
            return self._deletion_attempt(body)
        if method == "POST" and path == "/api/onyx-api/ingestion":
            return self._upsert(body)
        m = re.fullmatch(r"/api/onyx-api/connector-docs/(\d+)", path)
        if method == "GET" and m:
            return self._list_docs(int(m.group(1)))
        m = re.fullmatch(r"/api/onyx-api/ingestion/(.+)", path)
        if method == "DELETE" and m:
            return self._delete_doc(unquote(m.group(1)))
        return _Response(404, {"detail": "Not Found"})

    def _create_entry(self, body: Any) -> _Response:
        if not isinstance(body, dict) or not body.get("name") or not body.get("source"):
            return _Response(422, {"detail": "invalid connector"})
        connector_id = self._next_connector_id
        self._next_connector_id += 1
        cc_pair_id = self._next_cc_pair_id
        self._next_cc_pair_id += 1
        # An omitted prune_freq gets the 7 day default; explicit null stays null.
        prune_freq = body.get("prune_freq", DEFAULT_PRUNE_FREQ)
        self.entries[cc_pair_id] = FakeEntry(
            cc_pair_id=cc_pair_id,
            connector_id=connector_id,
            credential_id=connector_id,
            name=body["name"],
            source=body["source"],
            prune_freq=prune_freq,
            access_type=body.get("access_type", "public"),
        )
        message = (
            f"Creating new association between Connector {connector_id} "
            f"and Credential {connector_id}"
        )
        return _Response(200, {"success": True, "message": message, "data": cc_pair_id})

    def _read_entry(self, cc_pair_id: int) -> _Response:
        entry = self.entries.get(cc_pair_id)
        if entry is None:
            return _Response(404, {"error_code": "NOT_FOUND"})
        return _Response(
            200,
            {
                "id": entry.cc_pair_id,
                "name": entry.name,
                "status": entry.status,
                "num_docs_indexed": len(self.documents_of(cc_pair_id)),
                "access_type": entry.access_type,
                "connector": {
                    "id": entry.connector_id,
                    "name": entry.name,
                    "source": entry.source,
                    "input_type": "load_state",
                    "connector_specific_config": {},
                    "refresh_freq": None,
                    "prune_freq": entry.prune_freq,
                },
                "credential": {"id": entry.credential_id, "source": entry.source},
            },
        )

    def _set_status(self, cc_pair_id: int, body: Any) -> _Response:
        entry = self.entries.get(cc_pair_id)
        if entry is None:
            return _Response(404, {"error_code": "NOT_FOUND"})
        if not isinstance(body, dict) or body.get("status") not in {"PAUSED", "ACTIVE"}:
            return _Response(422, {"detail": "invalid status"})
        entry.status = body["status"]
        return _Response(200, {"success": True})

    def _deletion_attempt(self, body: Any) -> _Response:
        if not isinstance(body, dict):
            return _Response(422, {"detail": "invalid body"})
        for entry in list(self.entries.values()):
            if entry.connector_id == body.get("connector_id") and entry.credential_id == body.get(
                "credential_id"
            ):
                if entry.status != "PAUSED":
                    return _Response(400, {"detail": "Connector must be paused before deletion"})
                self.drop_entry(entry.cc_pair_id)
                return _Response(200, None)
        return _Response(404, {"error_code": "NOT_FOUND"})

    def _upsert(self, body: Any) -> _Response:
        if not isinstance(body, dict) or not isinstance(body.get("document"), dict):
            return _Response(422, {"detail": "document required"})
        doc = body["document"]
        sections = doc.get("sections")
        if not doc.get("id") or not doc.get("semantic_identifier") or not sections:
            return _Response(422, {"detail": "invalid document"})
        for section in sections:
            # Text sections only: tabular sections and file_id are rejected.
            if (
                not isinstance(section, dict)
                or not isinstance(section.get("text"), str)
                or set(section) - {"text", "link"}
            ):
                return _Response(400, {"error_code": "INVALID_INPUT"})
        cc_pair_id = body.get("cc_pair_id")
        if cc_pair_id is None or cc_pair_id not in self.entries:
            return _Response(404, {"error_code": "CONNECTOR_NOT_FOUND"})
        stored_id = rewrite_document_id(str(doc["id"]))
        text = "\n".join(s["text"] for s in sections)
        hash_input = json.dumps(
            [
                doc.get("semantic_identifier"),
                doc.get("title"),
                doc.get("source"),
                sections,
                doc.get("metadata"),
            ],
            sort_keys=True,
        )
        content_hash = hashlib.sha256(hash_input.encode()).hexdigest()
        existing = self.documents.get(stored_id)
        already_existed = existing is not None
        if existing is not None and existing.content_hash == content_hash:
            # Unchanged re-push: Onyx skips indexing by content hash.
            existing.doc_updated_at = doc.get("doc_updated_at")
        else:
            # The timestamp gate is off: changed text is re-indexed even with
            # an older doc_updated_at.
            self.documents[stored_id] = FakeDocument(
                document_id=stored_id,
                cc_pair_id=cc_pair_id,
                semantic_identifier=doc["semantic_identifier"],
                title=doc.get("title"),
                source=doc.get("source") or "ingestion_api",
                text=text,
                link=sections[0].get("link"),
                metadata=doc.get("metadata") or {},
                doc_updated_at=doc.get("doc_updated_at"),
                content_hash=content_hash,
                index_count=(existing.index_count + 1) if existing else 1,
            )
        return _Response(200, {"document_id": stored_id, "already_existed": already_existed})

    def _list_docs(self, cc_pair_id: int) -> _Response:
        if cc_pair_id not in self.entries:
            return _Response(404, {"error_code": "NOT_FOUND"})
        return _Response(
            200,
            [
                {"document_id": d.document_id, "semantic_id": d.semantic_identifier, "link": d.link}
                for d in self.documents_of(cc_pair_id)
            ],
        )

    def _delete_doc(self, document_id: str) -> _Response:
        doc = self.documents.get(document_id)
        if doc is None:
            return _Response(404, {"error_code": "DOCUMENT_NOT_FOUND"})
        if not doc.via_ingestion_api:
            return _Response(400, {"error_code": "INVALID_INPUT"})
        del self.documents[document_id]
        return _Response(200, None)
