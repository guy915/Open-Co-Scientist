from dataclasses import dataclass
from typing import Any

from mcp_server.text_extraction import clean_markup


@dataclass(frozen=True)
class PubmedRecord:
    title: str
    abstract: str | None
    doi: str | None
    authors: list[str]
    publication: str | None
    year: int | None
    date_revised: str | None
    publication_types: list[str]

    def fulltext_metadata(self, pmc_id: str | None) -> dict[str, Any]:
        return {
            "date_revised": self.date_revised,
            "title": self.title,
            "abstract": self.abstract,
            "doi": self.doi,
            "authors": self.authors,
            "publication": self.publication,
            "pmc_full_text_id": pmc_id,
            "publication_types": self.publication_types,
        }


def records_by_pmid(results: Any) -> dict[str, dict[str, Any] | None]:
    """None marks a book record; a response with neither kind fails. A record
    without a PMID cannot be matched to its request, so it is left out."""
    if isinstance(results, dict) and (
        results.get("PubmedArticle") or results.get("PubmedBookArticle")
    ):
        records: dict[str, dict[str, Any] | None] = {}
        # Book records (StatPearls, GeneReviews) carry no journal metadata.
        for book in results.get("PubmedBookArticle") or []:
            if pmid := (book.get("BookDocument") or {}).get("PMID"):
                records[str(pmid)] = None
        for article in results.get("PubmedArticle") or []:
            if pmid := (article.get("MedlineCitation") or {}).get("PMID"):
                records[str(pmid)] = dict(article)
        return records
    raise ValueError("PubMed returned no article record")


def parse_pubmed_record(raw: dict[str, Any]) -> PubmedRecord:
    citation = raw["MedlineCitation"]
    article = citation["Article"]
    journal = article.get("Journal") or {}
    year_text = journal.get("JournalIssue", {}).get("PubDate", {}).get("Year")
    try:
        year = int(year_text) if year_text else None
    except (TypeError, ValueError):
        year = None
    revised = citation.get("DateRevised") or {}
    date_revised = (
        "/".join(str(revised[key]) for key in ("Year", "Month", "Day"))
        if all(key in revised for key in ("Year", "Month", "Day"))
        else None
    )
    parts = article.get("Abstract", {}).get("AbstractText") or []
    abstract = clean_markup(" ".join(str(part) for part in parts)) if parts else None
    authors = [
        f"{author['ForeName']} {author['LastName']}"
        for author in article.get("AuthorList") or []
        if isinstance(author, dict) and author.get("ForeName") and author.get("LastName")
    ]
    doi = next(
        (
            str(identifier)
            for identifier in raw.get("PubmedData", {}).get("ArticleIdList") or []
            if getattr(identifier, "attributes", {}).get("IdType") == "doi"
            and str(identifier) not in {"", "<not found>"}
        ),
        None,
    )
    return PubmedRecord(
        title=clean_markup(article.get("ArticleTitle")) or "Unknown",
        abstract=abstract,
        doi=doi,
        authors=authors,
        publication=journal.get("Title"),
        year=year,
        date_revised=date_revised,
        publication_types=[str(item) for item in article.get("PublicationTypeList") or []],
    )
