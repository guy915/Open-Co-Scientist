from __future__ import annotations

import dataclasses
import math
from collections import Counter
from typing import Any

from co_scientist.core.text_matching import tokenize

ATTACHMENT_SOURCE = "attachment"


def _tokenize(text: str) -> list[str]:
    return list(tokenize(text, min_len=3))


@dataclasses.dataclass(frozen=True)
class CorpusDocument:
    doc_id: str
    title: str
    text: str
    source: str = "attachment"


@dataclasses.dataclass(frozen=True)
class RetrievedDocument:
    document: CorpusDocument
    score: float


class KeywordCorpusRetriever:
    def __init__(self, documents: list[CorpusDocument]) -> None:
        self._documents = documents
        self._doc_tokens = {d.doc_id: _tokenize(d.text) for d in documents}
        self._tf = {doc_id: Counter(tokens) for doc_id, tokens in self._doc_tokens.items()}
        n = len(documents) or 1
        df: Counter[str] = Counter()
        for tokens in self._doc_tokens.values():
            df.update(set(tokens))
        self._idf = {term: math.log(1 + n / (1 + count)) for term, count in df.items()}

    def _score(self, query_terms: list[str], doc_id: str) -> float:
        tf = self._tf[doc_id]
        length = len(self._doc_tokens[doc_id]) or 1
        score = 0.0
        for term in query_terms:
            if term in tf:
                score += (tf[term] / length) * self._idf.get(term, 0.0)
        return score

    def retrieve(self, query: str, k: int = 5) -> list[RetrievedDocument]:
        query_terms = _tokenize(query)
        scored = [
            RetrievedDocument(doc, self._score(query_terms, doc.doc_id)) for doc in self._documents
        ]
        hits = [r for r in scored if r.score > 0.0]
        hits.sort(key=lambda r: (-r.score, r.document.doc_id))
        return hits[:k]


def corpus_from_evidence(
    evidence_rows: list[dict[str, Any]],
) -> list[CorpusDocument]:
    return [
        CorpusDocument(
            doc_id=str(row.get("id")),
            title=str(row.get("title") or ""),
            text=str(row.get("abstract") or ""),
            source=ATTACHMENT_SOURCE,
        )
        for row in evidence_rows
        if row.get("source") == ATTACHMENT_SOURCE
    ]


def _context_source(document: CorpusDocument, excerpt_chars: int) -> dict[str, Any]:
    excerpt = document.text[:excerpt_chars]
    return {
        "display": f"Private scientist source '{document.title}': {excerpt}",
        "tool_id": "private_corpus",
        "source_type": "private_document",
        "data": {
            "document_id": document.doc_id,
            "title": document.title,
            "excerpt": excerpt,
            "private": True,
        },
    }


def engine_context_sources(
    evidence_rows: list[dict[str, Any]],
    research_goal: str,
    *,
    max_documents: int = 20,
    excerpt_chars: int = 6000,
) -> list[dict[str, Any]]:
    documents = corpus_from_evidence(evidence_rows)
    hits = KeywordCorpusRetriever(documents).retrieve(research_goal, k=max_documents)
    # No keyword overlap must not silently discard explicitly supplied context;
    # retain the small private corpus deterministically.
    selected = [hit.document for hit in hits]
    if not selected:
        selected = sorted(documents, key=lambda item: item.doc_id)[:max_documents]
    return [_context_source(document, excerpt_chars) for document in selected]
