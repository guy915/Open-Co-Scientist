import dataclasses
import logging
import re
from typing import TYPE_CHECKING, Any

from co_scientist.core.constants import strip_citation_markers
from co_scientist.core.exceptions import TASK_CONTROL_FLOW_ERRORS
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.retrieval.article import Article
from co_scientist.science.prompts._common import _format_meta_review_context

logger = logging.getLogger(__name__)


# Abstracts are already summaries; this bound mainly protects fulltext fallback.
PUBLIC_SNIPPET_CHARS = 2000

# Private notes/protocols say less per character than published abstracts, so
# their allowance is wider and tied to the public bound.
PRIVATE_SNIPPET_CHARS = PUBLIC_SNIPPET_CHARS * 5 // 4

# Opening and probe blocks share a prompt; distinct prefixes prevent ambiguous
# citations.
PUBLIC_LABEL = "P"
PRIVATE_LABEL = "E"
RETRIEVED_LABEL = "V"


@dataclasses.dataclass(frozen=True)
class EvidenceCaps:
    articles: int | None = None
    private_sources: int | None = None
    total_chars: int | None = None


_NO_CAPS = EvidenceCaps()


def showable_articles(
    articles: list[Article] | None, *, require_analyzed: bool = True
) -> list[Article]:
    """Retrieval demotion does not exclude retracted papers when the corpus
    is small."""
    return [
        article
        for article in (articles or [])
        if (article.used_in_analysis or not require_analyzed) and not article.is_retracted
    ]


def _article_excerpt(article: Article) -> str:
    """Fulltext may be the only content; strip public citation contamination
    but preserve intentional private citations."""
    excerpt = (article.abstract or article.content or "")[:PUBLIC_SNIPPET_CHARS]
    return strip_citation_markers(excerpt)


def _head(items: list[Any], cap: int | None) -> list[Any]:
    return items if cap is None else items[:cap]


def _article_sections(articles: list[Article], label: str) -> list[str]:
    """Contiguous rendered keys avoid implying that skipped sources were
    withheld."""
    return [
        f"[{label}{index}] {article.title}: {_article_excerpt(article)}"
        for index, article in enumerate(articles, start=1)
    ]


def _private_sections(sources: list[dict[str, Any]]) -> list[str]:
    return [
        f"[{PRIVATE_LABEL}{index}] {str(source.get('display') or '')[:PRIVATE_SNIPPET_CHARS]}"
        for index, source in enumerate(sources, start=1)
    ]


def build_evidence_context(
    articles: list[Article] | None,
    *,
    private_sources: list[dict[str, Any]] | None = None,
    caps: EvidenceCaps = _NO_CAPS,
    require_analyzed: bool = True,
    article_label: str = PUBLIC_LABEL,
) -> str:
    usable = showable_articles(articles, require_analyzed=require_analyzed)
    sections = [
        *_article_sections(_head(usable, caps.articles), article_label),
        *_private_sections(_head(list(private_sources or []), caps.private_sources)),
    ]
    joined = "\n\n".join(sections)
    return joined if caps.total_chars is None else joined[: caps.total_chars]


if TYPE_CHECKING:
    from co_scientist.platform.retrieval.evidence.search_support import (
        SearchConfig,
    )


_MAX_PROBE_QUERIES = 3

_MAX_PROBE_SOURCES = 6

# Coverage matching needs topical text, not an unbounded full-paper read.
_CORPUS_MATCH_CHARS = 4000

# One shared term is not grounding; require a substantial query-term match.
_MIN_QUERY_COVERAGE = 0.5

# Persist fallback provenance so unavailable live search is distinguishable.
CORPUS_FALLBACK_NOTE = "MCP unavailable; probes grounded against the run's retrieved corpus"

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_TOKEN_RE.findall(text.casefold()))


def _query_coverage(query_terms: frozenset[str], article: Article) -> float:
    """Normalize by query terms: the much larger article union would drive
    matches to zero."""
    searchable = " ".join((article.title, article.abstract or "", article.content or ""))[
        :_CORPUS_MATCH_CHARS
    ]
    matches = query_terms & _tokens(searchable)
    return len(matches) / len(query_terms)


