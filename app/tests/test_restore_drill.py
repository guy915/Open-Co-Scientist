import runpy
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

_DRILL = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "scripts/operations/restore_drill.py")
)
isolated_environment = cast(Callable[[Path, Path], dict[str, str]], _DRILL["isolated_environment"])
verify = cast(Callable[[Path], None], _DRILL["verify"])


def test_restore_drill_drops_credentials_and_does_not_load_repository_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "OPENROUTER_API_KEY",
        "AZURE_API_KEY",
        "SENTRY_DSN",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "SMTP_HOST",
        "HTTPS_PROXY",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(name, "must-not-reach-the-drill")
    environment = isolated_environment(tmp_path, tmp_path / "restored.db")
    assert environment["COSCIENTIST_TEST_DOUBLE"] == "deterministic"
    assert environment["OTEL_SDK_DISABLED"] == "true"
    assert environment["COSCIENTIST_LITESTREAM_ACTIVE"] == "1"
    assert environment["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert environment["PYTHON_DOTENV_DISABLED"] == "1"
    assert "must-not-reach-the-drill" not in environment.values()


def test_restore_drill_blocks_module_adjacent_dotenv(tmp_path: Path) -> None:
    module = tmp_path / "module"
    module.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (module / ".env").write_text("RESTORE_DRILL_DUMMY_SECRET=fixture-only\n")
    probe = module / "probe.py"
    probe.write_text(
        "from dotenv import load_dotenv\n"
        "import os\n"
        "load_dotenv()\n"
        "print('RESTORE_DRILL_DUMMY_SECRET' in os.environ)\n"
    )
    environment = isolated_environment(scratch, scratch / "restored.db")

    def loaded(child_environment: dict[str, str]) -> str:
        return subprocess.run(
            [sys.executable, str(probe)],
            cwd=scratch,
            env=child_environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()

    assert loaded(environment) == "False"
    # The negative control proves the dummy file is discoverable despite cwd.
    environment.pop("PYTHON_DOTENV_DISABLED")
    assert loaded(environment) == "True"


def test_restore_verification_requires_the_original_marker(tmp_path: Path) -> None:
    database = tmp_path / "restored.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE restore_drill_marker (value TEXT)")
        connection.execute("INSERT INTO restore_drill_marker VALUES ('synthetic-only')")
    verify(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM restore_drill_marker")
    with pytest.raises(AssertionError):
        verify(database)


def test_restore_verification_never_creates_a_missing_database(tmp_path: Path) -> None:
    database = tmp_path / "absent.db"
    with pytest.raises(sqlite3.OperationalError):
        verify(database)
    assert not database.exists()
