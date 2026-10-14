"""Scripts use the confined workspace; only invoked scripts receive
credentials.
"""

from __future__ import annotations

import contextlib
import datetime
import functools
import logging
import os
import pathlib
import re
import shutil
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass

import yaml

from co_scientist.core._context import _bind_contextvar

logger = logging.getLogger(__name__)


SKILLS_DIR_ENV = "COSCIENTIST_SKILLS_DIR"
SKILLS_PYTHON_ENV = "COSCIENTIST_SKILLS_PYTHON"

# Dependencies are baked into the image: uv's protected .git cache cannot
# initialize in the sandbox.
_FALLBACK_SKILLS_PYTHON = "python3"

# Bound hostile documents without excerpting the skill the model requested.
MAX_SKILL_DOCUMENT_CHARS = 60_000

MAX_SUMMARY_CHARS = 160

_DOCUMENT_NAME = "SKILL.md"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    directory: pathlib.Path


def skills_directory() -> pathlib.Path | None:
    """Unset and missing skills directories both mean this deployment offers
    no skills.
    """
    raw = os.environ.get(SKILLS_DIR_ENV, "").strip()
    if not raw:
        return None
    directory = pathlib.Path(raw)
    if not directory.is_dir():
        logger.warning(
            "%s is set to %r, which is not a directory; no skills available",
            SKILLS_DIR_ENV,
            raw,
        )
        return None
    return directory


def skills_python() -> str:
    return os.environ.get(SKILLS_PYTHON_ENV, "").strip() or (_FALLBACK_SKILLS_PYTHON)


def _parse_frontmatter(text: str) -> dict[str, object] | None:
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        parsed = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None
    return parsed if isinstance(parsed, dict) else None


# Upstream declares dependencies per script in PEP 723 metadata, not a shared
# manifest.
_METADATA_BLOCK = re.compile(r"# /// script(.*?)# ///", re.S)
_DEPENDENCY = re.compile(r'^#\s*"([A-Za-z0-9._-]+)')


