from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import functools
import logging
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

import httpx

import co_scientist.platform.retrieval.retraction_set as retraction_set
from co_scientist.core.text_matching import coverage, tokenize
from co_scientist.platform.retrieval.pinned_http import (
    UnsafeUrlError,
    request_with_screened_redirects,
)

logger = logging.getLogger(__name__)


class Resolvability(str, enum.Enum):
    """Source resolvability is separate from claim support."""

    RESOLVABLE = "resolvable"
    UNRESOLVABLE = "unresolvable"
    RETRACTED = "retracted"


class SourceType(str, enum.Enum):
    """Reviewed papers, preprints and curated records have distinct provenance;
    unknown source types must never be guessed.
    """

    PEER_REVIEWED = "peer_reviewed"
    PREPRINT = "preprint"
    DATABASE = "database"
    WEB = "web"
    DOCUMENT = "document"
    UNKNOWN = "unknown"

    @property
    def is_publication(self) -> bool:
        """Missing dates are defects only for publications, not records, web
        pages or attachments.
        """
        return self in (SourceType.PEER_REVIEWED, SourceType.PREPRINT)


class DateState(str, enum.Enum):
    PRESENT = "present"
    MISSING = "missing"
    IMPLAUSIBLE = "implausible"


@dataclasses.dataclass(frozen=True)
class CitationMetadata:
    """Persist publisher type during drain: evidence rows cannot reconstruct
    that stronger classification signal later.
    """

    url: str = ""
    doi: str = ""
    pmid: str = ""
    available: bool = True
    retracted: bool = False
    source: str = ""
    publication_type: str = ""
    year: int | None = None

    @property
    def has_identifier(self) -> bool:
        return bool(self.doi or self.pmid or self.url)


Resolver = Callable[[CitationMetadata], Resolvability]


def offline_resolver(meta: CitationMetadata) -> Resolvability:
    """Retraction overrides reachability; bare DOI/PMID identifiers are
    resolvable without an accompanying URL.
    """
    if meta.retracted:
        return Resolvability.RETRACTED
    if not meta.available or not meta.has_identifier:
        return Resolvability.UNRESOLVABLE
    return Resolvability.RESOLVABLE


def assess_resolvability(
    meta: CitationMetadata, *, resolver: Resolver = offline_resolver
) -> Resolvability:
    """Source metadata deliberately excludes claims and evidence text so
    reachability cannot be confused with support.
    """
    return resolver(meta)


# Unknown retrieval-source names stay UNKNOWN rather than being guessed from
# similar names.
_SOURCE_TYPE_BY_NAME = {
    "pubmed": SourceType.PEER_REVIEWED,
    "europepmc": SourceType.PEER_REVIEWED,
    "openalex": SourceType.PEER_REVIEWED,
    "academic": SourceType.PEER_REVIEWED,
    "arxiv": SourceType.PREPRINT,
    "biorxiv": SourceType.PREPRINT,
    "medrxiv": SourceType.PREPRINT,
    "preprint": SourceType.PREPRINT,
    "preprints": SourceType.PREPRINT,
    "scientific_database": SourceType.DATABASE,
    # Knowledge-graph statements are curated records, not publications.
    "knowledge_graph": SourceType.DATABASE,
    "web": SourceType.WEB,
    "attachment": SourceType.DOCUMENT,
}

# Check preprint first: PubMed indexes both preprints and reviewed journal
# articles.
_PUBLICATION_TYPE_MARKERS = (
    ("preprint", SourceType.PREPRINT),
    ("journal article", SourceType.PEER_REVIEWED),
    ("review", SourceType.PEER_REVIEWED),
    ("clinical trial", SourceType.PEER_REVIEWED),
    ("meta-analysis", SourceType.PEER_REVIEWED),
)

_PREPRINT_HOSTS = ("biorxiv.org", "medrxiv.org", "arxiv.org", "chemrxiv.org")


def _type_from_publication_type(publication_type: str) -> SourceType | None:
    text = publication_type.strip().lower()
    if not text:
        return None
    for marker, source_type in _PUBLICATION_TYPE_MARKERS:
        if marker in text:
            return source_type
    return None


