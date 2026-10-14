import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NamedTuple, Optional

from co_scientist.core.constants import (
    DEEP_HYPOTHESIS_MAX_TOKENS,
    DRAFT_MAX_TOKENS_CAP,
    DRAFT_TOKENS_PER_HYPOTHESIS,
    HIGH_TEMPERATURE,
    corpus_slug,
    get_draft_max_iterations,
    scaled_max_tokens,
)
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.llm import (
    DEFAULT_TOOL_LOOP_TOKEN_BUDGET,
    CompletionSpec,
    LLMCallOptions,
    ToolLoop,
    call_llm_with_tools,
    parse_tool_loop_json,
)
from co_scientist.platform.retrieval.tools.provider import MCPToolProvider
from co_scientist.platform.sandbox.skills import (
    catalogue_section,
    seed_licence_notices,
)
from co_scientist.platform.sandbox.workspace import (
    WorkspaceToolProvider,
    open_draft_workspace,
)
from co_scientist.platform.sandbox.workspace.tool_schemas import READ_SKILL
from co_scientist.platform.sandbox.workspace.tools import (
    command_execution_unavailable_reason,
)
from co_scientist.science.falsified_assumptions import build_falsified_assumptions_section
from co_scientist.science.generation.expansion_research import (
    EXPANSION_EXTRA_DRAFT_ITERATIONS,
    build_expansion_section,
    is_research_expansion,
)
from co_scientist.science.prompts import (
    DraftPromptRequest,
    PromptRunContext,
    get_draft_prompt_with_tools,
)

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from co_scientist.platform.retrieval.config import ToolRegistry


def _log_lit_review_context(articles_with_reasoning: str | None, articles: list[Any]) -> None:
    if articles_with_reasoning:
        logger.info("Including lit review summary as context for drafting")
        logger.info(
            "Warm start: corpus already populated with %s papers from literature review",
            len(articles),
        )
    else:
        logger.warning("No lit review summary available - agent will examine papers directly")


class _DraftStateContext(NamedTuple):
    """Skill availability depends on deployment and sandbox confinement,
    neither of which workflow state records."""

    research_goal: str
    supervisor_guidance: dict[str, Any]
    meta_review: Any
    articles_with_reasoning: str | None
    preferences: Any
    attributes: Any
    user_hypotheses: Any
    articles: list[Any]
    run_setup_guidance: Any
    run_focus_guidance: Any
    research_expansion_section: str
    falsified_assumptions_section: str
    lab_constraints: list[str] | None
    skills_section: str


def _gather_draft_state_context(
    state: WorkflowState, skills_section: str = ""
) -> _DraftStateContext:
    articles_with_reasoning = state.get("articles_with_reasoning")
    articles = state.get("articles") or []

    # Draft and validation share the corpus slug for warm retrieval reuse.

    shared_slug = corpus_slug(state["research_goal"])
    logger.info("Using shared corpus slug: %s", shared_slug)

    _log_lit_review_context(articles_with_reasoning, articles)

    return _DraftStateContext(
        research_goal=state["research_goal"],
        skills_section=skills_section,
        supervisor_guidance=state.get("supervisor_guidance", {}),
        meta_review=state.get("meta_review"),
        articles_with_reasoning=articles_with_reasoning,
        preferences=state.get("preferences"),
        attributes=state.get("attributes"),
        user_hypotheses=state.get("starting_hypotheses"),
        articles=articles,
        run_setup_guidance=state.get("run_setup_guidance"),
        run_focus_guidance=state.get("run_focus_guidance"),
        research_expansion_section=build_expansion_section(state),
        falsified_assumptions_section=build_falsified_assumptions_section(state.get("hypotheses")),
        lab_constraints=state.get("lab_constraints"),
    )