def _normalised(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@functools.cache
def _installed_distributions() -> frozenset[str] | None:
    """Inspect site-packages without spawning a process each turn; unknown
    layouts must not empty the catalogue.
    """
    interpreter = pathlib.Path(skills_python())
    if not interpreter.is_absolute():
        return None
    roots = list((interpreter.parent.parent / "lib").glob("*/site-packages"))
    if not roots:
        return None
    return frozenset(
        _normalised(entry.name.split("-")[0])
        for root in roots
        for entry in root.glob("*.dist-info")
    )


def _runnable_scripts(directory: pathlib.Path) -> list[pathlib.Path]:
    scripts = directory / "scripts"
    return sorted(scripts.glob("*.py")) if scripts.is_dir() else []


def _unmet_dependencies(directory: pathlib.Path) -> set[str]:
    """Unknown installed dependencies must not silently withhold skills."""
    installed = _installed_distributions()
    if installed is None:
        return set()
    scripts = directory / "scripts"
    sources = sorted(scripts.glob("*.py")) if scripts.is_dir() else []
    declared = set().union(*(_declared_in(s) for s in sources)) if sources else set()
    return declared - installed


def _declared_in(script: pathlib.Path) -> set[str]:
    try:
        text = script.read_text(encoding="utf-8")
    except OSError:
        return set()
    block = _METADATA_BLOCK.search(text)
    if block is None:
        return set()
    found = (_DEPENDENCY.match(line.strip()) for line in block.group(1).splitlines())
    return {_normalised(m.group(1)) for m in found if m is not None}


def _load_skill(directory: pathlib.Path) -> Skill | None:
    document = directory / _DOCUMENT_NAME
    try:
        text = document.read_text(encoding="utf-8")
    except OSError:
        return None
    front = _parse_frontmatter(text)
    if front is None:
        return None
    name = str(front.get("name") or "").strip()
    description = " ".join(str(front.get("description") or "").split())
    if not name or not description:
        return None
    withheld = _withholding_reason(directory)
    if withheld is not None:
        logger.info("Skill %r withheld: %s", name, withheld)
        return None
    return Skill(name=name, description=description, directory=directory)


def _withholding_reason(directory: pathlib.Path) -> str | None:
    """Withhold unusable skills before reading them: missing scripts or
    dependencies cannot be repaired by the model.
    """
    if not _runnable_scripts(directory):
        return "no runnable script"
    unmet = _unmet_dependencies(directory)
    if unmet:
        return f"{', '.join(sorted(unmet))} not installed for {skills_python()}"
    return None


@functools.cache
def available_skills() -> tuple[Skill, ...]:
    """The image-baked catalogue is cached because every tool-loop turn reads
    it.
    """
    directory = skills_directory()
    if directory is None:
        return ()
    found = [
        skill
        for child in sorted(directory.iterdir())
        if child.is_dir() and (skill := _load_skill(child)) is not None
    ]
    if found:
        logger.info("Loaded %d skills from %s", len(found), directory)
    else:
        logger.warning("No readable skills in %s", directory)
    return tuple(found)


def find_skill(name: str) -> Skill | None:
    wanted = name.strip().lower()
    for skill in available_skills():
        if skill.name.lower() == wanted:
            return skill
    return None


def _routing_summary(description: str) -> str:
    """The catalogue routes by the first sentence; the full document restores
    the remaining guidance.
    """
    head = description.split(". ", 1)[0].rstrip(".")
    return (
        f"{head[: MAX_SUMMARY_CHARS - 1]}\u2026" if (len(head) > MAX_SUMMARY_CHARS) else f"{head}."
    )


def catalogue_section() -> str:
    """Catalogue text is resent each turn, so summaries avoid a recurring
    prompt tax.
    """
    skills = available_skills()
    if not skills:
        return ""
    lines = [f"- {skill.name}: {_routing_summary(skill.description)}" for skill in skills]
    return "\n".join(lines)


def _skill_file(skill: Skill, path: str) -> pathlib.Path | None:
    """Resolve model-chosen paths inside the skill to prevent traversal
    reads.
    """
    candidate = (skill.directory / path).resolve()
    root = skill.directory.resolve()
    if candidate == root or root not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def _read_text(target: pathlib.Path | None) -> str | None:
    if target is None:
        return None
    try:
        return target.read_text(encoding="utf-8")
    except OSError:
        return None


def read_skill_document(
    name: str, path: str | None = None, *, network_allowed: bool = False
) -> str | None:
    """Expose nested references; the preamble overrides upstream uv and
    human-confirmation instructions without changing the pinned skill
    documents.
    """
    skill = find_skill(name)
    if skill is None:
        return None
    target = skill.directory / _DOCUMENT_NAME if path is None else _skill_file(skill, path)
    text = _read_text(target)
    if text is None:
        return None
    if path is not None:
        return text[:MAX_SKILL_DOCUMENT_CHARS]
    network = (
        "the network is available for the skill's own API calls only"
        if network_allowed
        else "this workspace has no network access, so steps that call an external API will fail"
    )
    preamble = (
        f"Skill directory: {skill.directory}\n"
        f"Run this skill's scripts with `{skills_python()} "
        f"{skill.directory}/scripts/<script>.py`, from the workspace, via "
        "run_command. Ignore any instruction below to use `uv run` or to "
        f"install packages: dependencies are already installed and {network}.\n"
        "Where this document points at a file under `references/`, read "
        "it by calling read_skill again with that path -- the exact "
        "command syntax usually lives there, not here.\n"
        "There is no user to ask. Where this document says to stop and "
        "ask a question, choose the most reasonable answer, say which "
        "you chose, and carry on.\n"
        "Write results to a file with the script's own output option "
        "wherever it has one, then read the fields you need. Every "
        "`--output /tmp/...` below is wrong here: the workspace is the "
        "only writable directory, so replace the whole path with a bare "
        "relative filename such as `results.json`. An absolute path is "
        "refused and the script dies having already spent its API "
        "call.\n\n"
    )
    return (preamble + text)[:MAX_SKILL_DOCUMENT_CHARS]


LICENCES_DIRNAME = ".licenses"

# Read licence filenames from SKILL.md so upstream renames cannot trigger
# duplicate disclosure work.
_TARGET = re.compile(r"\.licenses/([A-Za-z0-9_.-]+\.txt)")

# The terms live at URLs the document quotes; capturing them keeps the
# seeded notice specific rather than a generic placeholder.
_URL = re.compile(r"https?://[^\s)>\]]+")

_NOTICE = (
    "Notice recorded by the Co-Scientist harness on {stamp}.\n"
    "This run used the {name} skill. Review that source's terms of use "
    "before relying on or redistributing results:\n{urls}\n"
)


def _skill_notice(name: str, document: str) -> tuple[str, str] | None:
    match = _TARGET.search(document)
    if match is None:
        return None
    prerequisite = document[: match.end() + 600]
    urls = sorted(set(_URL.findall(prerequisite)))
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    return match.group(1), _NOTICE.format(
        stamp=stamp,
        name=name,
        urls="\n".join(f"  {url}" for url in urls) or "  (see SKILL.md)",
    )


def _write_notice(directory: pathlib.Path, skill: Skill) -> bool:
    try:
        document = (skill.directory / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return False
    notice = _skill_notice(skill.name, document)
    if notice is None:
        return False
    filename, contents = notice
    try:
        (directory / filename).write_text(contents, encoding="utf-8")
    except OSError as exc:
        logger.warning("could not write %s: %s", filename, exc)
        return False
    return True


def seed_licence_notices(root: pathlib.Path) -> int:
    """Notice seeding is best-effort: failure must not discard a review that
    will report its own workspace error.
    """
    skills = available_skills()
    if not skills:
        return 0
    directory = root / LICENCES_DIRNAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("could not create %s: %s", directory, exc)
        return 0
    written = sum(_write_notice(directory, skill) for skill in skills)
    logger.info("Seeded %d skill licence notices in %s", written, directory)
    return written


class SkillUsage:
    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def record(self, name: str) -> None:
        self._counts[name] = self._counts.get(name, 0) + 1

    def snapshot(self) -> dict[str, int]:
        return dict(self._counts)


_current_usage: ContextVar[SkillUsage | None] = ContextVar("current_skill_usage", default=None)


@contextlib.contextmanager
def scoped_skill_usage() -> Iterator[SkillUsage]:
    usage = SkillUsage()
    with _bind_contextvar(_current_usage, usage):
        yield usage


def record_skill_use(name: str) -> None:
    usage = _current_usage.get()
    if usage is not None:
        usage.record(name)


# The skill-facing credential name wins over translated host aliases.
_CREDENTIAL_ALIASES: dict[str, tuple[str, ...]] = {
    "NCBI_API_KEY": ("NCBI_API_KEY", "ENTREZ_API_KEY"),
    "USER_EMAIL": ("USER_EMAIL", "ENTREZ_EMAIL"),
    "NCBI_TOOL": ("NCBI_TOOL",),
    "ALPHAGENOME_API_KEY": ("ALPHAGENOME_API_KEY",),
    "OPENALEX_API_KEY": ("OPENALEX_API_KEY",),
    "FDA_API_KEY": ("FDA_API_KEY",),
}

_SKILL_CREDENTIALS: dict[str, tuple[str, ...]] = {
    "alphagenome_single_variant_analysis": ("ALPHAGENOME_API_KEY",),
    "clinvar_database": ("NCBI_API_KEY",),
    "dbsnp_database": ("NCBI_API_KEY",),
    "literature_search_openalex": ("OPENALEX_API_KEY",),
    "ncbi_sequence_fetch": ("NCBI_API_KEY",),
    "openfda_database": ("FDA_API_KEY",),
    "protein_sequence_msa": ("USER_EMAIL",),
    "protein_sequence_similarity_search": ("USER_EMAIL",),
    "pubmed_database": ("NCBI_API_KEY", "USER_EMAIL", "NCBI_TOOL"),
}


def skill_environment(skill_name: str) -> dict[str, str]:
    """Omit missing credentials: upstream skills distinguish absent variables
    from present empty strings.
    """
    resolved: dict[str, str] = {}
    for wanted in _SKILL_CREDENTIALS.get(skill_name, ()):
        aliases = _CREDENTIAL_ALIASES[wanted]
        for alias in aliases:
            value = os.environ.get(alias, "").strip()
            if value:
                resolved[wanted] = value
                break
    return resolved


def invoked_skill(
    argv: list[str],
    *,
    cwd: pathlib.Path | None = None,
    writable_roots: tuple[pathlib.Path, ...] = (),
) -> str | None:
    """Attribute actual script invocations per source, because third-party
    disclosure obligations differ.
    """
    directory = skills_directory()
    if directory is None or not argv:
        return None
    if len(argv) < 2 or not argv[1] or argv[1].startswith("-"):
        return None
    command_cwd = cwd or pathlib.Path.cwd()
    expected_python = _executable_paths(skills_python(), command_cwd, writable_roots)
    actual_python = _executable_paths(argv[0], command_cwd, writable_roots)
    if expected_python is None or actual_python is None:
        return None
    if expected_python[0] != actual_python[0] or expected_python[1] != actual_python[1]:
        return None
    if any(
        _path_in_writable_root(path, writable_roots)
        for path in (expected_python[0], expected_python[1])
    ):
        return None
    script = pathlib.Path(argv[1])
    if not script.is_absolute() or _has_symlink_component(script):
        return None
    try:
        resolved_script = script.resolve(strict=True)
    except (OSError, ValueError):
        return None
    if not resolved_script.is_file() or resolved_script.suffix != ".py":
        return None
    for skill in available_skills():
        root = skill.directory.resolve()
        scripts_root = root / "scripts"
        if any(_paths_overlap(scripts_root, writable) for writable in writable_roots):
            continue
        if not _is_within(str(resolved_script), scripts_root):
            continue
        if any(_same_path(resolved_script, candidate) for candidate in _runnable_scripts(root)):
            return skill.name
    return None


def _executable_paths(
    executable: str,
    cwd: pathlib.Path,
    writable_roots: tuple[pathlib.Path, ...],
) -> tuple[pathlib.Path, pathlib.Path] | None:
    resolved: str | None
    if os.sep in executable or (os.altsep and os.altsep in executable):
        candidate = pathlib.Path(executable)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        if _path_has_writable_prefix(candidate, writable_roots):
            return None
        resolved = os.path.abspath(candidate)
    else:
        path_entries = os.environ.get("PATH", os.defpath).split(os.pathsep)
        absolute_entries = []
        for entry in path_entries:
            directory = pathlib.Path(entry or ".")
            absolute_entries.append(str(directory if directory.is_absolute() else cwd / directory))
        absolute_path = os.pathsep.join(absolute_entries)
        resolved = shutil.which(executable, path=absolute_path)
    if resolved is None:
        return None
    try:
        lexical = pathlib.Path(resolved)
        if _path_has_writable_prefix(lexical, writable_roots):
            return None
        return pathlib.Path(os.path.abspath(lexical)), lexical.resolve(strict=True)
    except (OSError, ValueError):
        return None


def _path_in_writable_root(path: pathlib.Path, writable_roots: tuple[pathlib.Path, ...]) -> bool:
    lexical = pathlib.Path(os.path.abspath(path))
    try:
        canonical = path.resolve(strict=False)
    except (OSError, ValueError):
        canonical = lexical
    return any(
        lexical.is_relative_to(root) or canonical.is_relative_to(root) for root in writable_roots
    )


def _path_has_writable_prefix(path: pathlib.Path, writable_roots: tuple[pathlib.Path, ...]) -> bool:
    if not path.is_absolute():
        return True
    current = pathlib.Path(path.anchor)
    for part in path.parts[1:]:
        if part == "..":
            try:
                current = current.resolve(strict=False).parent
            except (OSError, ValueError):
                current = current.parent
        else:
            current = current / part
        if _path_in_writable_root(current, writable_roots):
            return True
    return False


def _has_symlink_component(path: pathlib.Path) -> bool:
    """Reject aliases so a checked script path cannot be swapped through a link."""
    if not path.is_absolute():
        return True
    current = pathlib.Path(path.anchor)
    for part in path.parts[1:]:
        if part == "..":
            current = current.parent
            continue
        current = current / part
        if current.is_symlink():
            return True
    return False


def _same_path(left: pathlib.Path, right: pathlib.Path) -> bool:
    try:
        return left.resolve(strict=True) == right.resolve(strict=True)
    except (OSError, ValueError):
        return False


def _paths_overlap(left: pathlib.Path, right: pathlib.Path) -> bool:
    try:
        left = left.resolve(strict=True)
        right = right.resolve(strict=True)
    except (OSError, ValueError):
        return True
    return left.is_relative_to(right) or right.is_relative_to(left)


def _is_within(argument: str, root: pathlib.Path) -> bool:
    """Resolve before checking containment so symlinks and traversal cannot
    impersonate skill scripts.
    """
    try:
        candidate = pathlib.Path(argument).resolve()
    except (OSError, ValueError):
        return False
    return candidate.is_relative_to(root)


__all__ = [
    "Skill",
    "SkillUsage",
    "available_skills",
    "catalogue_section",
    "invoked_skill",
    "read_skill_document",
    "record_skill_use",
    "scoped_skill_usage",
    "seed_licence_notices",
    "skill_environment",
    "skills_directory",
]
