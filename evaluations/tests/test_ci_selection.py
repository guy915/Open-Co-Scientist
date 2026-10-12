from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock
from urllib.request import Request

import pytest
from scripts import select_targets as selector
from scripts.presubmit import commands_for

ROOT = Path(__file__).resolve().parents[2]


def test_frontend_source_selects_its_consumers_but_not_backend_tests() -> None:
    selected = selector.select(["app/frontend/src/app/layout.tsx"], selector.load_rules(ROOT))
    assert selected["frontend"] and selected["e2e"] and selected["evaluations"]
    assert not selected["app"] and not selected["engine"] and not selected["docker"]
    commands = commands_for(selected)
    assert "test-frontend" in commands and "e2e" in commands and "e2e-production" in commands
    assert "test-app" not in commands and "test-engine" not in commands


def test_engine_source_selects_durable_and_browser_consumers() -> None:
    selected = selector.select(["engine/src/co_scientist/main.py"], selector.load_rules(ROOT))
    assert all(
        selected[name] for name in ("engine", "app", "e2e", "docker", "evaluations", "python_lint")
    )
    assert not selected["frontend"]
    commands = commands_for(selected)
    assert commands.index("test-engine") < commands.index("test-app")
    assert len(commands) == len(set(commands))


def test_docs_only_skip_test_targets_but_keep_other_affected_gates() -> None:
    selected = selector.select(["engine/README.md"], selector.load_rules(ROOT))
    assert not any(selected[target] for target in selector.TEST_TARGETS)
    assert selected["python_lint"]
    assert commands_for(selected) == ["ci-guards", "lint-python"]


@pytest.mark.parametrize(
    "path", ["requirements/api-linux-py312.txt", "NOTICE", "engine/NOTICE", "app/LICENSE"]
)
def test_licence_and_lock_changes_run_the_inventory_guard(path: str) -> None:
    selected = selector.select([path], selector.load_rules(ROOT))
    assert selected["evaluations"]
    assert "test-evaluations" in commands_for(selected)


def test_container_probe_changes_execute_real_image_start_checks() -> None:
    selected = selector.select(["scripts/ci/container_smoke.py"], selector.load_rules(ROOT))
    assert selected["docker"]
    assert "docker-build" in commands_for(selected)


def test_native_and_workflow_changes_have_explicit_local_equivalents() -> None:
    rules = selector.load_rules(ROOT)
    sandbox = selector.select(["engine/src/co_scientist/platform/sandbox/seatbelt.py"], rules)
    assert sandbox["sandbox"]
    assert "test-engine" in commands_for(sandbox)
    native = selector.select([".github/workflows/cross-browser.yml"], rules)
    assert native["cross_browser"] and native["workflows"]
    assert "lint-workflows" in commands_for(native)
    assert "e2e-production" in commands_for(native)
    frontend = selector.select(["app/frontend/src/app/layout.tsx"], rules)
    assert frontend["cross_browser"] and not frontend["sandbox"]
    assert not frontend["workflows"]


def test_launch_guards_run_even_with_no_changed_targets() -> None:
    selected = selector.select([], selector.load_rules(ROOT))
    assert commands_for(selected) == ["ci-guards"]


@pytest.mark.parametrize("target", ["sandbox", "cross_browser", "workflows"])
def test_missing_launch_selection_rules_fail_closed(tmp_path: Path, target: str) -> None:
    rules = selector.load_rules(ROOT)
    rules.pop(target)
    directory = tmp_path / ".github"
    directory.mkdir()
    (directory / "ci_paths.json").write_text(json.dumps(rules))
    with pytest.raises(ValueError, match="Missing target rules"):
        selector.load_rules(tmp_path)


def test_unknown_paths_and_full_events_select_comprehensive_checks() -> None:
    rules = selector.load_rules(ROOT)
    assert all(selector.select(["new-tool/new-file"], rules).values())
    assert all(selector.select([], rules, full=True).values())
    assert not any(selector.select([], rules).values())


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("README.md", "**/*.md", True),
        ("docs/a/b.md", "**/*.md", True),
        ("app/tests/a.py", "app/*", False),
        ("app/pyproject.toml", "app/*", True),
        ("engine/a/b.py", "engine/**", True),
    ],
)
def test_globs_respect_path_boundaries(path: str, pattern: str, expected: bool) -> None:
    assert selector.matches(path, pattern) is expected


def test_renames_select_both_sides_of_the_local_diff(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.test")
    path = tmp_path / "engine/old.py"
    path.parent.mkdir()
    path.write_text("value = 1\n")
    git("add", ".")
    git("commit", "-m", "test(fixture): add source")
    git("branch", "base")
    new = tmp_path / "app/frontend/new.ts"
    new.parent.mkdir(parents=True)
    path.rename(new)
    git("add", "-A")
    git("commit", "-m", "test(fixture): move source")
    assert set(selector.changed_paths(tmp_path, "base")) == {
        "engine/old.py",
        "app/frontend/new.ts",
    }


def test_pr_file_pagination_preserves_previous_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [
        [{"filename": f"docs/{i}.md"} for i in range(100)],
        [{"filename": "docs/new.md", "previous_filename": "engine/old.py"}],
    ]
    request_urls: list[str] = []

    def fetch(request: Request, timeout: int) -> MagicMock:
        request_urls.append(request.full_url)
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(pages.pop(0))
        return response

    monkeypatch.setattr(selector, "urlopen", fetch)
    paths = selector.pull_request_paths("owner/repo", 42, "synthetic", 101)
    assert paths is not None and "engine/old.py" in paths
    assert len(request_urls) == 2 and request_urls[1].endswith("page=2")


def test_truncated_pr_file_lists_cannot_silently_skip_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = '[{"filename": "docs/one.md"}]'
    monkeypatch.setattr(selector, "urlopen", lambda *_args, **_kwargs: response)
    assert selector.pull_request_paths("owner/repo", 42, "synthetic", 2) is None
    assert selector.pull_request_paths("owner/repo", 42, "synthetic", 3001) is None


def test_unknown_local_gate_requires_an_explicit_equivalent() -> None:
    with pytest.raises(ValueError, match="No local equivalent"):
        commands_for({"new_check": True})


def test_api_diff_growth_after_the_event_selects_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps([{"filename": f"docs/{i}.md"} for i in range(100)])
    monkeypatch.setattr(selector, "urlopen", lambda *_args, **_kwargs: response)
    assert selector.pull_request_paths("owner/repo", 42, "synthetic", 100) is None


def test_shared_recipes_and_selector_changes_cover_all_consumers() -> None:
    rules = selector.load_rules(ROOT)
    for path in ("Makefile", ".github/ci_paths.json", "scripts/select_targets.py"):
        assert all(selector.select([path], rules).values())


def test_frontend_equivalent_keeps_the_enabled_bundle_budget() -> None:
    selected = selector.select(["app/frontend/src/app/layout.tsx"], selector.load_rules(ROOT))
    assert "build-checked" in commands_for(selected)