def _invoke_draft_prompt_builder(
    count: int,
    max_iterations: int,
    tool_registry: Optional["ToolRegistry"],
    reference_index: Any | None,
    ctx: _DraftStateContext,
) -> str:
    ref_text = reference_index.text if reference_index else ""
    prompt, _ = get_draft_prompt_with_tools(
        DraftPromptRequest(
            research_goal=ctx.research_goal,
            hypotheses_count=count,
            articles=ctx.articles,
            articles_with_reasoning=ctx.articles_with_reasoning,
            skills_section=ctx.skills_section,
            preferences=ctx.preferences,
            attributes=ctx.attributes,
            user_hypotheses=ctx.user_hypotheses,
            max_iterations=max_iterations,
            reference_list=ref_text,
            research_expansion_section=ctx.research_expansion_section,
            falsified_assumptions_section=ctx.falsified_assumptions_section,
            lab_constraints=ctx.lab_constraints,
            context=PromptRunContext(
                supervisor_guidance=ctx.supervisor_guidance,
                meta_review=ctx.meta_review,
                tool_registry=tool_registry,
                run_setup_guidance=ctx.run_setup_guidance,
                run_focus_guidance=ctx.run_focus_guidance,
            ),
        )
    )

    return prompt


if TYPE_CHECKING:
    from co_scientist.platform.retrieval.config import ToolRegistry


def _resolve_tool_registry_fallback(
    tool_registry: Optional["ToolRegistry"], label: str, log: logging.Logger
) -> Optional["ToolRegistry"]:
    if tool_registry is not None:
        return tool_registry
    try:
        from co_scientist.platform.retrieval.config import get_tool_registry

        tool_registry = get_tool_registry()
        log.info("Using global tool registry for %s", label)
    except Exception as e:
        log.warning("Failed to get tool registry: %s", e)
    return tool_registry


def _resolve_mcp_whitelist(
    tool_registry: Optional["ToolRegistry"],
    workflow_name: str,
    label: str,
    log: logging.Logger,
) -> list[str] | None:
    if not tool_registry:
        log.warning("No tool registry - using all available MCP tools")
        return None

    tool_ids = tool_registry.get_tools_for_workflow(workflow_name)
    mcp_whitelist = tool_registry.get_mcp_tool_names(tool_ids)
    log.info("Tool whitelist for %s: %s", label, mcp_whitelist)
    return mcp_whitelist


def _setup_tool_provider(
    mcp_client: Any,
    tool_registry: Optional["ToolRegistry"],
    workflow_name: str,
    label: str,
    log: logging.Logger,
    *,
    run_id: str | None = None,
    corpus: str | None = None,
) -> tuple[MCPToolProvider, list[Any], Optional["ToolRegistry"]]:
    """A missing registry permits all available tools for maintenance callers
    that do not thread configuration through state."""
    tool_registry = _resolve_tool_registry_fallback(tool_registry, label, log)

    provider = MCPToolProvider(mcp_client=mcp_client, run_id=run_id, corpus=corpus)

    mcp_whitelist = _resolve_mcp_whitelist(tool_registry, workflow_name, label, log)

    tools_dict, openai_tools = provider.get_tools(mcp_whitelist=mcp_whitelist)
    log.info("Initialized %s provider with %s tools", label, len(tools_dict))

    return provider, openai_tools, tool_registry


# Skill catalogues/documents consume transcript tokens; fund that budget instead
# of adding turns.


DRAFT_SKILLS_TOKEN_BUDGET = 360_000


_SECTION = """

### Science skills (database access)

Also installed here are scripts that query scientific databases \
directly -- sequences and structures, genomics and regulation, \
pathways, chemistry, clinical trials. They complement the search tools \
above rather than replacing them: those return papers, these return \
records.

{catalogue}

To use one, call `read_skill` with its name for its full instructions \
and the exact command, then run that command with `run_command` in your \
working directory. The one-line summaries above are not enough to use a \
skill correctly. Have each script write to a file and read back the \
fields you need instead of printing everything; the licence notices \
they ask for are already written in `.licenses/`, so skip that step \
entirely.

**Check one entity against a database before you finalise.** Take the \
gene, protein, drug, variant or pathway your drafts lean on hardest and \
look up what is actually recorded about it -- whether the target is \
already drugged, which variants are known, what the pathway contains, \
whether a trial has run. A gap argued from papers alone is a gap in \
what someone wrote up; a record tells you what is known. Say in \
`gap_reasoning` what the lookup showed, including when it closed a gap \
you were about to draft.

Read **at most two** skills: each document is hundreds of lines that \
every later turn re-sends, and what you are judged on is the drafts, \
not the survey."""


