"""I9: the token never appears in logs, reports, state, CLI output or error messages."""

from __future__ import annotations

import os
import subprocess
import sys
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from onyx_folder_sync.interfaces import OnyxError
from onyx_folder_sync.models import ExitCode
from onyx_folder_sync.onyx_client import create_client
from tests.fake_onyx import FakeOnyx
from tests.fixtures import write_config
from tests.invariants.harness import Env, dead_url, state_bytes

TOKEN = "tok-9f3aC0ffeeS3CR3T-77"  # noqa: S105
BAD_TOKEN = "tok-WRONG-bad1deaS3CR3T-55"  # noqa: S105


@pytest.fixture
def secret_env(tmp_path: Path) -> Iterator[Env]:
    with FakeOnyx(token=TOKEN) as server:
        yield Env(tmp_path, server)


def _assert_clean(env: Env, *secrets: str, extra: str = "") -> None:
    haystack_text = "\n".join(env.log_lines) + "\n" + extra
    blob = state_bytes(env.state_path) if env.state_path.exists() else b""
    for secret in secrets:
        assert secret not in haystack_text, "token found in logs, report or messages"
        assert secret.encode() not in blob, "token found in the state database or its wal"


def _report_text(report: object) -> str:
    messages = getattr(report, "messages", [])
    return repr(report) + "\n" + "\n".join(messages)


def test_i9_token_is_absent_from_a_normal_run(secret_env: Env) -> None:
    env = secret_env
    env.tree.files(4)
    report = env.run()
    assert report.exit_code == ExitCode.OK
    env.tree.remove("doc0000.txt")
    report2 = env.run()
    _assert_clean(env, TOKEN, extra=_report_text(report) + _report_text(report2))


def test_i9_token_is_absent_after_an_authentication_failure(secret_env: Env) -> None:
    env = secret_env
    env.tree.files(3)
    env.run()
    report = env.run(token=BAD_TOKEN)
    assert report.exit_code == ExitCode.AUTH_FAILURE
    _assert_clean(env, TOKEN, BAD_TOKEN, extra=_report_text(report))


def test_i9_token_is_absent_after_a_mid_run_authorization_failure(secret_env: Env) -> None:
    env = secret_env
    env.tree.files(3)
    env.fake.fail_next("POST", "/onyx-api/ingestion$", 403)
    report = env.run()
    assert report.exit_code == ExitCode.AUTH_FAILURE
    _assert_clean(env, TOKEN, extra=_report_text(report))


def test_i9_token_is_absent_after_server_errors_and_conflicts(secret_env: Env) -> None:
    env = secret_env
    env.tree.files(4)
    env.fake.fail_next("POST", "/onyx-api/ingestion$", 500, times=3)
    env.fake.fail_next("POST", "/onyx-api/ingestion$", 409, times=1)
    report = env.run()
    report2 = env.run()
    _assert_clean(env, TOKEN, extra=_report_text(report) + _report_text(report2))


def test_i9_token_is_absent_when_onyx_is_unreachable(secret_env: Env) -> None:
    env = secret_env
    env.tree.files(2)
    report = env.run(url=dead_url())
    assert report.exit_code == ExitCode.OK  # unreachable at preflight is not a failure
    _assert_clean(env, TOKEN, extra=_report_text(report))


def test_i9_token_is_absent_from_exception_text(secret_env: Env) -> None:
    env = secret_env
    fake = env.fake
    texts: list[str] = []

    def capture(action: Callable[[], object]) -> None:
        try:
            action()
        except OnyxError as exc:
            texts.append(str(exc) + "\n" + repr(exc))
            texts.append("".join(traceback.format_exception(exc)))
        else:
            pytest.fail("the call was expected to fail")

    bad = create_client(fake.url, BAD_TOKEN, sleep=lambda s: None)
    capture(bad.check_auth)
    bad.close()

    good = create_client(fake.url, TOKEN, sleep=lambda s: None)
    fake.fail_next("GET", "/api/me", 500, times=3)
    capture(good.check_auth)
    fake.fail_next("DELETE", "/onyx-api/ingestion/", 500, times=3)
    capture(lambda: good.delete_document("fc_" + "0" * 32 + "x/y z"))
    capture(lambda: good.list_document_ids(999_999))
    good.close()

    gone = create_client(dead_url(), TOKEN, sleep=lambda s: None)
    capture(gone.check_auth)
    gone.close()

    joined = "\n".join(texts)
    assert texts
    assert TOKEN not in joined
    assert BAD_TOKEN not in joined


def _cli(env: Env, config: Path, token_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("ONYX_")}
    return subprocess.run(
        [sys.executable, "-m", "onyx_folder_sync", "--config", str(config), "sync"],
        env={**base, **token_env},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_i9_token_is_absent_from_cli_output_and_state(secret_env: Env) -> None:
    env = secret_env
    env.tree.files(3)
    config = write_config(
        env.tmp_path / "config.yaml",
        onyx_url=env.fake.url,
        state_path=env.state_path,
        roots={"main": env.root},
    )
    good = _cli(env, config, {"ONYX_TOKEN": TOKEN})
    assert good.returncode == ExitCode.OK, good.stderr
    bad = _cli(env, config, {"ONYX_TOKEN": BAD_TOKEN})
    assert bad.returncode == ExitCode.AUTH_FAILURE
    token_file = env.tmp_path / "token.txt"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    from_file = _cli(env, config, {"ONYX_TOKEN_FILE": str(token_file)})
    assert from_file.returncode == ExitCode.OK, from_file.stderr
    bad_file = env.tmp_path / "bad-token.txt"
    bad_file.write_text(BAD_TOKEN, encoding="utf-8")
    from_bad_file = _cli(env, config, {"ONYX_TOKEN_FILE": str(bad_file)})
    assert from_bad_file.returncode == ExitCode.AUTH_FAILURE
    for proc in (good, bad, from_file, from_bad_file):
        for secret in (TOKEN, BAD_TOKEN):
            assert secret not in proc.stdout
            assert secret not in proc.stderr
    _assert_clean(env, TOKEN, BAD_TOKEN)
