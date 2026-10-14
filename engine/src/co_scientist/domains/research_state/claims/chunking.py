from __future__ import annotations

import itertools
import re
from collections.abc import Sequence

from co_scientist.domains.research_state.claims.assessor import SENTENCE_SPLIT, EvidencePassage

# Five retrieved passages should fit dense paragraphs rather than funding five
# whole papers.
CHUNK_MAX_CHARS = 1200

# Overlap keeps boundary support sentences intact; total length can exceed the
# packing target by the prefix and joining space.
CHUNK_OVERLAP_CHARS = 150

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")

# Only an all-digit suffix is a chunk index; legitimate article IDs containing #
# remain article-level.
_CHUNK_SUFFIX = re.compile(r"#(\d+)$")


def parent_evidence_id(evidence_id: str) -> str:
    """Located chunk spans must resolve to their parent article before
    reports join them to stored evidence.
    """
    match = _CHUNK_SUFFIX.search(evidence_id)
    return evidence_id[: match.start()] if match else evidence_id


def _split_long_unit(unit: str, max_chars: int) -> list[str]:
    sentences = [s.strip() for s in SENTENCE_SPLIT.split(unit) if s.strip()]
    windows: list[str] = []
    for sentence in sentences or [unit]:
        if len(sentence) <= max_chars:
            windows.append(sentence)
        else:
            windows.extend(sentence[i : i + max_chars] for i in range(0, len(sentence), max_chars))
    return windows


def _pack_units(units: Sequence[str], max_chars: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current} {unit}".strip() if current else unit
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = unit
    if current:
        chunks.append(current)
    return chunks


def _add_overlap(chunks: list[str], overlap: int) -> list[str]:
    if overlap <= 0 or len(chunks) <= 1:
        return chunks
    overlapped = [chunks[0]]
    for previous, current in itertools.pairwise(chunks):
        overlapped.append(f"{previous[-overlap:]} {current}".strip())
    return overlapped


def chunk_text(
    text: str,
    *,
    max_chars: int = CHUNK_MAX_CHARS,
    overlap: int = CHUNK_OVERLAP_CHARS,
) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []
    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT.split(stripped) if p.strip()] or [stripped]
    units: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            units.append(paragraph)
        else:
            units.extend(_split_long_unit(paragraph, max_chars))
    return _add_overlap(_pack_units(units, max_chars), overlap)


def chunk_evidence_passage(
    article_id: str,
    *,
    head_text: str,
    body_text: str,
    source: str,
    url: str,
) -> list[EvidencePassage]:
    """Keep title/abstract intact for reliable retrieval; abstract-only
    articles retain their bare identity rather than introducing synthetic
    chunk IDs.
    """
    head = head_text.strip()
    all_chunks = ([head] if head else []) + chunk_text(body_text)
    if len(all_chunks) <= 1:
        return [
            EvidencePassage(
                evidence_id=article_id,
                text=all_chunks[0] if all_chunks else "",
                source=source,
                url=url,
            )
        ]
    return [
        EvidencePassage(evidence_id=f"{article_id}#{i}", text=chunk, source=source, url=url)
        for i, chunk in enumerate(all_chunks)
    ]
