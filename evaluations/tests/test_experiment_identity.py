from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from evaluations import _artifacts, _run_driver
from evaluations._identity import identity_digest, validate_identity

_ROOT = Path(__file__).resolve().parents[2]


def _persist(
    tmp_path: Path,
    goal: str = "Public goal",
    tier: str = "express",
    overrides: dict[str, Any] | None = None,
) -> tuple[str, str]:
    db = str(tmp_path / "arms.db")
    _run_driver.configure_environment(db, live=False)
    invocation = _run_driver.ArmInvocation("comparison-test", "offline", db)
    run_id = _run_driver.persist_arm_run(goal, tier, overrides or {}, invocation)
    return run_id, db


def _identity(run_id: str, db: str) -> dict[str, Any]:
    from co_scientist.platform.db import runs

    run = runs.get_run(run_id, db_path=db)
    assert run is not None
    identity: dict[str, Any] = run.config["evaluation_identity"]
    return identity


def _rewrite_config(run_id: str, config: dict[str, Any], db_path: str) -> None:
    from co_scientist.platform import db

    with db.connect(db_path) as conn:
        conn.execute(
            "UPDATE runs SET config_json=? WHERE id=?",
            (json.dumps(config), run_id),
        )


def test_identity_helpers_work_without_execution_imports() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
sys.modules["app"] = None
sys.modules["co_scientist"] = None
from evaluations._identity import identity_digest, validate_identity
fields = {"version": 1, "dataset": {"name": "ordered", "items": []}}
sealed = {**fields, "digest": identity_digest(fields)}
assert validate_identity(sealed) is sealed
assert "evaluations._run_driver" not in sys.modules
assert not any(name.startswith("co_scientist.") for name in sys.modules)
""",
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_digest_is_order_independent_and_detects_tampering() -> None:
    items = [1, 2]
    fields = {"version": 1, "dataset": {"name": "β", "items": items}}
    reordered = {"dataset": {"items": [1, 2], "name": "β"}, "version": 1}
    assert identity_digest(fields) == identity_digest(reordered)
    sealed = {**fields, "digest": identity_digest(fields)}
    assert validate_identity(sealed) is sealed
    items.append(3)
    with pytest.raises(ValueError, match="does not match its contents"):
        validate_identity(sealed)
    with pytest.raises(ValueError, match="JSON compliant"):
        identity_digest({"version": 1, "value": float("nan")})


def test_persisted_arm_freezes_inputs_and_model_policy(tmp_path: Path) -> None:
    identity = _identity(*_persist(tmp_path))
    assert _identity(*_persist(tmp_path)) == identity
    other = _identity(*_persist(tmp_path, "Other goal"))
    assert other["digest"] != identity["digest"]
    assert identity["backend"] == "offline"
    assert identity["configured_models"]["worker"]
    engine = _ROOT / "engine" / "src" / "co_scientist"
    assert identity["request_policy_files"]
    for name, digest in identity["request_policy_files"].items():
        source = (engine / name).read_bytes()
        assert digest == hashlib.sha256(source).hexdigest()
    assert "API_KEY" not in str(identity)


def test_model_and_fallback_changes_change_persisted_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import co_scientist.platform.llm.profile as routes
    from co_scientist.core.config import settings

    monkeypatch.setattr(settings, "model_name", "openrouter/minimax/minimax-m3:free")
    original = _identity(*_persist(tmp_path))
    model = settings.model_name
    declared = routes.ROUTES[model]
    monkeypatch.setitem(routes.ROUTES, model, {**declared, "fallbacks": ()})
    rerouted = _identity(*_persist(tmp_path))
    assert original["routing"] != rerouted["routing"]
    assert original["digest"] != rerouted["digest"]
    monkeypatch.setattr(settings, "model_name", "openrouter/test/other:free")
    changed_model = _identity(*_persist(tmp_path))
    assert changed_model["digest"] != rerouted["digest"]
    assert changed_model["goal_sha256"] == original["goal_sha256"]


@pytest.mark.parametrize("fault", ["missing", "corrupt", "model", "config"])
def test_invalid_identity_stops_before_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from co_scientist.core.config import settings
    from co_scientist.orchestration import task_worker
    from co_scientist.platform.db import runs

    run_id, db = _persist(tmp_path)
    run = runs.get_run(run_id, db_path=db)
    assert run is not None
    config = run.config
    if fault == "missing":
        del config["evaluation_identity"]
    elif fault == "corrupt":
        config["evaluation_identity"]["goal_sha256"] = "altered"
    elif fault == "config":
        config["max_llm_calls"] += 1
    else:
        monkeypatch.setattr(settings, "model_name", "openrouter/changed:free")
    _rewrite_config(run_id, config, db)

    async def must_not_run(*args: Any, **kwargs: Any) -> None:
        pytest.fail("worker started with an invalid comparison identity")

    monkeypatch.setattr(task_worker, "run_run_worker_pool", must_not_run)
    with pytest.raises(ValueError, match="comparison"):
        _run_driver.drive_arm_run(run_id, db)


@pytest.mark.parametrize("fault", ["model", "config", "identity"])
def test_worker_drift_cannot_produce_an_arm_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from co_scientist.core.config import settings
    from co_scientist.orchestration import task_worker
    from co_scientist.platform.db import runs

    db = str(tmp_path / "arms.db")
    _run_driver.configure_environment(db, live=False)

    async def changed_worker(run_id: str, *args: Any, **kwargs: Any) -> None:
        if fault == "model":
            monkeypatch.setattr(settings, "model_name", "openrouter/changed:free")
            return
        run = runs.get_run(run_id, db_path=db)
        assert run is not None
        config = run.config
        if fault == "config":
            config["max_llm_calls"] += 1
        else:
            identity = config["evaluation_identity"]
            identity["request_policy_sha256"] = "altered"
            del identity["digest"]
            identity["digest"] = identity_digest(identity)
        _rewrite_config(run_id, config, db)

    monkeypatch.setattr(task_worker, "run_run_worker_pool", changed_worker)
    with pytest.raises(ValueError, match="comparison"):
        _run_driver.run_arm(
            "Public goal",
            "express",
            {},
            _run_driver.ArmInvocation("comparison-test", "offline", db),
        )


def test_provenance_records_source_and_prompts_and_passes_inputs_through() -> None:
    provenance = _artifacts.build_provenance(
        model="offline/test-model", seed="42", cost={"total_usd": 0.01}
    )

    assert provenance["source"]["git_commit"] != "unknown"
    assert provenance["prompts"]["file_count"] > 0
    assert provenance["prompts"]["digest"] is not None
    assert provenance["model"] == "offline/test-model"
    assert provenance["seed"] == "42"
    assert provenance["cost"] == {"total_usd": 0.01}


@pytest.fixture
def scratch_results_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    # The writer uses relative_to, so artifacts must stay under the repo root.
    scratch = _artifacts._RESULTS_DIR / "_pytest_scratch_artifacts"
    monkeypatch.setattr(_artifacts, "_RESULTS_DIR", scratch)
    try:
        yield scratch
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_dated_artifact_stamps_provenance_and_defaults_unknowns_to_none(
    scratch_results_dir: Path,
) -> None:
    out = _artifacts.write_dated_artifact(
        {"ok": True}, "unit-test-artifact", model="m", seed=1, cost=0.0
    )
    written = json.loads((_artifacts._ROOT / out).read_text())
    assert written["ok"] is True
    assert (written["provenance"]["model"], written["provenance"]["seed"]) == (
        "m",
        1,
    )

    out = _artifacts.write_dated_artifact({"n": 1}, "unit-test-defaults")
    provenance = json.loads((_artifacts._ROOT / out).read_text())["provenance"]
    assert [provenance[key] for key in ("model", "seed", "cost")] == [None] * 3
