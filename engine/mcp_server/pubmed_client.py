from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from Bio import Entrez

from mcp_server.entrez import entrez_call, initialize_entrez, read_entrez
from mcp_server.log_privacy import failure_summary
from mcp_server.pubmed_records import PubmedRecord, parse_pubmed_record, records_by_pmid

logger = logging.getLogger(__name__)

# Clamp the sufficiency threshold to retmax so tiny requests cannot force
# impossible relaxation.
MIN_RESULTS_BEFORE_RELAX = 3

# PubMed reserves uppercase AND/OR/NOT; lowercase forms are ordinary search terms
# (NCBI PubMed Help).
_BOOLEAN_OPERATORS = frozenset({"AND", "OR", "NOT"})

EsearchFn = Callable[[str, int, int], list[str]]


def _field_tagged_term(term: str) -> str:
    """Field tags bypass automatic term mapping, which silently splits
    unmatched phrases into ANDed words.
    """
    return f"({term}[tiab] OR {term}[mesh])"


# Two leading terms anchor the topic; one is broad, while three can reproduce
# zero-hit conjunctions.
_ANCHOR_TERMS = 2


def anchored_relaxed_query(query: str) -> str | None:
    """Leading terms carry the subject; relaxing them would answer a
    different question.
    """
    tokens = query.split()
    if any(token in _BOOLEAN_OPERATORS for token in tokens):
        return None
    if len(tokens) <= _ANCHOR_TERMS:
        return None
    anchors = " AND ".join(_field_tagged_term(term) for term in tokens[:_ANCHOR_TERMS])
    loosened = " OR ".join(_field_tagged_term(term) for term in tokens[_ANCHOR_TERMS:])
    return f"{anchors} AND ({loosened})"


def or_relaxed_query(query: str) -> str | None:
    tokens = query.split()
    if any(token in _BOOLEAN_OPERATORS for token in tokens):
        return None
    if len(tokens) < 2:
        return None
    return " OR ".join(_field_tagged_term(term) for term in tokens)


def relaxation_ladder(query: str, recency_years: int = 0) -> list[tuple[str, int]]:
    """Keep the recency-only rung untagged; anchor the subject before
    broadening every term.
    """
    ladder: list[tuple[str, int]] = [(query, recency_years)]
    if recency_years > 0:
        ladder.append((query, 0))
    for rung in (anchored_relaxed_query(query), or_relaxed_query(query)):
        if rung is not None and rung not in {term for term, _ in ladder}:
            ladder.append((rung, 0))
    return ladder


def search_with_relaxation(
    query: str,
    retmax: int,
    recency_years: int,
    esearch: EsearchFn,
) -> list[str]:
    """Keep precise-rung hits when broader searches supply enough results;
    some evidence beats none.
    """
    validate_paper_count(retmax)
    threshold = min(MIN_RESULTS_BEFORE_RELAX, retmax)
    merged: list[str] = []
    for term, recency in relaxation_ladder(query, recency_years):
        ids = esearch(term, retmax, recency)
        if len(ids) > retmax:
            raise ValueError("PubMed returned more identifiers than requested")
        merged.extend(i for i in dict.fromkeys(ids) if i not in merged)
        del merged[retmax:]
        if len(ids) >= threshold:
            return merged
    return merged


PUBMED_SEARCH_SORT = "pub_date"

# NCBI asks for at most 200 UIDs per E-utility request.
EUTILS_BATCH_SIZE = 200
MAX_PUBMED_PAPERS = 400
MAX_FULLTEXT_PAPERS = 20


def validate_paper_count(count: int, maximum: int = MAX_PUBMED_PAPERS) -> None:
    if type(count) is not int or not 1 <= count <= maximum:
        raise ValueError(f"max_papers must be an integer between 1 and {maximum}")


def _batches(paper_ids: Sequence[str]) -> list[list[str]]:
    if len(paper_ids) > MAX_PUBMED_PAPERS:
        raise ValueError("PubMed identifier budget exceeded")
    unique = list(dict.fromkeys(paper_ids))
    return [unique[i : i + EUTILS_BATCH_SIZE] for i in range(0, len(unique), EUTILS_BATCH_SIZE)]