def _corpus_probe_evidence(
    state: WorkflowState, queries: list[str]
) -> tuple[list[Article], list[str]]:
    """An unavailable search backend must not discard evidence the run
    already retrieved."""
    query_term_sets = [terms for terms in map(_tokens, queries) if terms]
    scored: list[tuple[float, int, str, Article]] = []
    for article in showable_articles(state.get("articles"), require_analyzed=False):
        coverage = max(
            (_query_coverage(terms, article) for terms in query_term_sets),
            default=0.0,
        )
        if coverage < _MIN_QUERY_COVERAGE:
            continue
        scored.append((-coverage, -article.citations, article.title, article))
    ranked = [entry[-1] for entry in sorted(scored)]
    return ranked[:_MAX_PROBE_SOURCES], [CORPUS_FALLBACK_NOTE]


def _probe_queries(result: dict[str, Any]) -> list[str]:
    """Keyword indexes AND prose terms; use targeted queries, falling back to
    the question only when absent."""
    probes = result.get("probes") or []
    ordered = sorted(
        probes,
        key=lambda probe: not bool(probe.get("assumption_is_fundamental")),
    )
    queries: list[str] = []
    seen: set[str] = set()
    for probe in ordered:
        raw = probe.get("search_query") or probe.get("question") or ""
        query = " ".join(str(raw).split())
        key = query.casefold()
        if not query or key in seen:
            continue
        seen.add(key)
        queries.append(query)
        if len(queries) == _MAX_PROBE_QUERIES:
            break
    return queries


def merge_retrieved_articles(
    existing: list[Article] | None,
    results: list[dict[str, Any] | None],
) -> list[Article]:
    merged = list(existing or [])
    identities = {
        (article.source, article.source_id or article.doi or article.url) for article in merged
    }
    for result in results:
        if not result:
            continue
        for payload in result.get("retrieved_articles") or []:
            article = Article.from_dict(payload)
            identity = (
                article.source,
                article.source_id or article.doi or article.url,
            )
            if identity in identities:
                continue
            identities.add(identity)
            merged.append(article)
    return merged


async def _retrieve_probe_evidence(
    state: WorkflowState, queries: list[str]
) -> tuple[list[Article], list[str]]:
    """Without live search, ground against the existing corpus rather than no
    evidence."""
    if not queries:
        return [], []
    if not state.get("mcp_available"):
        return _corpus_probe_evidence(state, queries)

    from co_scientist.platform.retrieval.evidence.article_support import (
        build_articles_from_metadata,
    )

    config = _probe_search_config(state)
    metadata, errors, failure = await _collect_probe_papers(state, queries, config)
    if failure is not None:
        return [], failure

    articles = build_articles_from_metadata(metadata, config.source_name)
    usable = [
        article
        for article in articles
        if not article.is_retracted and (article.abstract or article.content)
    ]
    return usable[:_MAX_PROBE_SOURCES], errors


def _probe_search_config(state: WorkflowState) -> "SearchConfig":
    """Semantic scoring would multiply calls by idea, cycle and caller; the
    run-level pass already paid that cost."""
    from co_scientist.platform.retrieval.evidence.search_support import (
        search_config_for,
    )

    return dataclasses.replace(
        search_config_for(state),
        papers_to_read_count=_MAX_PROBE_SOURCES,
        semantic_relevance_enabled=False,
    )


async def _collect_probe_papers(
    state: WorkflowState, queries: list[str], config: "SearchConfig"
) -> tuple[dict[str, Any], list[str], list[str] | None]:
    """Search failure becomes retrieval data, allowing verification to
    degrade safely."""
    from co_scientist.platform.retrieval.evidence.search import (
        collect_papers,
    )
    from co_scientist.platform.retrieval.mcp_client import get_mcp_client

    errors: list[str] = []
    try:
        client = await get_mcp_client(tool_registry=config.tool_registry)
        metadata, _ = await collect_papers(queries, state, config, client, errors)
    except TASK_CONTROL_FLOW_ERRORS:
        raise
    except Exception as exc:
        logger.warning("Probe evidence retrieval unavailable: %s", exc)
        return {}, errors, [*errors, str(exc)]
    return metadata, errors, None


def _retrieved_evidence_context(articles: list[Article]) -> str:
    """Probe keys must not collide with the opening evidence in the same
    prompt."""
    return build_evidence_context(articles, require_analyzed=False, article_label=RETRIEVED_LABEL)


def _augment_evidence_context_with_meta_review(evidence_context: str, state: WorkflowState) -> str:
    """Recurring cross-agent errors must reach verification as well as
    generation."""
    meta_context = _format_meta_review_context(state.get("meta_review"))
    if not meta_context:
        return evidence_context
    return (
        f"{evidence_context}\n\nCross-agent meta-review feedback "
        f"(recurring patterns to probe):\n{meta_context}"
    )
