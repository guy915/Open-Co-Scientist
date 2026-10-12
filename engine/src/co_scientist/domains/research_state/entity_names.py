import re

# The hyphenated suffix excludes pathway notation such as RAGE-JAK2.
_HYPHENATED_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,5}-[0-9]{1,2}[A-Z]?)\b")
_STANDALONE_RE = re.compile(r"\b([A-Z][A-Z0-9]{2,5})\b")

# These ordinary words, abbreviations and broad families are not specific gene
# queries.
_STOP = frozenset(
    {
        "THE",
        "AND",
        "FOR",
        "WITH",
        "THIS",
        "THAT",
        "FROM",
        "INTO",
        "BUT",
        "NOT",
        "HAS",
        "CAN",
        "MAY",
        "WILL",
        "ARE",
        "WAS",
        "TWO",
        "ONE",
        "USE",
        "NEW",
        "ALL",
        "HOW",
        "ANY",
        "ITS",
        "VIA",
        "WHO",
        "WHY",
        "YET",
        "SET",
        "OUR",
        "OUT",
        "WAY",
        "TRY",
        "LET",
        "PUT",
        "GET",
        "END",
        "DID",
        "HIS",
        "HER",
        "BEEN",
        "ALSO",
        "SHOW",
        "THAN",
        "DOES",
        "SUCH",
        "HAVE",
        "MORE",
        "WELL",
        "MOST",
        "ONLY",
        "BOTH",
        "SOME",
        "MCP",
        "LLM",
        "API",
        "PDF",
        "URL",
        "PCT",
        "KEY",
        "RED",
        "DNA",
        "RNA",
        "ATP",
        "ADP",
        "GDP",
        "GTP",
        "USA",
        "NIH",
        "CSF",
        "CNS",
        "BBB",
        "PPI",
        "PET",
        "MRI",
        "CVE",
        "ROS",
        "iPSC",
        "CRISPR",
        "ELISA",
        "GWAS",
        "SNP",
        "DOID",
        "MESH",
        "HGNC",
        "CHEBI",
        "CYP450",
        "CSPG",
        "CSPGS",
    }
)

_ALIAS_MAP: dict[str, str] = {
    "RAGE": "AGER",
    "MK2": "MAPKAPK2",
    "P38": "MAPK14",
    "P53": "TP53",
    "BACE": "BACE1",
    "YKL40": "CHI3L1",
    "MCP1": "CCL2",
    "ABETA": "APP",
    "APOE4": "APOE",
}


def _normalize_entity(raw: str) -> str:
    """Canonical gene names omit hyphens and use aliases such as AGER for
    RAGE."""
    normalized = raw.replace("-", "")
    upper = normalized.upper()
    return _ALIAS_MAP.get(upper, normalized)


def _should_skip_entity(upper: str, seen: set[str]) -> bool:
    return upper in _STOP or upper in seen


def _is_mutation_notation(raw: str) -> bool:
    """Single-letter/digit mutation labels such as V600E are not standalone
    gene names."""
    return len(raw) >= 2 and raw[0].isupper() and raw[1].isdigit()


def _add_hyphenated_entities(hyphenated: list[str], seen: set[str], result: list[str]) -> None:
    """Hyphenated names are higher-signal; marking their prefixes prevents a
    second standalone match for the same entity."""
    for raw in hyphenated:
        prefix = raw.split("-")[0].upper()
        seen.add(prefix)
        normalized = _normalize_entity(raw)
        upper = normalized.upper()
        if _should_skip_entity(upper, seen):
            continue
        seen.add(upper)
        result.append(normalized)


def _add_standalone_entities(
    standalone: list[str], seen: set[str], result: list[str], max_entities: int
) -> None:
    for raw in standalone:
        if len(result) >= max_entities:
            break
        if _is_mutation_notation(raw):
            continue
        normalized = _normalize_entity(raw)
        upper = normalized.upper()
        if _should_skip_entity(upper, seen):
            continue
        seen.add(upper)
        result.append(normalized)


def extract_entity_names(text: str, max_entities: int = 3) -> list[str]:
    hyphenated = _HYPHENATED_RE.findall(text)
    standalone = _STANDALONE_RE.findall(text)

    seen: set[str] = set()
    result: list[str] = []

    _add_hyphenated_entities(hyphenated, seen, result)
    _add_standalone_entities(standalone, seen, result, max_entities)

    return result[:max_entities]
