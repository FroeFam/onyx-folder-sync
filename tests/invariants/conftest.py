"""Fixtures for the invariant suite."""

from __future__ import annotations

import ssl
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fake_onyx import FakeOnyx
from tests.invariants.harness import Env, reset_fake


@pytest.fixture(scope="session", autouse=True)
def _fast_tls_context() -> Iterator[None]:
    """httpx loads the CA bundle (about 0.25 s) for every client, but the fake is plain HTTP."""
    patch = pytest.MonkeyPatch()
    patch.setattr(ssl.SSLContext, "load_verify_locations", lambda self, *a, **k: None)
    yield
    patch.undo()


@pytest.fixture(scope="session")
def _shared_fake() -> Iterator[FakeOnyx]:
    """One server for the whole session: stopping it costs half a second per test."""
    with FakeOnyx() as server:
        yield server


@pytest.fixture
def fake(_shared_fake: FakeOnyx) -> FakeOnyx:
    reset_fake(_shared_fake)
    return _shared_fake


@pytest.fixture
def env(tmp_path: Path, fake: FakeOnyx) -> Iterator[Env]:
    environment = Env(tmp_path, fake)
    try:
        yield environment
    finally:
        environment.tree.restore()
