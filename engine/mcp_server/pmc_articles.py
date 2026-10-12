import re
from collections.abc import Iterable

# `(?=[\s/>])` keeps `<article-id>`, `<article-meta>` and similar out.
_ARTICLE_TAG = re.compile(r"<(/?)article(?=[\s/>])[^>]*?(/?)>")
_ARTICLE_META = re.compile(r"<article-meta[\s>].*?</article-meta>", re.DOTALL)
_PMC_ARTICLE_ID = re.compile(
    r"<article-id\b[^>]*\bpub-id-type\s*=\s*[\"'](?:pmc|pmcid)[\"'][^>]*>"
    r"\s*(?:PMC)?(\d+)\s*</article-id>",
    re.IGNORECASE,
)
# PMC answers an ID it cannot serve with `<error id="...">` in the article set.
_UNAVAILABLE = re.compile(r"<error\b[^>]*\bid\s*=\s*[\"'](?:PMC)?(\d+)[\"']", re.IGNORECASE)
_TRUNCATION_MARKERS = ("[truncated]", "Result too long")
_DOCUMENT = '<?xml version="1.0" encoding="UTF-8"?>\n<pmc-articleset>{}</pmc-articleset>'


def _complete_articles(body: str) -> list[str]:
    """Only articles that close before any truncation marker are complete;
    a cut-off tail is left out rather than glued to its neighbour."""
    cutoff = min(
        (at for marker in _TRUNCATION_MARKERS if (at := body.find(marker)) >= 0), default=len(body)
    )
    articles: list[str] = []
    depth = 0
    start = 0
    for tag in _ARTICLE_TAG.finditer(body):
        closing, self_closing = tag.group(1), tag.group(2)
        if self_closing:
            continue
        if not closing:
            if depth == 0:
                start = tag.start()
            depth += 1
            continue
        if depth == 0:
            # A close with no open means the stream is out of step; stop trusting it.
            break
        depth -= 1
        if depth == 0:
            if tag.end() > cutoff:
                break
            articles.append(body[start : tag.end()])
    return articles


def _own_pmc_id(article: str) -> str | None:
    """The first article-meta is the article's own; sub-articles and related
    articles carry their own IDs further in."""
    meta = _ARTICLE_META.search(article)
    if meta is None:
        return None
    ids = {match.group(1) for match in _PMC_ARTICLE_ID.finditer(meta.group(0))}
    return ids.pop() if len(ids) == 1 else None


def split_pmc_articles(body: str, requested: Iterable[str]) -> dict[str, str]:
    """Maps each requested PMC ID to its own article from a batched efetch.
    An ID that is absent, cut off or claimed twice is left out, so the caller
    can fetch it alone instead of keeping a wrong or partial document."""
    wanted = set(requested)
    found: dict[str, str] = {}
    claimed_twice: set[str] = set()
    for article in _complete_articles(body):
        pmc_id = _own_pmc_id(article)
        if pmc_id is None or pmc_id not in wanted:
            continue
        if pmc_id in found:
            claimed_twice.add(pmc_id)
        found[pmc_id] = _DOCUMENT.format(article)
    return {pmc_id: text for pmc_id, text in found.items() if pmc_id not in claimed_twice}


def unavailable_pmc_ids(body: str) -> set[str]:
    return {match.group(1) for match in _UNAVAILABLE.finditer(body)}
