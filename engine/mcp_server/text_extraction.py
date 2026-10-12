import html
import logging
import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from mcp_server.log_privacy import failure_summary

logger = logging.getLogger(__name__)

# Block-level markup becomes a space: "<h4>Aims</h4>The convergence" must
# not close up into "AimsThe".
_BLOCK_TAG_RE = re.compile(r"</?(?:p|br|div|h[1-6])\s*/?>", re.IGNORECASE)

# Inline markup closes up instead: bla<sub>NDM-1</sub> is one gene name,
# and splitting it would break the string a citation is matched on.
_INLINE_TAG_RE = re.compile(
    r"</?(?:i|b|em|strong|u|sup|sub|sc|italic|bold|underline)\s*/?>",
    re.IGNORECASE,
)

# Europe PMC zero-width separators are wrapping hints, not word boundaries or
# model-visible whitespace.
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200d\ufeff]")

_WHITESPACE_RE = re.compile(r"\s+")


def clean_markup(raw: Any) -> str:
    if not isinstance(raw, str) or not raw:
        return ""
    text = _BLOCK_TAG_RE.sub(" ", html.unescape(raw))
    text = _ZERO_WIDTH_RE.sub("", _INLINE_TAG_RE.sub("", text))
    return _WHITESPACE_RE.sub(" ", text).strip()


def _build_section_block(section: Tag) -> str | None:
    if section.parent is None or section.parent.name == "sec":
        return None
    paragraphs = [
        text
        for paragraph in section.find_all("p", recursive=False)
        if (text := paragraph.get_text(strip=True))
    ]
    for child in section.children:
        if isinstance(child, Tag) and child.name not in (
            "sec",
            "title",
            "label",
        ):
            paragraphs.extend(
                text
                for paragraph in child.find_all("p")
                if (text := paragraph.get_text(strip=True))
            )
    if not paragraphs:
        return None
    heading = section.find(["title", "label"])
    heading_text = heading.get_text(strip=True) if heading else "section"
    content = "\n\n".join(paragraphs)
    return f"## {heading_text}\n\n{content}"


def truncate_markdown(markdown: str, max_chars: int) -> str:
    """Use one server-wide truncation marker so web and literature text
    agree.
    """
    if len(markdown) > max_chars:
        logger.info(
            "Truncating extracted text from %s to %s chars",
            len(markdown),
            max_chars,
        )
        return markdown[:max_chars] + "\n\n[... truncated for length ...]"
    return markdown


def _pmc_html_to_markdown(html_content: str, max_chars: int) -> str:
    soup = BeautifulSoup(html_content, "lxml-xml")
    for tag in soup.find_all(["back", "ref-list", "ack", "fn-group", "fig", "table-wrap"]):
        tag.decompose()

    parts = []
    abstract = soup.find("abstract")
    if abstract:
        paragraphs = abstract.find_all("p")
        abstract_text = (
            "\n\n".join(p.get_text(strip=True) for p in paragraphs)
            if paragraphs
            else abstract.get_text(strip=True)
        )
        if abstract_text:
            parts.append(f"# abstract\n\n{abstract_text}")
    body = soup.find("body")
    if body:
        parts.extend(
            block for section in body.find_all("sec") if (block := _build_section_block(section))
        )
    return truncate_markdown("\n\n".join(parts), max_chars)


def extract_text_from_pmc_html(html_content: str, max_chars: int = 200_000) -> str:
    try:
        return _pmc_html_to_markdown(html_content, max_chars)
    except Exception as exc:
        logger.error("PMC HTML text extraction failed (%s)", failure_summary(exc))
        try:
            text = BeautifulSoup(html_content, "lxml-xml").get_text(separator="\n", strip=True)
            return truncate_markdown(text, max_chars)
        except Exception as fallback_exc:
            logger.error("Fallback text extraction failed (%s)", failure_summary(fallback_exc))
            return "[error: could not extract text from HTML]"