def skills_section() -> str:
    catalogue = catalogue_section()
    if not catalogue:
        return ""
    return _SECTION.format(catalogue=catalogue)


@dataclass(frozen=True)
class DraftSkills:
    provider: Any
    tools: list[Any] = field(default_factory=list)
    section: str = ""
    max_prompt_tokens: int = DEFAULT_TOOL_LOOP_TOKEN_BUDGET


def attach_skills(state: WorkflowState, provider: Any, tools: list[Any]) -> DraftSkills:
    """Workspace/skill setup failure must retain MCP drafting rather than
    lose the cycle's hypotheses."""
    if not skills_section():
        return DraftSkills(provider, tools)
    run_id = state.get("run_id")
    if not run_id:
        logger.warning("no run id in state; drafting without science skills")
        return DraftSkills(provider, tools)
    unavailable = command_execution_unavailable_reason()
    if unavailable is not None:
        logger.info("science skills unavailable (%s); drafting without them", unavailable)
        return DraftSkills(provider, tools)
    try:
        # Each pass needs a fresh workspace so prior-cycle results cannot be
        # read as its own.

        session = open_draft_workspace(run_id, uuid.uuid4().hex)
        # Skills may refuse to run until their licence notices exist in the
        # workspace.

        seed_licence_notices(session.root)
        workspace = WorkspaceToolProvider(session, delegate=provider)
        names, _ = workspace.get_tools()
    except OSError as exc:
        logger.warning("could not open a draft workspace: %s", exc)
        return DraftSkills(provider, tools)
    if READ_SKILL not in names:
        # Withhold executable skills when no sandbox can confine their commands.

        logger.info("commands cannot be confined here; drafting without them")
        return DraftSkills(provider, tools)
    logger.info("Draft phase: science skills offered from %s", session.root)
    return DraftSkills(
        workspace,
        workspace.merge_tools(tools),
        skills_section(),
        DRAFT_SKILLS_TOKEN_BUDGET,
    )


if TYPE_CHECKING:
    from co_scientist.platform.retrieval.config import ToolRegistry


@dataclass(frozen=True)
class _DraftCall:
    prompt: str
    openai_tools: list[Any]
    executor: Any
    count: int
    max_iterations: int
    max_prompt_tokens: int = DEFAULT_TOOL_LOOP_TOKEN_BUDGET


def _parse_draft_response(final_response: str) -> list[dict[str, str]]:
    """An empty failed draft would silently make validation a no-op, so
    parsing failure must propagate."""
    drafts: list[dict[str, str]] = parse_tool_loop_json(final_response, "drafts", "Draft phase")
    logger.info("Parsed %s draft hypotheses", len(drafts))
    return drafts


def _compute_draft_max_tokens(count: int, max_iterations: int) -> int:
    # Shallow drafts become shallow final hypotheses; fund full-depth ideation.

    draft_max_tokens = scaled_max_tokens(
        DEEP_HYPOTHESIS_MAX_TOKENS,
        count,
        per_item=DRAFT_TOKENS_PER_HYPOTHESIS,
        cap=DRAFT_MAX_TOKENS_CAP,
    )
    logger.info(
        "Calling draft agent: %s iterations, %s max tokens",
        max_iterations,
        draft_max_tokens,
    )
    return draft_max_tokens


async def _call_draft_llm_with_tools(
    state: WorkflowState,
    call: _DraftCall,
    draft_max_tokens: int,
) -> tuple[str, int]:
    final_response, messages = await call_llm_with_tools(
        prompt=call.prompt,
        spec=CompletionSpec(
            role="drafting",
            model_name=state["model_name"],
            max_tokens=draft_max_tokens,
            temperature=HIGH_TEMPERATURE,
        ),
        loop=ToolLoop(
            tools=call.openai_tools,
            executor=call.executor,
            max_iterations=call.max_iterations,
            max_prompt_tokens=call.max_prompt_tokens,
        ),
        options=LLMCallOptions(
            run_id=state.get("run_id"),
            prompt_name="generate_draft_with_tools",
        ),
    )
    # Count real tool-loop completions, including the closing assistant turn.
    return final_response, sum(1 for m in messages if m.get("role") == "assistant")


