import pytest

from onyx_folder_sync import __version__
from onyx_folder_sync.cli import format_report, main, parse_interval
from onyx_folder_sync.interfaces import ConfigError
from onyx_folder_sync.models import ExitCode, RootReport, SkipReason, SyncReport


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "onyx-folder-sync" in out
    for command in ("sync", "extract-report", "status", "forget", "rename-root", "reconcile"):
        assert command in out


def test_unknown_command_is_a_usage_error() -> None:
    assert main(["no-such-command"]) == ExitCode.CONFIG_OR_STATE


def test_missing_config_is_exit_code_2(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONYX_FOLDER_SYNC_CONFIG", raising=False)
    assert main(["sync"]) == ExitCode.CONFIG_OR_STATE
    assert "--config" in capsys.readouterr().err


def test_parse_interval() -> None:
    assert parse_interval("90") == 90
    assert parse_interval("45s") == 45
    assert parse_interval("15m") == 900
    assert parse_interval("6h") == 21600
    assert parse_interval("1d") == 86400
    for bad in ("", "0", "h", "-5m", "1w"):
        with pytest.raises(ConfigError):
            parse_interval(bad)


def test_format_report() -> None:
    root = RootReport(name="docs", pushed=2, unchanged=3, failed=1, deleted=4)
    root.skipped = {SkipReason.EXTENSION: 5, SkipReason.EXCLUDED: 1}
    root.unknown_subtrees = ["a/b", ""]
    root.blocked_deletes = 12
    report = SyncReport(roots=[root], messages=["note"])
    lines = format_report(report)
    assert lines[0] == (
        "root 'docs': pushed 2, unchanged 3, failed 1, deleted 4, "
        "skipped 6 (excluded 1, extension 5)"
    )
    assert "  blocked deletes: 12" in lines
    assert "  unknown subtree: a/b" in lines
    assert "  unknown subtree: ." in lines
    assert lines[-1] == "note"

    root.would_push = ["x.txt"]
    root.would_delete = ["y.txt"]
    dry = format_report(report, dry_run=True)
    assert dry[0].startswith("root 'docs': would push 1, would delete 1")
    assert "  push   x.txt" in dry
    assert "  delete y.txt" in dry
