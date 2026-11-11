"""HTTP client for the Onyx API (plan Appendix A, failure rules of 5.5).

Invariant I9: the token and request headers never appear in exception
messages or in repr(); this module does not log.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from onyx_folder_sync.interfaces import (
    OnyxApi,
    OnyxAuthError,
    OnyxEntryNotFoundError,
    OnyxRequestError,
    OnyxTimeoutError,
    OnyxTransportError,
)
from onyx_folder_sync.models import EntryInfo, OnyxDocument

_ATTEMPTS = 3
_BACKOFF_SECONDS = (2.0, 4.0)
_READ_TIMEOUT = 120.0
_CONNECT_TIMEOUT = 10.0
_MAX_MESSAGE_CHARS = 200
_NOT_FOUND_CODE = "CONNECTOR_NOT_FOUND"


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _error_code(payload: Any) -> str | None:
    if isinstance(payload, dict):
        code = payload.get("error_code")
        if isinstance(code, str):
            return code
    return None


class _OnyxClient:
    def __init__(self, base_url: str, token: str, sleep: Callable[[float], None]) -> None:
        self._base_url = base_url.rstrip("/") + "/api"
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT),
        )

    def __repr__(self) -> str:
        return f"OnyxClient(base_url={self._base_url!r})"

    # -- transport -----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        timeout: float | None = None,
        accept_404: bool = False,
    ) -> httpx.Response:
        kwargs: dict[str, Any] = {}
        if json is not None:
            kwargs["json"] = json
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout, connect=_CONNECT_TIMEOUT)
        for attempt in range(_ATTEMPTS):
            try:
                response = self._http.request(method, path, **kwargs)
            except httpx.TimeoutException:
                raise OnyxTimeoutError(f"{method} request timed out") from None
            except httpx.TransportError:
                raise OnyxTransportError(f"{method} request failed without a response") from None
            status = response.status_code
            if 200 <= status < 300:
                return response
            if status in (401, 403):
                raise OnyxAuthError(f"HTTP {status}: authentication failed")
            if status == 404 and accept_404:
                return response
            if (status == 409 or status >= 500) and attempt < _ATTEMPTS - 1:
                self._sleep(_BACKOFF_SECONDS[attempt])
                continue
            raise self._error(response)
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _error(response: httpx.Response) -> OnyxRequestError:
        code = _error_code(_json_or_none(response))
        message = response.text[:_MAX_MESSAGE_CHARS]
        if code == _NOT_FOUND_CODE:
            return OnyxEntryNotFoundError(response.status_code, code, message)
        return OnyxRequestError(response.status_code, code, message)

    # -- OnyxApi ---------------------------------------------------------------

    def check_auth(self) -> None:
        self._request("GET", "/me")

    def create_entry(self, name: str) -> int:
        body: dict[str, Any] = {
            "name": name,
            "source": "ingestion_api",
            "input_type": "load_state",
            "connector_specific_config": {},
            "refresh_freq": None,
            "prune_freq": None,
            "access_type": "public",
            "groups": [],
        }
        payload = _json_or_none(
            self._request("POST", "/manage/admin/connector-with-mock-credential", json=body)
        )
        try:
            return int(payload["data"])
        except (TypeError, KeyError, ValueError):
            raise OnyxRequestError(200, None, "unexpected create_entry response") from None

    def get_entry(self, cc_pair_id: int) -> EntryInfo | None:
        response = self._request("GET", f"/manage/admin/cc-pair/{cc_pair_id}", accept_404=True)
        if response.status_code == 404:
            return None
        payload = _json_or_none(response)
        try:
            return EntryInfo(
                cc_pair_id=cc_pair_id,
                name=str(payload["name"]),
                status=str(payload["status"]),
                num_docs_indexed=int(payload["num_docs_indexed"]),
                source=str(payload["connector"]["source"]),
                prune_freq=payload["connector"]["prune_freq"],
                connector_id=int(payload["connector"]["id"]),
                credential_id=int(payload["credential"]["id"]),
            )
        except (TypeError, KeyError, ValueError):
            raise OnyxRequestError(200, None, "unexpected get_entry response") from None

    def upsert_document(
        self, document: OnyxDocument, cc_pair_id: int, *, timeout: float | None = None
    ) -> None:
        body = {"document": document.model_dump(exclude_none=True), "cc_pair_id": cc_pair_id}
        response = self._request("POST", "/onyx-api/ingestion", json=body, timeout=timeout)
        if _error_code(_json_or_none(response)) == _NOT_FOUND_CODE:
            raise OnyxEntryNotFoundError(response.status_code, _NOT_FOUND_CODE, "")

    def list_document_ids(self, cc_pair_id: int) -> list[str]:
        payload = _json_or_none(self._request("GET", f"/onyx-api/connector-docs/{cc_pair_id}"))
        try:
            return [str(item["document_id"]) for item in payload]
        except (TypeError, KeyError):
            raise OnyxRequestError(200, None, "unexpected list_document_ids response") from None

    def delete_document(self, document_id: str) -> None:
        self._request(
            "DELETE", f"/onyx-api/ingestion/{quote(document_id, safe='')}", accept_404=True
        )

    def remove_entry(self, cc_pair_id: int, connector_id: int, credential_id: int) -> None:
        self._request(
            "PUT", f"/manage/admin/cc-pair/{cc_pair_id}/status", json={"status": "PAUSED"}
        )
        self._request(
            "POST",
            "/manage/admin/deletion-attempt",
            json={"connector_id": connector_id, "credential_id": credential_id},
        )

    def close(self) -> None:
        self._http.close()


def create_client(
    base_url: str, token: str, *, sleep: Callable[[float], None] = time.sleep
) -> OnyxApi:
    return _OnyxClient(base_url, token, sleep)
