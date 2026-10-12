import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt


# Pinned vendored files are never edited, so their broken links are exempt one by one.
# No path-wide exceptions.
PINNED_LINK_EXCEPTIONS = {
    (
        "vendor/science-skills/skills/opentargets_database/SKILL.md",
        132,
        "references/OpenTargets_GraphQL_Guide",
    ): "pinned vendor, shipped as-is, AGENTS.md",
    (
        "vendor/science-skills/skills/uniprot_database/SKILL.md",
        86,
        "references/id_mapping_documentation.md",
    ): "pinned vendor, shipped as-is, AGENTS.md",
    (
        "vendor/science-skills/skills/uniprot_database/SKILL.md",
        286,
        "references/id_mapping_documentation.md",
    ): "pinned vendor, shipped as-is, AGENTS.md",
}


@dataclass
class Link:
    target: str
    line: int


@dataclass
class Document:
    anchors: set[str] = field(default_factory=set)
    links: list[Link] = field(default_factory=list)


def srcset_targets(value: str) -> list[str]:
    # HTML collects a URL up to ASCII whitespace; commas inside data URLs belong
    # to the URL. Only trailing commas and descriptor separators split candidates.
    targets = []
    remaining = value
    whitespace = " \t\n\r\f"
    while remaining:
        remaining = remaining.lstrip(whitespace + ",")
        if not remaining:
            break
        match = re.match(r"[^ \t\n\r\f]+", remaining)
        assert match is not None
        target = match.group()
        remaining = remaining[len(target) :]
        targets.append(target.rstrip(","))
        if target.endswith(","):
            continue
        depth = 0
        for index, char in enumerate(remaining):
            if char == "(":
                depth += 1
            elif char == ")" and depth:
                depth -= 1
            elif char == "," and not depth:
                remaining = remaining[index + 1 :]
                break
        else:
            break
    return targets


class HtmlLinks(HTMLParser):
    def __init__(self, document: Document, line: int):
        super().__init__()
        self.document = document
        self.line = line

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if value is None:
                continue
            if key == "id" or (tag == "a" and key == "name"):
                self.document.anchors.add(value)
            if key in {"href", "src", "srcset"}:
                targets = srcset_targets(value) if key == "srcset" else [value]
                self.document.links.extend(
                    Link(target, self.line + self.getpos()[0] - 1) for target in targets
                )


def slug(text: str) -> str:
    text = re.sub(r"<[^>]*>", "", text).lower()
    return "".join(
        char for char in text if char in " -_" or unicodedata.category(char)[0] in "LNM"
    ).replace(" ", "-")


def parse_document(source: str) -> Document:
    document = Document()
    tokens = (
        MarkdownIt("commonmark").enable("table").enable("strikethrough").parse(source)
    )
    for index, token in enumerate(tokens):
        line = token.map[0] + 1 if token.map else 1
        if token.type == "heading_open":
            heading = tokens[index + 1]
            content = "".join(
                child.content
                for child in heading.children or []
                if child.type in {"text", "code_inline", "image", "html_inline"}
            )
            base = slug(content)
            anchor = base
            suffix = 0
            while anchor in document.anchors:
                suffix += 1
                anchor = f"{base}-{suffix}"
            document.anchors.add(anchor)
        if token.type == "html_block":
            HtmlLinks(document, line).feed(token.content)
        for child in token.children or []:
            if child.type == "link_open":
                document.links.append(Link(child.attrGet("href") or "", line))
            elif child.type == "image":
                document.links.append(Link(child.attrGet("src") or "", line))
            elif child.type == "html_inline":
                HtmlLinks(document, line).feed(child.content)
    return document


def check_links(root: Path, tracked: list[str]) -> list[str]:
    root = root.resolve()
    files = {(root / name).resolve() for name in tracked}
    directories = {
        parent
        for path in files
        for parent in path.parents
        if parent.is_relative_to(root)
    }
    documents = {
        path: parse_document(path.read_text(encoding="utf-8"))
        for path in files
        if path.suffix.lower() == ".md"
    }
    readmes = {}
    for path in sorted(documents):
        if path.name.lower() == "readme.md":
            readmes.setdefault(path.parent, path)
    findings = []
    accepted_exceptions = set()
    for path, document in sorted(documents.items()):
        for link in document.links:
            target = urlsplit(link.target)
            if target.scheme or target.netloc:
                continue
            destination = (
                (path.parent / unquote(target.path)).resolve() if target.path else path
            )
            if target.path.startswith("/"):
                destination = (root / unquote(target.path).lstrip("/")).resolve()
            problem = None
            if destination not in files and destination not in directories:
                problem = "target is absent from tracked files"
            elif target.fragment and (
                destination in documents or destination in directories
            ):
                anchor_destination = readmes.get(destination, destination)
                anchor = unquote(target.fragment)
                if anchor_destination not in documents:
                    problem = "directory anchor has no tracked Markdown README"
                elif anchor not in documents[anchor_destination].anchors:
                    problem = "heading/HTML anchor is missing"
            if problem:
                exception = (path.relative_to(root).as_posix(), link.line, link.target)
                if (
                    problem == "target is absent from tracked files"
                    and exception in PINNED_LINK_EXCEPTIONS
                    and exception not in accepted_exceptions
                ):
                    accepted_exceptions.add(exception)
                    continue
                findings.append(
                    f"{path.relative_to(root)}:{link.line}: {link.target}: {problem}"
                )
    return findings


def main() -> int:
    tracked = (
        subprocess.check_output(["git", "ls-files", "-z"], text=True)
        .rstrip("\0")
        .split("\0")
    )
    findings = check_links(Path("."), tracked)
    for finding in findings:
        print(finding)
    print(
        f"Markdown links: {sum(name.lower().endswith('.md') for name in tracked)} files, {len(findings)} findings"
    )
    print(
        f"Pinned vendor exceptions: {len(PINNED_LINK_EXCEPTIONS)} exact file/line/target keys"
    )
    return int(bool(findings))


if __name__ == "__main__":
    sys.exit(main())
