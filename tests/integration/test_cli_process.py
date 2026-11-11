import subprocess
import sys


def test_module_runs_as_a_process() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "onyx_folder_sync", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "onyx-folder-sync" in result.stdout