async def _invoke_draft_llm(
    state: WorkflowState,
    call: _DraftCall,
    draft_max_tokens: int,
) -> tuple[str, int]:
    """Draft failure cannot degrade to empty input: validation would then
    silently do nothing."""
    try:
        return await _call_draft_llm_with_tools(state, call, draft_max_tokens)
    except Exception as e:
        logger.error("Draft phase failed: %s", e)
        raise


def _log_draft_completion(tool_call_counts: dict[str, int]) -> None:
    total_calls = sum(tool_call_counts.values())
    calls_summary = ", ".join(f"{name}={count}" for name, count in tool_call_counts.items())
    logger.info("Draft phase complete: %s tool calls (%s)", total_calls, calls_summary)


def _compute_draft_iteration_budget(count: int, is_expansion: bool = False) -> int:

    max_iterations = get_draft_max_iterations(count)
    if is_expansion:
        max_iterations += EXPANSION_EXTRA_DRAFT_ITERATIONS
    logger.info(
        "Draft budget: %s iterations for %s hypotheses%s",
        max_iterations,
        count,
        " (research expansion)" if is_expansion else "",
    )
    return max_iterations


async def _prepare_draft_call(
    state: WorkflowState,
    count: int,
    mcp_client: Any,
    tool_registry: Optional["ToolRegistry"],
    reference_index: Any | None,
) -> tuple[DraftSkills, int, str]:

    provider, openai_tools, tool_registry = _setup_tool_provider(
        mcp_client,
        tool_registry,
        "draft_generation",
        "draft",
        logger,
        run_id=state.get("run_id"),
        corpus=corpus_slug(state["research_goal"]),
    )

    skills = attach_skills(state, provider, openai_tools)

    try:
        max_iterations = _compute_draft_iteration_budget(
            count, is_expansion=is_research_expansion(state)
        )
        prompt = _invoke_draft_prompt_builder(
            count,
            max_iterations,
            tool_registry,
            reference_index,
            _gather_draft_state_context(state, skills.section),
        )
    except BaseException:
        await _close_draft_skills(skills)
        raise

    return skills, max_iterations, prompt


async def _close_draft_skills(skills: DraftSkills) -> None:
    if isinstance(skills.provider, WorkspaceToolProvider):
        await skills.provider.session.close()


async def _run_draft_pipeline(
    state: WorkflowState,
    count: int,
    mcp_client: Any,
    tool_registry: Optional["ToolRegistry"],
    reference_index: Any | None,
) -> tuple[str, dict[str, int], int]:
    skills, max_iterations, prompt = await _prepare_draft_call(
        state, count, mcp_client, tool_registry, reference_index
    )

    try:
        draft_tracked_executor, tool_call_counts = skills.provider.tracked_executor("Draft")
        call = _DraftCall(
            prompt=prompt,
            openai_tools=skills.tools,
            executor=draft_tracked_executor,
            count=count,
            max_iterations=max_iterations,
            max_prompt_tokens=skills.max_prompt_tokens,
        )
        draft_max_tokens = _compute_draft_max_tokens(call.count, call.max_iterations)
        final_response, llm_calls = await _invoke_draft_llm(state, call, draft_max_tokens)
        return final_response, tool_call_counts, llm_calls
    finally:
        await _close_draft_skills(skills)


async def draft_hypotheses(
    state: WorkflowState,
    count: int,
    mcp_client: Any,
    tool_registry: Optional["ToolRegistry"] = None,
    reference_index: Any | None = None,
) -> tuple[list[dict[str, str]], int]:
    logger.info("Phase 1: Drafting %s hypotheses by examining literature", count)

    final_response, tool_call_counts, llm_calls = await _run_draft_pipeline(
        state, count, mcp_client, tool_registry, reference_index
    )

    _log_draft_completion(tool_call_counts)

    return _parse_draft_response(final_response), llm_calls
