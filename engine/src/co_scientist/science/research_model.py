from __future__ import annotations

from collections.abc import Sequence

from co_scientist.core.constants import (
    DEFAULT_MAX_TOKENS,
    EXTENDED_MAX_TOKENS,
    strip_citation_markers,
    truncate_for_prompt,
)
from co_scientist.platform.llm import CompletionSpec, LLMCallOptions, call_llm_json
from co_scientist.platform.retrieval.research import (
    Document,
    ExtractedFinding,
    Extraction,
    Finding,
    stripped_string_items,
)
from co_scientist.science.prompts import load_prompt_with_schema

# Extraction embeds every admitted document, so a whole-paper limit would
# multiply by breadth.
_DOCUMENT_MAX_CHARS = 24_000


class LlmResearchModel:
    """Bind the model once so callers choose whether worker or supervisor
    budgets fund research.
    """

    def __init__(
        self,
        model_name: str,
        *,
        run_id: str | None = None,
        temperature: float = 0.4,
    ) -> None:
        """Reading and rendering use lower temperature because variety is
        noise.
        """
        self._model = model_name
        self._run_id = run_id
        self._temperature = temperature
        self._planned_questions: dict[tuple[str, str], str] = {}
        self._planned_queries: dict[str, str] = {}

    async def plan_stances(self, *, goal: str, limit: int) -> Sequence[str]:
        self._planned_questions = {}
        self._planned_queries = {}
        data = await self._ask(
            "research_plan", {"research_goal": goal, "limit": limit}, DEFAULT_MAX_TOKENS
        )
        records = data.get("perspectives")
        if not isinstance(records, list):
            return await self._plan_stances_legacy(goal=goal, limit=limit)

        stances: list[str] = []
        stance_by_key: dict[str, str] = {}
        question_conflicts: set[tuple[str, str]] = set()
        query_conflicts: set[str] = set()
        has_valid_stance = False
        for item in records:
            if not isinstance(item, dict):
                continue
            raw_stance = item.get("stance")
            if not isinstance(raw_stance, str) or not raw_stance.strip():
                continue
            has_valid_stance = True
            stance = raw_stance.strip()
            stance_key = stance.casefold()
            if stance_key not in stance_by_key:
                if len(stances) >= max(limit, 0):
                    continue
                stance_by_key[stance_key] = stance
                stances.append(stance)
            stance = stance_by_key[stance_key]

            question = _nonempty_string(item.get("question"))
            if question is None:
                continue
            question_key = (goal, stance)
            previous_question = self._planned_questions.get(question_key)
            if question_key in question_conflicts:
                continue
            if previous_question is None:
                self._planned_questions[question_key] = question
            elif previous_question != question:
                del self._planned_questions[question_key]
                question_conflicts.add(question_key)
                continue

            query = _nonempty_string(item.get("query"))
            if query is None or question in query_conflicts:
                continue
            previous_query = self._planned_queries.get(question)
            if previous_query is None:
                self._planned_queries[question] = query
            elif previous_query != query:
                del self._planned_queries[question]
                query_conflicts.add(question)

        if not has_valid_stance:
            return await self._plan_stances_legacy(goal=goal, limit=limit)
        return stances

    async def ask_questions(self, *, goal: str, stance: str, limit: int) -> Sequence[str]:
        if limit == 1:
            question = self._planned_questions.get((goal, stance))
            if question is not None:
                return [question]
        data = await self._ask(
            "research_questions",
            {"research_goal": goal, "stance": stance, "limit": limit},
            DEFAULT_MAX_TOKENS,
        )
        return stripped_string_items(data.get("questions"))[:limit]

    async def to_query(self, *, question: str) -> str:
        planned_query = self._planned_queries.get(question)
        if planned_query is not None:
            return planned_query
        data = await self._ask("research_query", {"question": question}, DEFAULT_MAX_TOKENS)
        query = data.get("query")
        # An unusable query is not a refused question; retain the original
        # thread as fallback.
        return query.strip() if isinstance(query, str) and query else question

    async def _plan_stances_legacy(self, *, goal: str, limit: int) -> Sequence[str]:
        data = await self._ask(
            "research_stances",
            {"research_goal": goal, "limit": limit},
            DEFAULT_MAX_TOKENS,
        )
        return stripped_string_items(data.get("stances"))[:limit]

    async def extract(self, *, question: str, documents: Sequence[Document]) -> Extraction:
        if not documents:
            return Extraction()
        data = await self._ask(
            "research_extract",
            {
                "question": question,
                "documents": _numbered(documents),
            },
            EXTENDED_MAX_TOKENS,
        )
        return Extraction(
            findings=_findings(data.get("findings"), documents),
            follow_ups=tuple(stripped_string_items(data.get("follow_ups"))),
        )

    async def compress(self, *, question: str, findings: Sequence[Finding]) -> str:
        if not findings:
            return ""
        data = await self._ask(
            "research_compress",
            {
                "question": question,
                "findings": "\n".join(
                    f"- {finding.text} ({finding.locator})" for finding in findings
                ),
            },
            DEFAULT_MAX_TOKENS,
        )
        summary = data.get("summary")
        return summary.strip() if isinstance(summary, str) else ""

    async def _ask(
        self,
        prompt_name: str,
        variables: dict[str, object],
        max_tokens: int,
    ) -> dict[str, object]:
        prompt, schema = load_prompt_with_schema(prompt_name, variables)
        result = await call_llm_json(
            prompt,
            CompletionSpec(
                role="research_extract" if prompt_name == "research_extract" else "research",
                model_name=self._model,
                max_tokens=max_tokens,
                temperature=self._temperature,
                json_schema=schema,
            ),
            options=LLMCallOptions(
                run_id=self._run_id,
                prompt_name=prompt_name,
            ),
        )
        return result


def _numbered(documents: Sequence[Document]) -> str:
    """Strip source citation markers only from the prompt copy to prevent
    false finding attribution.
    """
    blocks = []
    for index, document in enumerate(documents):
        kind = "full text" if document.full_text else "abstract only"
        text = strip_citation_markers(truncate_for_prompt(document.text, _DOCUMENT_MAX_CHARS))
        blocks.append(f"[{index}] {document.hit.title} ({kind})\n{text}")
    return "\n\n".join(blocks)


def _nonempty_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _findings(raw: object, documents: Sequence[Document]) -> tuple[ExtractedFinding, ...]:
    """Drop unknown document indexes rather than invent plausible source
    provenance.
    """
    if not isinstance(raw, list):
        return ()
    bound = (_one_finding(item, documents) for item in raw)
    return tuple(finding for finding in bound if finding is not None)


def _one_finding(item: object, documents: Sequence[Document]) -> ExtractedFinding | None:
    if not isinstance(item, dict):
        return None
    index, claim, quote = (
        item.get("document"),
        item.get("claim"),
        item.get("quote"),
    )
    if not isinstance(index, int) or not 0 <= index < len(documents):
        return None
    if not isinstance(claim, str) or not isinstance(quote, str):
        return None
    if not claim.strip() or not quote.strip():
        return None
    return ExtractedFinding(
        text=claim.strip(),
        locator=documents[index].hit.locator,
        span=quote.strip(),
    )
