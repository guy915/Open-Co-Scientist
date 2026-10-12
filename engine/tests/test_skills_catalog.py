from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

import co_scientist.platform.sandbox.skills as catalog
import co_scientist.science.generation.literature_tools.draft as draft_skills
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.llm import DEFAULT_TOOL_LOOP_TOKEN_BUDGET
from co_scientist.platform.sandbox import workspace_write
from co_scientist.platform.sandbox.workspace.output import OutputRecorder
from co_scientist.platform.sandbox.workspace.session import SessionRead, WorkspaceSession
from co_scientist.platform.sandbox.workspace.tool_schemas import READ_SKILL
from co_scientist.platform.sandbox.workspace.tools import _handle_run_command, _ToolContext


@pytest.fixture
def _clear_distribution_cache() -> object:
    """Installed-package discovery is cached across interpreter fixtures."""
    catalog._installed_distributions.cache_clear()
    yield
    catalog._installed_distributions.cache_clear()


@pytest.fixture
def _skills_catalog_clear_cache() -> object:
    """The process-wide catalogue assumes a fixed directory; tests change it."""
    catalog.available_skills.cache_clear()
    yield
    catalog.available_skills.cache_clear()


def _write_skill(
    root: pathlib.Path,
    folder: str,
    front: str,
    body: str = "Body.",
    *,
    runnable: bool = True,
) -> pathlib.Path:
    """A runnable script keeps fixtures from accidentally testing
    withholding."""
    directory = root / folder
    (directory / "scripts").mkdir(parents=True)
    if runnable:
        (directory / "scripts" / "cli.py").write_text("", encoding="utf-8")
    (directory / "SKILL.md").write_text(f"---\n{front}\n---\n\n{body}\n", encoding="utf-8")
    return directory


def _install_skill(root: pathlib.Path, name: str, description: str = "Queries things.") -> None:
    _write_skill(root, name, f"name: {name}\ndescription: {description}")


