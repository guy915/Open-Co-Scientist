from collections.abc import Callable
from typing import Any

import mcp_server.entrez as entrez_rate_limit
import pytest
from Bio import Entrez


class CannedEntrezHandle:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def close(self) -> None:
        pass


def install_entrez(monkeypatch: pytest.MonkeyPatch, **requests: Callable[..., Any]) -> None:
    monkeypatch.setattr(entrez_rate_limit, "_await_slot", lambda: None)
    for name, request in requests.items():
        monkeypatch.setattr(Entrez, name, request)
    monkeypatch.setattr(Entrez, "read", lambda handle: handle.payload)


def _ids(request_ids: Any) -> list[str]:
    if isinstance(request_ids, str):
        return request_ids.split(",")
    return [str(paper_id) for paper_id in request_ids]


def efetch_by_id(responses: Callable[[str], Any]) -> Callable[..., CannedEntrezHandle]:
    """Answers a batched efetch with each PMID's canned response, stamped with
    its PMID as PubMed does."""

    def efetch(**kwargs: Any) -> CannedEntrezHandle:
        merged: dict[str, list[Any]] = {"PubmedArticle": [], "PubmedBookArticle": []}
        for paper_id in _ids(kwargs["id"]):
            response = responses(paper_id)
            if not isinstance(response, dict):
                continue
            for article in response.get("PubmedArticle") or []:
                citation = {**article["MedlineCitation"], "PMID": paper_id}
                merged["PubmedArticle"].append({**article, "MedlineCitation": citation})
            for book in response.get("PubmedBookArticle") or []:
                document = {**book.get("BookDocument", {}), "PMID": paper_id}
                merged["PubmedBookArticle"].append({**book, "BookDocument": document})
        return CannedEntrezHandle(merged)

    return efetch


def elink_by_id(links: Callable[[str], str | None]) -> Callable[..., CannedEntrezHandle]:
    def elink(**kwargs: Any) -> CannedEntrezHandle:
        link_sets = []
        for paper_id in _ids(kwargs["id"]):
            pmc_id = links(paper_id)
            link_db = [{"LinkName": "pubmed_pmc", "Link": [{"Id": pmc_id}]}] if pmc_id else []
            link_sets.append({"IdList": [paper_id], "LinkSetDb": link_db})
        return CannedEntrezHandle(link_sets)

    return elink