def classify_source_type(meta: CitationMetadata) -> SourceType:
    """Publisher type outranks retrieval source because PubMed also indexes
    preprints.
    """
    declared = _type_from_publication_type(meta.publication_type)
    if declared is not None:
        return declared
    named = _SOURCE_TYPE_BY_NAME.get(meta.source.strip().lower())
    if named is not None:
        return named
    url = meta.url.lower()
    if any(host in url for host in _PREPRINT_HOSTS):
        return SourceType.PREPRINT
    return SourceType.UNKNOWN


# Philosophical Transactions began in 1665; earlier publication years are parse
# artifacts.
_EARLIEST_PLAUSIBLE_YEAR = 1665


def classify_date(meta: CitationMetadata, *, today_year: int | None = None) -> DateState:
    """Next-year accepted papers are plausible; pre-1665 dates precede the first
    scientific journal and indicate parse artifacts.
    """
    year = meta.year
    if not year:
        return DateState.MISSING
    latest = (today_year or dt.date.today().year) + 1
    if year < _EARLIEST_PLAUSIBLE_YEAR or year > latest:
        return DateState.IMPLAUSIBLE
    return DateState.PRESENT


_RESOLVE_CONCURRENCY = 6
_RESOLVE_TIMEOUT_SECONDS = 8.0

_PUBMED_ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"


def _doi_url(doi: str) -> str:
    return f"https://doi.org/{doi.strip()}"


def _reachable(client: httpx.Client, url: str) -> bool:
    try:
        response = request_with_screened_redirects(client, "HEAD", url)
        return response.status_code < 400
    except (httpx.HTTPError, UnsafeUrlError, ValueError) as exc:
        logger.debug("dereference failed for %s: %s", url, exc)
        return False


def _pmid_found(client: httpx.Client, pmid: str) -> bool:
    if not pmid.isascii() or not pmid.isdecimal():
        return False
    try:
        response = client.get(
            _PUBMED_ESUMMARY_URL,
            params={"db": "pubmed", "id": pmid, "retmode": "json"},
        )
        if response.status_code >= 400:
            return False
        record = response.json().get("result", {}).get(pmid, {})
        return bool(record) and "error" not in record
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("PMID lookup failed for %s: %s", pmid, exc)
        return False


def _resolve_with_client(
    client: httpx.Client | None, check: Callable[[httpx.Client], bool]
) -> Resolvability:
    owns_client = client is None
    active = client or httpx.Client(timeout=_RESOLVE_TIMEOUT_SECONDS, trust_env=False)
    try:
        found = check(active)
    finally:
        if owns_client:
            active.close()
    return Resolvability.RESOLVABLE if found else Resolvability.UNRESOLVABLE