def fetch_pubmed_records(paper_ids: Sequence[str]) -> dict[str, PubmedRecord | None]:
    """None marks a book record. An ID PubMed did not answer for, or whose
    record failed to parse, is absent, so callers count it as failed."""
    records: dict[str, PubmedRecord | None] = {}
    for batch in _batches(paper_ids):
        answered = records_by_pmid(read_entrez(entrez_call(Entrez.efetch, db="pubmed", id=batch)))
        for paper_id in batch:
            if paper_id not in answered:
                continue
            raw = answered[paper_id]
            try:
                records[paper_id] = None if raw is None else parse_pubmed_record(raw)
            except Exception as exc:
                # A malformed paper must not discard successful siblings.
                logger.warning("PubMed metadata fetch failed (%s)", failure_summary(exc))
    return records


def _pmc_link(link_set: Any) -> str | None:
    for link_db in link_set.get("LinkSetDb") or []:
        if link_db.get("LinkName", "pubmed_pmc") == "pubmed_pmc" and link_db.get("Link"):
            return str(link_db["Link"][0]["Id"])
    return None


def fetch_pmc_ids(paper_ids: Sequence[str]) -> dict[str, str | None]:
    """A list `id` makes ELink answer one link set per PMID. `pubmed_pmc` is
    the article's own PMC copy; unnamed PMC links include papers citing it."""
    links: dict[str, str | None] = {}
    for batch in _batches(paper_ids):
        try:
            link_sets = read_entrez(
                entrez_call(
                    Entrez.elink, dbfrom="pubmed", db="pmc", linkname="pubmed_pmc", id=batch
                )
            )
            for link_set in link_sets:
                source_ids = link_set.get("IdList") or []
                if len(source_ids) == 1 and str(source_ids[0]) in batch:
                    links[str(source_ids[0])] = _pmc_link(link_set)
        except Exception:
            logger.debug("fulltext not available in pmc")
    return {paper_id: links.get(paper_id) for paper_id in dict.fromkeys(paper_ids)}


initialize_entrez()


def _apply_recency_filter(search_params: dict[str, Any], recency_years: int) -> None:
    if recency_years <= 0:
        return
    from datetime import datetime

    current_year = datetime.now().year
    min_year = current_year - recency_years
    search_params["mindate"] = f"{min_year}/01/01"
    search_params["maxdate"] = f"{current_year}/12/31"
    search_params["datetype"] = "pdat"
    logger.debug(
        "applying recency filter: %s-%s (last %s years)",
        min_year,
        current_year,
        recency_years,
    )


class _EntrezClient:
    def __init__(self, qualified_path: Path):
        self.qualified_path = qualified_path

    def entrez_read(self, handle: Any) -> Any:
        return read_entrez(handle)

    def _fetch_papers_details(self, paper_ids: Sequence[str]) -> dict[str, dict[str, Any] | None]:
        """Blocking Entrez requests and XML parsing must run off the event
        loop. None marks a book record; a failed ID is absent.
        """
        records = fetch_pubmed_records(paper_ids)
        journal_ids = [paper_id for paper_id, record in records.items() if record is not None]
        pmc_ids = fetch_pmc_ids(journal_ids) if journal_ids else {}
        return {
            paper_id: None if record is None else record.fulltext_metadata(pmc_ids.get(paper_id))
            for paper_id, record in records.items()
        }

    def _esearch_ids(self, query: str, retmax: int, recency_years: int) -> list[str]:
        search_params: dict[str, Any] = {
            "db": "pubmed",
            "term": query,
            "retmax": retmax,
            "sort": PUBMED_SEARCH_SORT,
        }
        _apply_recency_filter(search_params, recency_years)
        logger.debug("searching pubmed with sort=%s", PUBMED_SEARCH_SORT)
        results = self.entrez_read(entrez_call(Entrez.esearch, **search_params))
        if id_list := results.get("IdList", None):
            return [str(paper_id) for paper_id in id_list]
        return []

    def pubmed_search_ids(
        self,
        query: str,
        retmax: int = 10,
        recency_years: int = 0,
    ) -> list[str]:
        ids = search_with_relaxation(query, retmax, recency_years, self._esearch_ids)
        if not ids:
            logger.warning("PubMed search returned no results")
        return ids