@pytest.mark.usefixtures("_clear_distribution_cache", "_skills_catalog_clear_cache")
class TestSkillsCatalog:
    def test_run_command_only_dispatches_credentials_to_authorized_skill(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        skill_root = tmp_path / "skills"
        skill = _write_skill(
            skill_root,
            "ncbi_sequence_fetch",
            "name: ncbi_sequence_fetch\ndescription: Queries NCBI.",
        )
        script = skill / "scripts" / "cli.py"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        interpreter_alias = workspace / "python"
        interpreter_alias.symlink_to(sys.executable)
        monkeypatch.setenv(catalog.SKILLS_DIR_ENV, str(skill_root))
        monkeypatch.setenv(catalog.SKILLS_PYTHON_ENV, sys.executable)
        monkeypatch.setenv("NCBI_API_KEY", "ncbi-secret")
        monkeypatch.setenv("FDA_API_KEY", "fda-secret")

        session = WorkspaceSession(
            workspace, policy=workspace_write(workspace), skills_enabled=True
        )
        context = _ToolContext(session, OutputRecorder(session.root))
        dispatched: list[tuple[list[str], dict[str, str] | None]] = []

        class _FakeCommandSession:
            async def wait_for(self, seconds: float) -> None:
                del seconds

            def read(self) -> SessionRead:
                return SessionRead(
                    session_id="fake-session",
                    running=False,
                    exit_code=0,
                    stdout="",
                    stderr="",
                    cursor={"stdout": 0, "stderr": 0},
                    truncated={"stdout": False, "stderr": False},
                )

        async def _fake_start(argv: list[str], **kwargs: object) -> _FakeCommandSession:
            env_extra = kwargs.get("env_extra")
            assert env_extra is None or isinstance(env_extra, dict)
            dispatched.append((argv, env_extra))
            return _FakeCommandSession()

        monkeypatch.setattr(session.sessions, "start", _fake_start)

        async def invoke(argv: list[str]) -> None:
            await _handle_run_command(context, {"argv": argv})

        asyncio.run(invoke([sys.executable, "-c", "print(1)", str(script)]))
        asyncio.run(invoke([str(interpreter_alias), str(script)]))
        asyncio.run(invoke([sys.executable, str(script)]))

        assert dispatched == [
            ([sys.executable, "-c", "print(1)", str(script)], None),
            ([str(interpreter_alias), str(script)], None),
            ([sys.executable, str(script)], {"NCBI_API_KEY": "ncbi-secret"}),
        ]

    def test_skill_credentials_require_the_declared_script_operand(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        skill_root = tmp_path / "skills"
        _install_skill(skill_root, "ncbi_sequence_fetch")
        _install_skill(skill_root, "openfda_database")
        monkeypatch.setenv(catalog.SKILLS_DIR_ENV, str(skill_root))
        monkeypatch.setenv(catalog.SKILLS_PYTHON_ENV, sys.executable)
        monkeypatch.setenv("NCBI_API_KEY", "ncbi-secret")
        monkeypatch.setenv("FDA_API_KEY", "fda-secret")
        script = skill_root / "ncbi_sequence_fetch" / "scripts" / "cli.py"

        assert catalog.invoked_skill([sys.executable, str(script)]) == "ncbi_sequence_fetch"
        assert catalog.invoked_skill([sys.executable, "-c", "print(1)", str(script)]) is None
        assert catalog.invoked_skill([sys.executable, "-m", "json.tool", str(script)]) is None
        assert catalog.invoked_skill([sys.executable, "--", str(script)]) is None
        assert catalog.invoked_skill([sys.executable, str(script.parent)]) is None
        assert catalog.skill_environment("ncbi_sequence_fetch") == {"NCBI_API_KEY": "ncbi-secret"}
        assert catalog.skill_environment("openfda_database") == {"FDA_API_KEY": "fda-secret"}
        assert catalog.skill_environment("unrecognized") == {}

    def test_skill_recognition_rejects_symlinks_and_writable_script_roots(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        skill_root = tmp_path / "skills"
        skill = _write_skill(
            skill_root,
            "ncbi_sequence_fetch",
            "name: ncbi_sequence_fetch\ndescription: Queries NCBI.",
        )
        monkeypatch.setenv(catalog.SKILLS_DIR_ENV, str(skill_root))
        monkeypatch.setenv(catalog.SKILLS_PYTHON_ENV, sys.executable)
        script = skill / "scripts" / "cli.py"
        link = skill / "scripts" / "alias.py"
        link.symlink_to(script)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        interpreter_alias = workspace / "python"
        interpreter_alias.symlink_to(sys.executable)

        assert catalog.invoked_skill([sys.executable, str(link)]) is None
        assert (
            catalog.invoked_skill(
                [sys.executable, str(script)], writable_roots=(skill_root.resolve(),)
            )
            is None
        )
        assert (
            catalog.invoked_skill(
                [str(interpreter_alias), str(script)],
                cwd=workspace,
                writable_roots=(workspace.resolve(),),
            )
            is None
        )

        trusted_venv = tmp_path / "trusted-venv"
        (trusted_venv / "nested").mkdir(parents=True)
        trusted_python = trusted_venv / "python"
        trusted_python.symlink_to(sys.executable)
        configured_with_parent = trusted_venv / "nested" / ".." / "python"
        monkeypatch.setenv(catalog.SKILLS_PYTHON_ENV, str(configured_with_parent))
        assert (
            catalog.invoked_skill(
                [str(configured_with_parent), str(script)],
                cwd=workspace,
                writable_roots=(workspace.resolve(),),
            )
            == "ncbi_sequence_fetch"
        )
        monkeypatch.setenv(catalog.SKILLS_PYTHON_ENV, str(interpreter_alias))
        assert (
            catalog.invoked_skill(
                [str(interpreter_alias), str(script)],
                cwd=workspace,
                writable_roots=(workspace.resolve(),),
            )
            is None
        )

    def test_a_reference_file_is_reachable_but_never_outside_the_skill(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Bundles disclose an overview first and reference files second, and
        the path comes from the model and cannot be trusted."""
        directory = _write_skill(
            tmp_path,
            "string",
            "name: string\ndescription: Queries STRING.",
            body="See references/interactions.md.",
        )
        (directory / "references").mkdir()
        (directory / "references" / "interactions.md").write_text(
            "Run `string_cli.py partners --identifiers TP53`.\n",
            encoding="utf-8",
        )
        (tmp_path / "secret.txt").write_text("not yours", encoding="utf-8")
        monkeypatch.setenv(catalog.SKILLS_DIR_ENV, str(tmp_path))

        text = catalog.read_skill_document("string", "references/interactions.md")

        assert text is not None
        assert "partners --identifiers" in text
        assert "Skill directory:" not in text
        assert catalog.read_skill_document("string", "../secret.txt") is None
        assert catalog.read_skill_document("string", "/etc/hosts") is None

    def test_the_preamble_states_the_workspace_network_policy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        _write_skill(tmp_path, "string", "name: string\ndescription: Queries STRING.")
        monkeypatch.setenv(catalog.SKILLS_DIR_ENV, str(tmp_path))

        confined = catalog.read_skill_document("string")
        networked = catalog.read_skill_document("string", network_allowed=True)

        assert confined is not None and networked is not None
        assert "no network access" in confined
        assert "network is available" not in confined
        assert "network is available for the skill's own API calls only" in networked


# Backend-exempt, so `can_run_commands` is true without a sandbox backend
# on the host running the tests -- which CI does not have.


_STATE: WorkflowState = {"research_goal": "a goal", "run_id": "run-1"}  # type: ignore[typeddict-item]


class _StubProvider:
    """Stands in for the MCP provider the phase already resolved."""


_MCP_TOOLS = [{"type": "function", "function": {"name": "search_pubmed"}}]


@pytest.mark.usefixtures("_skills_catalog_clear_cache")
class TestSkillsDraftPhase:
    def test_skills_skip_workspace_when_command_lifecycle_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        _install_skill(tmp_path / "skills", "uniprot")
        monkeypatch.setenv(catalog.SKILLS_DIR_ENV, str(tmp_path / "skills"))
        monkeypatch.setattr(
            draft_skills,
            "command_execution_unavailable_reason",
            lambda: "no writable delegated cgroup v2 lifecycle boundary",
        )
        monkeypatch.setattr(
            draft_skills,
            "open_draft_workspace",
            lambda *_args: pytest.fail("unavailable command capability opened a workspace"),
        )

        attached = draft_skills.attach_skills(_STATE, _StubProvider(), _MCP_TOOLS)

        assert attached.tools == _MCP_TOOLS
        assert attached.section == ""

    def test_skills_attach_beside_the_search_tools(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        _install_skill(
            tmp_path / "skills",
            "uniprot",
            "Queries UniProt. Then a second sentence nobody needs to route.",
        )
        monkeypatch.setenv(catalog.SKILLS_DIR_ENV, str(tmp_path / "skills"))
        monkeypatch.setattr(
            "co_scientist.platform.sandbox.workspace.run_workspace.workspaces_root",
            lambda: tmp_path / "ws",
        )
        monkeypatch.setattr(
            "co_scientist.platform.sandbox.workspace.tools.command_lifecycle_available",
            lambda: True,
        )
        monkeypatch.setattr(
            "co_scientist.platform.sandbox.workspace.tools.sandbox_backend", lambda: "test"
        )

        attached = draft_skills.attach_skills(_STATE, _StubProvider(), _MCP_TOOLS)

        names = {schema["function"]["name"] for schema in attached.tools if "function" in schema}
        assert READ_SKILL in names
        assert "search_pubmed" in names
        # The transcript backstop binds before the turn limit; more turns cannot
        # fund skills.
        assert attached.max_prompt_tokens > DEFAULT_TOOL_LOOP_TOKEN_BUDGET
        # The catalogue is resent on every transcript turn, so it carries
        # summaries rather than full descriptions.
        assert "uniprot: Queries UniProt." in attached.section
        assert "second sentence" not in attached.section