class ProbeCache:
    """One probe per target per run. Concurrent askers wait for the first
    probe's verdict instead of sending their own."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._verdicts: dict[tuple[str, str], Future[Resolvability]] = {}

    def verdict(self, key: tuple[str, str], probe: Callable[[], Resolvability]) -> Resolvability:
        with self._lock:
            pending = self._verdicts.get(key)
            owner = pending is None
            if pending is None:
                pending = self._verdicts[key] = Future()
        if not owner:
            return pending.result()
        try:
            result = probe()
        except BaseException as exc:
            pending.set_exception(exc)
            raise
        pending.set_result(result)
        return result


def _probe(
    probes: ProbeCache | None,
    key: tuple[str, str],
    client: httpx.Client | None,
    check: Callable[[httpx.Client], bool],
) -> Resolvability:
    def probe() -> Resolvability:
        return _resolve_with_client(client, check)

    return probe() if probes is None else probes.verdict(key, probe)


def _resolve_doi(
    doi: str, client: httpx.Client | None, probes: ProbeCache | None = None
) -> Resolvability:
    if retraction_set.is_known_retracted(doi):
        return Resolvability.RETRACTED
    # DOI names are case-insensitive at doi.org, and sources disagree on case
    # (OpenAlex lowercases, PubMed keeps the publisher's), so one probe serves both.
    url = _doi_url(doi.lower())
    return _probe(probes, ("reach", url), client, lambda c: _reachable(c, url))


def resolve_one(
    *,
    doi: str,
    pmid: str,
    url: str,
    retracted: bool,
    client: httpx.Client | None = None,
    probes: ProbeCache | None = None,
) -> Resolvability:
    """Check an independent DOI retraction set because source indexes can lag
    months. Prefer DOI, then PMID, then URL; actually dereference identifiers
    rather than trusting nonempty strings. Retraction checks run per citation;
    only the network probe is shared through `probes`.
    """
    if retracted:
        return Resolvability.RETRACTED
    if doi:
        return _resolve_doi(doi, client, probes)
    if pmid:
        return _probe(probes, ("pmid", pmid), client, lambda c: _pmid_found(c, pmid))
    if url:
        return _probe(probes, ("reach", url), client, lambda c: _reachable(c, url))
    return Resolvability.UNRESOLVABLE


def run_live_resolver() -> Resolver:
    """Live and offline checks use the same resolver seam. Probes are shared
    across one run's citations, so a paper several sources returned is
    dereferenced once."""
    probes = ProbeCache()

    def resolve(meta: CitationMetadata) -> Resolvability:
        return resolve_one(
            doi=meta.doi,
            pmid=meta.pmid,
            url=meta.url,
            retracted=meta.retracted,
            probes=probes,
        )

    return resolve


def resolve_many(metas: Iterable[CitationMetadata], *, resolver: Resolver) -> list[Resolvability]:
    """Keep bounded, input-ordered fan-out for live checks; offline
    classification shares the same resolver path.
    """
    items = list(metas)
    if not items:
        return []
    assess = functools.partial(assess_resolvability, resolver=resolver)
    with ThreadPoolExecutor(max_workers=min(_RESOLVE_CONCURRENCY, len(items))) as pool:
        return list(pool.map(assess, items))


class CitationState(str, enum.Enum):
    VERIFIED = "verified"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"


ALL_STATES: tuple[CitationState, ...] = (
    CitationState.VERIFIED,
    CitationState.PARTIAL,
    CitationState.UNSUPPORTED,
    CitationState.UNAVAILABLE,
)

STATE_RANK: dict[str, int] = {
    state.value: len(ALL_STATES) - 1 - i for i, state in enumerate(ALL_STATES)
}


def empty_citation_summary() -> dict[str, int]:
    return {state.value: 0 for state in ALL_STATES}


@dataclass
class CitationRecord:
    url: str = ""
    abstract: str = ""
    claim: str = ""  # the inline claim cited from this source
    available: bool = True


@functools.lru_cache(maxsize=256)
def _content_tokens(text: str) -> frozenset[str]:
    """Claims repeat across citations, so cache tokenization rather than repeat
    it per source.
    """
    # Match words rather than splitting on whitespace: attached punctuation
    # made a document that states the claim score lower than the same words
    # written bare, which docs/OPERATIONS.md requires to reach the top band.
    # Drop short function words so grammatical overlap cannot inflate apparent
    # claim support.
    return frozenset(tokenize(text, min_len=4))


def _token_overlap(claim: str, abstract: str) -> float:
    """Use claim-token coverage, not Jaccard: abstract length must not cap
    perfect support for a short claim.
    """
    if not claim or not abstract:
        return 0.0
    a = _content_tokens(claim)
    b = _content_tokens(abstract)
    return coverage(a, b)


def classify_citation(record: CitationRecord) -> CitationState:
    """Coverage thresholds differ from Jaccard thresholds; copying the latter
    would make upper support states unreachable.
    """
    if not record.available or not record.url:
        return CitationState.UNAVAILABLE
    overlap = _token_overlap(record.claim, record.abstract)
    if overlap >= 0.60:
        return CitationState.VERIFIED
    if overlap >= 0.30:
        return CitationState.PARTIAL
    return CitationState.UNSUPPORTED
