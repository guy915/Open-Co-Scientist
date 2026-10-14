"""Pool heading signals across the document to keep levels consistent;
bookmarks take precedence over numbering and font styles.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Hashable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, TypeVar

_Key = TypeVar("_Key", bound=Hashable)
_Rank = TypeVar("_Rank", bound=Hashable)


def compress_to_levels(raw: dict[_Key, _Rank]) -> dict[_Key, int]:
    if not raw:
        return {}
    # mypy knows _Rank as Hashable, but every rank produced here is an orderable
    # integer or integer/boolean tuple.
    ordered = sorted(set(raw.values()))  # type: ignore[type-var]
    rank_to_level = {rank: level for level, rank in enumerate(ordered, start=1)}
    return {key: rank_to_level[rank] for key, rank in raw.items()}


# Weak bookmark matches are worse than leaving a title unmatched.
_MATCH_THRESHOLD = 0.72
MAX_OUTLINE_ENTRIES = 2_000
MAX_PAGE_BOOKMARKS = 64
MAX_PAGE_LINES = 5_000
MAX_HEADING_COMPARISONS = 20_000
MAX_OUTLINE_DEPTH = 32


class HeadingBudgetExceededError(ValueError):
    pass


@dataclass
class _HeadingBudget:
    remaining: int = MAX_HEADING_COMPARISONS

    def compare(self) -> None:
        if self.remaining <= 0:
            raise HeadingBudgetExceededError("PDF heading comparison budget exceeded")
        self.remaining -= 1


_LEADING_MARKER = re.compile(r"^\s*(?:\(?[0-9]+(?:\.[0-9]+)*[).]?|\(?[A-Za-z]{1,2}[).])[\s.:)\-]*")


def _norm(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", text.lower()).strip()
    return re.sub(r"^[\W_]+|[\W_]+$", "", collapsed)


def _match_score(line: str, title: str) -> float:
    variants_a = {_norm(line), _norm(_LEADING_MARKER.sub("", line))} - {""}
    variants_b = {_norm(title), _norm(_LEADING_MARKER.sub("", title))} - {""}
    best = 0.0
    for a in variants_a:
        for b in variants_b:
            best = max(best, SequenceMatcher(None, a, b).ratio())
            if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
                best = max(best, 0.92)
    return best


def _best_match(
    title: str, lines: list[str], claimed: set[int], budget: _HeadingBudget
) -> int | None:
    best_index: int | None = None
    best_score = _MATCH_THRESHOLD
    for index, line in enumerate(lines):
        if index in claimed or len(line) > MAX_HEADING_CHARS:
            continue
        budget.compare()
        score = _match_score(line, title)
        if score > best_score:
            best_index, best_score = index, score
    return best_index


def raw_bookmark_matches(
    outline: list[tuple[str, int]], lines: list[str], budget: _HeadingBudget | None = None
) -> dict[int, int]:
    if len(outline) > MAX_PAGE_BOOKMARKS or len(lines) > MAX_PAGE_LINES:
        raise HeadingBudgetExceededError("PDF page heading budget exceeded")
    budget = budget or _HeadingBudget()
    claimed: set[int] = set()
    matches: dict[int, int] = {}
    exact: dict[str, list[int]] = {}
    for line_index, line in enumerate(lines):
        if len(line) <= MAX_HEADING_CHARS:
            exact.setdefault(_norm(line), []).append(line_index)
    for title, depth in outline:
        if len(title) > MAX_HEADING_CHARS:
            raise HeadingBudgetExceededError("PDF bookmark title budget exceeded")
        if not title.strip():
            continue
        index = next((i for i in exact.get(_norm(title), []) if i not in claimed), None)
        if index is None:
            index = _best_match(title, lines, claimed, budget)
        if index is not None:
            claimed.add(index)
            matches[index] = depth
    return matches


_FAMILY_ORDER = [
    "part",
    "chapter",
    "article",
    "roman_u",
    "arabic",
    "alpha_u",
    "alpha_l",
    "roman_l",
]

# Single Roman letters can also be alphabetic markers; document context must
# resolve the ambiguity.
_ROMAN_SINGLES = set("IVXLCDMivxlcdm")

_ROMAN_RE = re.compile(
    r"^M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$",
    re.IGNORECASE,
)
_KW_PART = re.compile(r"^(part|title|book)\b", re.IGNORECASE)
_KW_CHAPTER = re.compile(r"^chapter\b", re.IGNORECASE)
_KW_ARTICLE = re.compile(
    r"^(?:(?:article|section|clause|schedule|annex|appendix|rule)\b"
    r"|§+\s*\d)",
    re.IGNORECASE,
)
# Require punctuation or whitespace after dotted numbering so unit-like text
# such as 1.1x is not a heading marker.
_DOTTED = re.compile(r"^(\d+(?:\.\d+)+)(?:[.)\]\s]|$)")
_ARABIC = re.compile(r"^(\d+)[.)]")
_LETTER = re.compile(r"^\(?\s*([A-Za-z]+)\s*[).]")


@dataclass
class _Marker:
    family: str
    depth: int = 1  # dotted-decimal segment count; 1 for every other kind
    token: str | None = None  # raw letter token, kept for ambiguity resolution
    ambiguous: bool = False


def _is_roman(token: str) -> bool:
    return bool(token) and _ROMAN_RE.fullmatch(token) is not None


def _classify_letter(token: str) -> _Marker | None:
    upper = token.isupper()
    if len(token) == 1:
        if token in _ROMAN_SINGLES:
            family = "roman_u" if upper else "roman_l"
            return _Marker(family=family, token=token, ambiguous=True)
        return _Marker(family="alpha_u" if upper else "alpha_l", token=token)
    # Multi-letter tokens must be genuine Roman numerals, otherwise ordinary
    # words can masquerade as numbering.
    if _is_roman(token):
        return _Marker(family="roman_u" if upper else "roman_l", token=token)
    return None


def _match_keyword(stripped: str) -> _Marker | None:
    if _KW_PART.match(stripped):
        return _Marker(family="part")
    if _KW_CHAPTER.match(stripped):
        return _Marker(family="chapter")
    if _KW_ARTICLE.match(stripped):
        return _Marker(family="article")
    return None


def _match_dotted(stripped: str) -> _Marker | None:
    match = _DOTTED.match(stripped)
    if match is None:
        return None
    return _Marker(family="dotted", depth=match.group(1).count(".") + 1)


def _match_arabic(stripped: str) -> _Marker | None:
    if _ARABIC.match(stripped):
        return _Marker(family="arabic")
    return None


def _match_letter(stripped: str) -> _Marker | None:
    match = _LETTER.match(stripped)
    if match is None:
        return None
    return _classify_letter(match.group(1))


def _parse_marker(text: str) -> _Marker | None:
    stripped = text.strip()
    if not stripped:
        return None
    for classify in (
        _match_keyword,
        _match_dotted,
        _match_arabic,
        _match_letter,
    ):
        marker = classify(stripped)
        if marker is not None:
            return marker
    return None


def _has_unambiguous(markers: list[_Marker | None], family: str) -> bool:
    return any(m is not None and not m.ambiguous and m.family == family for m in markers)


def _resolve_one_ambiguous(marker: _Marker, has_roman: bool, has_alpha: bool) -> None:
    upper = marker.token is not None and marker.token.isupper()
    if has_roman and not has_alpha:
        roman = True
    elif has_alpha and not has_roman:
        roman = False
    else:
        roman = marker.token in ("I", "i")
    marker.family = (
        ("roman_u" if roman else "alpha_u") if upper else ("roman_l" if roman else "alpha_l")
    )
    marker.ambiguous = False


def _resolve_ambiguous(markers: list[_Marker | None]) -> None:
    upper_roman = _has_unambiguous(markers, "roman_u")
    upper_alpha = _has_unambiguous(markers, "alpha_u")
    lower_roman = _has_unambiguous(markers, "roman_l")
    lower_alpha = _has_unambiguous(markers, "alpha_l")
    for marker in markers:
        if marker is None or not marker.ambiguous:
            continue
        is_upper = marker.token is not None and marker.token.isupper()
        has_roman = upper_roman if is_upper else lower_roman
        has_alpha = upper_alpha if is_upper else lower_alpha
        _resolve_one_ambiguous(marker, has_roman, has_alpha)


def _family_rank(family: str) -> int:
    key = "arabic" if family == "dotted" else family
    try:
        return _FAMILY_ORDER.index(key)
    except ValueError:
        return len(_FAMILY_ORDER)


def infer_numbering_levels(lines: list[str]) -> dict[int, int]:
    markers = [_parse_marker(line) for line in lines]
    _resolve_ambiguous(markers)

    keys: dict[int, tuple[int, int]] = {
        index: (_family_rank(marker.family), marker.depth)
        for index, marker in enumerate(markers)
        if marker is not None
    }
    return compress_to_levels(keys)


# Cluster nearly equal font sizes so measurement jitter cannot manufacture
# heading levels.
_SIZE_TOLERANCE = 0.08

# Long large-font text is more likely a pull quote or table cell than a section
# heading.
MAX_HEADING_CHARS = 120


@dataclass(frozen=True)
class LineStyle:
    size: float
    bold: bool
    all_caps: bool


def _is_all_caps(text: str) -> bool:
    letters = [char for char in text if char.isalpha()]
    return len(letters) >= 4 and all(char.isupper() for char in letters)


def _is_bold(font_dict: Any) -> bool:
    if not isinstance(font_dict, dict):
        return False
    base_font = str(font_dict.get("/BaseFont", ""))
    lowered = base_font.lower()
    return "bold" in lowered or "black" in lowered or "heavy" in lowered


def _effective_size(font_size: float, tm_matrix: list[float]) -> float:
    scale = math.hypot(tm_matrix[1], tm_matrix[3])
    return font_size * (scale or 1.0)


class _LineCollector:
    """Only embedded line breaks end a line; font and positioning changes
    must not fragment a heading.
    """

    def __init__(self) -> None:
        self.lines: list[tuple[str, float, bool]] = []
        self._text = ""
        self._max_size = 0.0
        self._bold_chars = 0
        self._plain_chars = 0

    def _flush_line(self) -> None:
        if self._text.strip():
            bold = self._bold_chars >= self._plain_chars
            self.lines.append((self._text, self._max_size, bold))
        self._text = ""
        self._max_size = 0.0
        self._bold_chars = 0
        self._plain_chars = 0

    def __call__(
        self,
        text: str,
        _cm_matrix: list[float],
        tm_matrix: list[float],
        font_dict: Any,
        font_size: float,
    ) -> None:
        size = _effective_size(font_size, tm_matrix)
        bold = _is_bold(font_dict)
        segments = text.split("\n")
        for index, segment in enumerate(segments):
            if segment:
                self._text += segment
                self._max_size = max(self._max_size, size)
                if bold:
                    self._bold_chars += len(segment)
                else:
                    self._plain_chars += len(segment)
            if index < len(segments) - 1:
                self._flush_line()


def collect_line_styles(page: Any) -> dict[str, LineStyle]:
    collector = _LineCollector()
    page.extract_text(visitor_text=collector)
    collector._flush_line()
    styles: dict[str, LineStyle] = {}
    for text, size, bold in collector.lines:
        stripped = text.strip()
        if not stripped or len(stripped) > MAX_HEADING_CHARS:
            continue
        styles.setdefault(
            stripped,
            LineStyle(size=size, bold=bold, all_caps=_is_all_caps(stripped)),
        )
    return styles


def _cluster_sizes(sizes: set[float]) -> dict[float, int]:
    clusters: dict[float, int] = {}
    index = 0
    previous: float | None = None
    for size in sorted(sizes, reverse=True):
        gap = previous - size if previous is not None else 0.0
        if previous is not None and gap > _SIZE_TOLERANCE * previous:
            index += 1
        clusters[size] = index
        previous = size
    return clusters


def rank_heading_styles(styles: dict[int, LineStyle], body_size: float) -> dict[int, int]:
    candidates = {index: style for index, style in styles.items() if style.size > body_size}
    if not candidates:
        return {}

    clusters = _cluster_sizes({style.size for style in candidates.values()})
    keys = {
        index: (clusters[style.size], not style.bold, not style.all_caps)
        for index, style in candidates.items()
    }
    return compress_to_levels(keys)


# Flatten rare inferred levels beyond h3 rather than complicating the heading
# cascade.
_MAX_LEVEL = 3


def _walk_outline(
    reader: Any, items: Any, depth: int, out: list[tuple[str, int, int]], budget: _HeadingBudget
) -> None:
    if depth > MAX_OUTLINE_DEPTH:
        raise HeadingBudgetExceededError("PDF outline depth budget exceeded")
    for item in items:
        budget.compare()
        if isinstance(item, list):
            _walk_outline(reader, item, depth + 1, out, budget)
            continue
        title = getattr(item, "title", None)
        if not title:
            continue
        try:
            page_index = reader.get_destination_page_number(item)
        except Exception:  # a dangling /Dest reference costs one bookmark
            continue
        out.append((str(title), depth, page_index))


def flatten_outline(reader: Any) -> list[tuple[str, int, int]]:
    outline = reader.outline
    if not outline:
        return []
    out: list[tuple[str, int, int]] = []
    _walk_outline(reader, outline, 1, out, _HeadingBudget(MAX_OUTLINE_ENTRIES))
    return out


def _pool_bookmark_levels(
    outline: list[tuple[str, int, int]], lines_per_page: list[list[str]]
) -> dict[tuple[int, int], int]:
    raw: dict[tuple[int, int], int] = {}
    budget = _HeadingBudget()
    for page_index, lines in enumerate(lines_per_page):
        page_outline = [(title, depth) for title, depth, page in outline if page == page_index]
        if not page_outline:
            continue
        matches = raw_bookmark_matches(page_outline, lines, budget)
        for line_index, depth in matches.items():
            raw[(page_index, line_index)] = depth
    return compress_to_levels(raw)


def _pool_numbering_levels(
    lines_per_page: list[list[str]],
) -> dict[tuple[int, int], int]:
    positions = [
        (page_index, line_index, line.strip())
        for page_index, lines in enumerate(lines_per_page)
        for line_index, line in enumerate(lines)
        if line.strip() and len(line.strip()) <= MAX_HEADING_CHARS
    ]
    levels = infer_numbering_levels([text for *_, text in positions])
    return {(positions[i][0], positions[i][1]): level for i, level in levels.items()}


def _pool_style_levels(
    pages: list[Any], lines_per_page: list[list[str]]
) -> dict[tuple[int, int], int]:
    per_page_styles = [collect_line_styles(page) for page in pages]
    all_sizes: list[float] = []
    for styles in per_page_styles:
        all_sizes.extend(style.size for style in styles.values())
    if not all_sizes:
        return {}
    body_size = Counter(round(size, 1) for size in all_sizes).most_common(1)[0][0]
    candidates = [
        ((page_index, line_index), style)
        for page_index, lines in enumerate(lines_per_page)
        for line_index, line in enumerate(lines)
        if (style := per_page_styles[page_index].get(line.strip())) is not None
    ]
    ranked = rank_heading_styles(
        {index: style for index, (_, style) in enumerate(candidates)}, body_size
    )
    return {candidates[index][0]: level for index, level in ranked.items()}


def _combine_levels(
    bookmark: dict[tuple[int, int], int],
    numbering: dict[tuple[int, int], int],
    style: dict[tuple[int, int], int],
) -> dict[tuple[int, int], int]:
    combined = {**style, **numbering, **bookmark}
    return {key: max(1, min(level, _MAX_LEVEL)) for key, level in combined.items()}


def _rewrite_lines(
    lines_per_page: list[list[str]], levels: dict[tuple[int, int], int]
) -> list[str]:
    pages_out = []
    for page_index, lines in enumerate(lines_per_page):
        rewritten = list(lines)
        for line_index, line in enumerate(lines):
            level = levels.get((page_index, line_index))
            if level is not None:
                rewritten[line_index] = "#" * level + " " + line.strip()
        pages_out.append("\n".join(rewritten))
    return pages_out


def apply_heading_markup(reader: Any, pages: list[Any], pages_text: list[str]) -> list[str]:
    lines_per_page = [text.split("\n") for text in pages_text]
    outline = flatten_outline(reader)
    bookmark_levels = _pool_bookmark_levels(outline, lines_per_page)
    numbering_levels = _pool_numbering_levels(lines_per_page)
    style_levels = _pool_style_levels(pages, lines_per_page)
    levels = _combine_levels(bookmark_levels, numbering_levels, style_levels)
    return _rewrite_lines(lines_per_page, levels)
