import logging
import re
from datetime import datetime, timezone
from typing import Any, TypedDict, Unpack

import httpx

from mcp_server.http_client import make_client
from mcp_server.log_privacy import failure_summary
from mcp_server.tools._pacing import RequestPacer
from mcp_server.tools._results import failed, non_raising, ok

logger = logging.getLogger(__name__)

_CHEMBL_URL = "https://www.ebi.ac.uk/chembl/api/data"
_UNIPROT_URL = "https://rest.uniprot.org/uniprotkb/search"


class _GetOptions(TypedDict, total=False):
    params: dict[str, str | int]
    headers: dict[str, str]


async def _get_json(url: str, **options: Unpack[_GetOptions]) -> Any:
    async with make_client(30) as client:
        response = await client.get(url, **options)
        response.raise_for_status()
    return response.json()


def _response_records(payload: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    if field not in payload:
        raise ValueError("response omitted its record list")
    records = payload[field]
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise ValueError("expected a list of JSON objects")
    return records


def _failure_result(source: str, query: str, exc: Exception, provider_name: str) -> dict[str, Any]:
    logger.warning("%s search failed (%s)", provider_name, failure_summary(exc))
    return failed(exc)


def _chembl_record(molecule: dict[str, Any]) -> dict[str, Any]:
    chembl_id = molecule.get("molecule_chembl_id")
    return {
        "chembl_id": chembl_id,
        "name": molecule.get("pref_name"),
        "type": molecule.get("molecule_type"),
        "max_phase": molecule.get("max_phase"),
        "first_approval": molecule.get("first_approval"),
        "url": f"https://www.ebi.ac.uk/chembl/explore/compound/{chembl_id}",
    }


@non_raising
async def search_chembl(query: str, max_results: int = 10) -> dict[str, Any]:
    """Search ChEMBL molecules and return normalized drug records.

    Args:
        query: Molecule, synonym, target, or indication search text.
        max_results: Maximum records to return, capped at 25.

    Returns:
        Source-stamped ChEMBL molecule records, or an empty-records envelope
        with non-secret error metadata if the request fails.
    """
    limit = max(1, min(max_results, 25))
    params: dict[str, str | int] = {
        "q": query,
        "limit": limit,
        "format": "json",
    }
    try:
        payload = await _get_json(f"{_CHEMBL_URL}/molecule/search.json", params=params)
        molecules = _response_records(payload, "molecules")
        records = [_chembl_record(molecule) for molecule in molecules[:limit]]
    except (
        httpx.HTTPError,
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        IndexError,
    ) as exc:
        # Per-source network/parsing failure must not abort the whole literature
        # review.
        return _failure_result("ChEMBL", query, exc, "ChEMBL")
    return ok(records)


def _uniprot_record(result: dict[str, Any]) -> dict[str, Any]:
    genes = result.get("genes") or []
    primary_gene = (genes[0].get("geneName") or {}).get("value") if genes else None
    protein = result.get("proteinDescription") or {}
    recommended = protein.get("recommendedName") or {}
    protein_name = (recommended.get("fullName") or {}).get("value")
    return {
        "accession": result.get("primaryAccession"),
        "gene": primary_gene,
        "protein_name": protein_name,
        "organism": (result.get("organism") or {}).get("scientificName"),
        "functions": [
            comment.get("texts", [{}])[0].get("value")
            for comment in result.get("comments") or []
            if comment.get("commentType") == "FUNCTION" and comment.get("texts")
        ],
        "url": (f"https://www.uniprot.org/uniprotkb/{result.get('primaryAccession')}/entry"),
    }


@non_raising
async def search_uniprot(query: str, max_results: int = 10) -> dict[str, Any]:
    """Search reviewed UniProtKB protein records with functional summaries.

    Args:
        query: UniProt query syntax or free-text protein/gene search.
        max_results: Maximum records to return, capped at 25.

    Returns:
        Source-stamped reviewed protein records, or an empty-records envelope
        with non-secret error metadata if the request fails.
    """
    limit = max(1, min(max_results, 25))
    params: dict[str, str | int] = {
        "query": f"({query}) AND reviewed:true",
        "size": limit,
        "format": "json",
    }
    try:
        payload = await _get_json(_UNIPROT_URL, params=params)
        results = _response_records(payload, "results")[:limit]
        records = [_uniprot_record(result) for result in results]
    except (
        httpx.HTTPError,
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        IndexError,
    ) as exc:
        # Per-source network/parsing failure must not abort the whole literature
        # review.
        return _failure_result("UniProtKB/Swiss-Prot", query, exc, "UniProt")
    return ok(records)


_TRIALS_URL = "https://clinicaltrials.gov/api/v2/studies"


def _record(study: dict[str, Any]) -> dict[str, Any]:
    """Missing ClinicalTrials v2 modules are ordinary, so lookups tolerate
    absence.
    """
    protocol = study.get("protocolSection") or {}
    identification = protocol.get("identificationModule") or {}
    status = protocol.get("statusModule") or {}
    design = protocol.get("designModule") or {}
    conditions = protocol.get("conditionsModule") or {}
    arms = protocol.get("armsInterventionsModule") or {}
    nct_id = identification.get("nctId")
    return {
        "nct_id": nct_id,
        "title": identification.get("briefTitle"),
        # Withdrawn/terminated trials are negative evidence least likely to
        # appear in publications.
        "status": status.get("overallStatus"),
        "why_stopped": status.get("whyStopped"),
        "phases": design.get("phases"),
        "enrollment": (design.get("enrollmentInfo") or {}).get("count"),
        "conditions": conditions.get("conditions"),
        "interventions": [
            intervention.get("name")
            for intervention in (arms.get("interventions") or [])
            if isinstance(intervention, dict)
        ],
        "start_date": (status.get("startDateStruct") or {}).get("date"),
        "url": f"https://clinicaltrials.gov/study/{nct_id}",
    }


@non_raising
async def search_clinical_trials(query: str, max_results: int = 10) -> dict[str, Any]:
    """Search registered clinical trials by intervention, condition or term.

    Args:
        query: An intervention, condition, or free-text search term.
        max_results: Maximum studies to return, capped at 25.

    Returns:
        Source-stamped trial records carrying status and, where a trial
        stopped early, why -- or an empty-records envelope.
    """
    limit = max(1, min(max_results, 25))
    params: dict[str, str | int] = {
        "query.term": query,
        "pageSize": limit,
        "format": "json",
    }
    try:
        payload = await _get_json(_TRIALS_URL, params=params)
        studies = _response_records(payload, "studies")[:limit]
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("ClinicalTrials.gov search failed (%s)", failure_summary(exc))
        return failed(exc)
    return ok([_record(study) for study in studies])


_ENSEMBL_URL = "https://rest.ensembl.org/lookup/symbol/homo_sapiens"
_GNOMAD_URL = "https://gnomad.broadinstitute.org/api"


def _genomics_databases_empty_result(source: str, query: str) -> dict[str, Any]:
    return ok([])


@non_raising
async def search_ensembl_gene(query: str, max_results: int = 1) -> dict[str, Any]:
    """Resolve a gene symbol to its canonical Ensembl record.

    Args:
        query: A human gene symbol, e.g. "WEE1".
        max_results: Unused -- a symbol resolves to one gene. Accepted so
            every search tool shares one parameter mapping.

    Returns:
        A source-stamped record with the stable id, locus, biotype and
        description, or an empty-records envelope.
    """
    del max_results
    try:
        gene = await _get_json(
            f"{_ENSEMBL_URL}/{query}",
            headers={"Content-Type": "application/json"},
        )
    except (httpx.HTTPError, ValueError) as exc:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
            return ok([])
        logger.warning("Ensembl lookup failed (%s)", failure_summary(exc))
        return failed(exc)
    record = {
        "ensembl_id": gene.get("id"),
        "symbol": gene.get("display_name"),
        "biotype": gene.get("biotype"),
        "description": gene.get("description"),
        "locus": (f"{gene.get('seq_region_name')}:{gene.get('start')}-{gene.get('end')}"),
        "strand": gene.get("strand"),
        "url": f"https://www.ensembl.org/Homo_sapiens/Gene/Summary?g={gene.get('id')}",
    }
    return ok([record])


_GNOMAD_QUERY = """
query($symbol: String!) {
  gene(gene_symbol: $symbol, reference_genome: GRCh38) {
    gene_id
    symbol
    gnomad_constraint {
      pli
      oe_lof
      oe_lof_lower
      oe_lof_upper
      mis_z
    }
  }
}
"""


@non_raising
async def search_gnomad_constraint(query: str, max_results: int = 1) -> dict[str, Any]:
    """Return how strongly a gene is depleted of damaging variation.

    Args:
        query: A human gene symbol, e.g. "WEE1".
        max_results: Unused -- a symbol resolves to one gene. Accepted so
            every search tool shares one parameter mapping.

    Returns:
        A source-stamped constraint record, or an empty-records envelope.
    """
    del max_results
    try:
        async with make_client(45) as client:
            response = await client.post(
                _GNOMAD_URL,
                json={"query": _GNOMAD_QUERY, "variables": {"symbol": query}},
            )
            response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            return failed("gnomAD query failed")
        gene = ((payload.get("data") or {}).get("gene")) or {}
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("gnomAD lookup failed (%s)", failure_summary(exc))
        return failed(exc)
    constraint = gene.get("gnomad_constraint") or {}
    if not gene.get("gene_id") or not constraint:
        return _genomics_databases_empty_result("gnomAD", query)
    return ok([_constraint_record(gene, constraint)])


def _constraint_record(gene: dict[str, Any], constraint: dict[str, Any]) -> dict[str, Any]:
    """pLI near 1 means intolerant; LOEUF below 0.35 means constrained."""
    return {
        "gene_id": gene.get("gene_id"),
        "symbol": gene.get("symbol"),
        "pli": constraint.get("pli"),
        "loeuf": constraint.get("oe_lof_upper"),
        "observed_expected_lof": constraint.get("oe_lof"),
        "missense_z": constraint.get("mis_z"),
        "interpretation": (
            "pLI near 1 means intolerant of heterozygous loss of function; "
            "LOEUF (loeuf) below 0.35 marks a constrained gene, above 1.0 an "
            "unconstrained one; missense_z above 3.09 marks missense "
            "constraint."
        ),
        "url": f"https://gnomad.broadinstitute.org/gene/{gene.get('gene_id')}",
    }


_STRING_URL = "https://string-db.org/api/json/interaction_partners"
_REACTOME_SEARCH = "https://reactome.org/ContentService/search/query"
_REACTOME_PATHWAYS = "https://reactome.org/ContentService/data/pathways/low"
_OPENTARGETS_URL = "https://api.platform.opentargets.org/api/v4/graphql"

# Human. Every consumer of these tools is a human-biology run, and the
# APIs require a taxon rather than defaulting to one.
_HUMAN_TAXON = 9606


def _systems_biology_empty_result(source: str, query: str) -> dict[str, Any]:
    return ok([])


def _capped(max_results: int) -> int:
    return max(1, min(max_results, 25))


@non_raising
async def search_string_interactions(query: str, max_results: int = 10) -> dict[str, Any]:
    """Return the proteins a gene or protein is linked to.

    Args:
        query: A gene or protein symbol, e.g. "WEE1" or "SLC9A1".
        max_results: Maximum partners to return, capped at 25.

    Returns:
        Source-stamped interaction partners with STRING's combined score
        and its evidence channels, or an empty-records envelope.
    """
    limit = _capped(max_results)
    params: dict[str, str | int] = {
        "identifiers": query,
        "species": _HUMAN_TAXON,
        "limit": limit,
    }
    try:
        partners = await _get_json(_STRING_URL, params=params)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("STRING lookup failed (%s)", failure_summary(exc))
        return failed(exc)
    records = [
        {
            "partner": partner.get("preferredName_B"),
            "combined_score": partner.get("score"),
            # Keep STRING evidence channels separate: text mining is not
            # experimental support.
            "experimental_score": partner.get("escore"),
            "database_score": partner.get("dscore"),
            "coexpression_score": partner.get("ascore"),
            "textmining_score": partner.get("tscore"),
            "url": f"https://string-db.org/network/{partner.get('stringId_B')}",
        }
        for partner in partners[:limit]
        if isinstance(partner, dict)
    ]
    return ok(records)


async def _reactome_entity(client: httpx.AsyncClient, query: str) -> str | None:
    response = await client.get(
        _REACTOME_SEARCH,
        params={
            "query": query,
            "species": "Homo sapiens",
            "cluster": "true",
        },
    )
    response.raise_for_status()
    groups = response.json().get("results") or []
    entries = [
        entry
        for group in groups
        for entry in (group.get("entries") or [])
        if entry.get("exactType") == "ReferenceGeneProduct"
    ]
    return str(entries[0]["stId"]) if entries else None


@non_raising
async def search_reactome_pathways(query: str, max_results: int = 10) -> dict[str, Any]:
    """Return the curated pathways a gene or protein participates in.

    Two calls rather than one: Reactome's free-text search returns the
    entity, and pathway membership is a separate lookup from it. Searching
    for the pathway name directly finds pathways *called* that, which is a
    different question from the one a mechanism needs answered.

    Args:
        query: A gene or protein symbol, e.g. "WEE1".
        max_results: Maximum pathways to return, capped at 25.

    Returns:
        Source-stamped pathway records, or an empty-records envelope.
    """
    limit = _capped(max_results)
    try:
        async with make_client(30) as client:
            entity = await _reactome_entity(client, query)
            if entity is None:
                return _systems_biology_empty_result("Reactome", query)
            response = await client.get(
                f"{_REACTOME_PATHWAYS}/entity/{entity}/allForms",
                params={"species": str(_HUMAN_TAXON)},
            )
            response.raise_for_status()
        pathways = response.json()
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning("Reactome lookup failed (%s)", failure_summary(exc))
        return failed(exc)
    records = [
        {
            "pathway_id": pathway.get("stId"),
            "name": pathway.get("displayName"),
            "url": f"https://reactome.org/content/detail/{pathway.get('stId')}",
        }
        for pathway in pathways[:limit]
        if isinstance(pathway, dict)
    ]
    return ok(records)


_OPENTARGETS_QUERY = """
query($q: String!, $n: Int!) {
  search(queryString: $q, entityNames: ["target"], page: {index: 0, size: 1}) {
    hits {
      id
      object {
        ... on Target {
          approvedSymbol
          approvedName
          associatedDiseases(page: {index: 0, size: $n}) {
            rows { score disease { id name } }
          }
          tractability { label modality value }
        }
      }
    }
  }
}
"""


def _tractable_modalities(tractability: list[dict[str, Any]]) -> list[str]:
    """Open Targets includes false buckets; carry only satisfied modalities
    to avoid meaningless clutter.
    """
    return [
        f"{entry.get('modality')}: {entry.get('label')}"
        for entry in tractability
        if isinstance(entry, dict) and entry.get("value")
    ]


@non_raising
async def search_open_targets(query: str, max_results: int = 10) -> dict[str, Any]:
    """Return a target's disease associations and druggability.

    Args:
        query: A gene or protein symbol, e.g. "WEE1".
        max_results: Maximum disease associations to return, capped at 25.

    Returns:
        A single source-stamped target record carrying scored disease
        associations and the tractability buckets it satisfies, or an
        empty-records envelope.
    """
    limit = _capped(max_results)
    try:
        async with make_client(30) as client:
            response = await client.post(
                _OPENTARGETS_URL,
                json={
                    "query": _OPENTARGETS_QUERY,
                    "variables": {"q": query, "n": limit},
                },
            )
            response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            return failed("Open Targets query failed")
        hits = (((payload.get("data") or {}).get("search") or {}).get("hits")) or []
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Open Targets lookup failed (%s)", failure_summary(exc))
        return failed(exc)
    if not hits:
        return _systems_biology_empty_result("Open Targets", query)
    return ok([_open_targets_record(hits[0])])


def _open_targets_record(hit: dict[str, Any]) -> dict[str, Any]:
    target = hit.get("object") or {}
    associations = (target.get("associatedDiseases") or {}).get("rows") or []
    return {
        "ensembl_id": hit.get("id"),
        "symbol": target.get("approvedSymbol"),
        "name": target.get("approvedName"),
        "associated_diseases": [
            {
                "disease": (row.get("disease") or {}).get("name"),
                "score": row.get("score"),
            }
            for row in associations
            if isinstance(row, dict)
        ],
        "tractability": _tractable_modalities(target.get("tractability") or []),
        "url": f"https://platform.opentargets.org/target/{hit.get('id')}",
    }


_API_URL = "https://www.ebi.ac.uk/gwas/rest/api/v2/associations"
_FAQ_URL = "https://www.ebi.ac.uk/gwas/docs/faq/"
_RS_ID_RE = re.compile(r"^rs[0-9]+$", re.IGNORECASE)
MAX_PAGE_SIZE = 100
MAX_PAGE = 100
_REQUEST_TIMEOUT_SECONDS = 15
# Process-wide start slots stay below EBI's documented 15 qps cap.
_wait_for_request_slot = RequestPacer(0.1).wait


def _page_number(value: Any, fallback: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else fallback


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _strings(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _trait_names(row: dict[str, Any]) -> list[str]:
    efo_traits = row.get("efo_traits") or []
    traits = [
        item["efo_trait"]
        for item in efo_traits
        if isinstance(item, dict) and isinstance(item.get("efo_trait"), str)
    ]
    return traits or _strings(row.get("reported_trait"))


def _record_urls(association_id: int | str, accession: str, pubmed_id: Any) -> dict[str, str]:
    urls = {
        "source_url": f"{_API_URL}/{association_id}",
        "study_url": f"https://www.ebi.ac.uk/gwas/studies/{accession}",
    }
    if pubmed_id is not None and str(pubmed_id).isdigit():
        urls["pubmed_url"] = f"https://pubmed.ncbi.nlm.nih.gov/{pubmed_id}/"
    return urls


def _association_effects(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "p_value": _number(row.get("p_value")),
        "pvalue_mantissa": _number(row.get("pvalue_mantissa")),
        "pvalue_exponent": _number(row.get("pvalue_exponent")),
        "beta": row.get("beta"),
        "odds_ratio": row.get("odds_ratio"),
        "risk_frequency": row.get("risk_frequency"),
        "confidence_interval": [
            _number(row.get("ci_lower")),
            _number(row.get("ci_upper")),
        ],
    }


def _association(row: dict[str, Any], rs_id: str) -> dict[str, Any]:
    association_id = row.get("association_id")
    accession = row.get("accession_id")
    pubmed_id = row.get("pubmed_id")
    if not isinstance(association_id, (int, str)) or not str(association_id).isdigit():
        raise ValueError("GWAS Catalog association is missing its source ID")
    if not isinstance(accession, str) or not re.fullmatch(r"GCST[0-9]+", accession):
        raise ValueError("GWAS Catalog association is missing a valid study accession")
    traits = _trait_names(row)

    result: dict[str, Any] = {
        "rs_id": rs_id,
        "association_id": association_id,
        "study_accession": accession,
        "trait": traits[0] if traits else None,
        "traits": traits,
        "reported_traits": _strings(row.get("reported_trait")),
        **_association_effects(row),
        "mapped_genes": _strings(row.get("mapped_genes")),
        "pubmed_id": str(pubmed_id) if pubmed_id is not None else None,
        "effect_alleles": _strings(row.get("snp_effect_allele")),
        "interpretation": (
            "The association does not establish causality or a mapped-gene mechanism."
        ),
    }
    result.update(_record_urls(association_id, accession, pubmed_id))
    return result


async def _request_page(params: dict[str, str | int]) -> Any:
    await _wait_for_request_slot()
    async with make_client(
        _REQUEST_TIMEOUT_SECONDS,
        headers={"Accept": "application/json"},
        honour_proxy_env=False,
    ) as client:
        response = await client.get(_API_URL, params=params)
        response.raise_for_status()
    return response.json()


def _association_rows(payload: dict[str, Any], page_data: dict[str, Any]) -> list[Any]:
    if "_embedded" not in payload:
        total_elements = page_data.get("totalElements")
        if (
            isinstance(total_elements, int)
            and not isinstance(total_elements, bool)
            and total_elements == 0
        ):
            return []
        raise ValueError("invalid GWAS Catalog association list")

    embedded = payload.get("_embedded")
    associations = embedded.get("associations") if isinstance(embedded, dict) else None
    if not isinstance(associations, list) or any(not isinstance(row, dict) for row in associations):
        raise ValueError("invalid GWAS Catalog association list")
    return associations


def _parse_page(
    payload: Any, rs_id: str, page: int, size: int
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise ValueError("invalid GWAS Catalog response")
    page_data = payload.get("page") or {}
    if not isinstance(page_data, dict):
        raise ValueError("invalid GWAS Catalog page metadata")

    associations = _association_rows(payload, page_data)
    page_result = {
        "number": _page_number(page_data.get("number"), page),
        "size": _page_number(page_data.get("size"), size),
        "total_elements": _page_number(page_data.get("totalElements"), 0),
        "total_pages": _page_number(page_data.get("totalPages"), 0),
    }
    return page_result, [_association(row, rs_id) for row in associations]


@non_raising
async def search_gwas_catalog_associations(
    rs_id: str, size: int = 20, page: int = 0
) -> dict[str, Any]:
    """Return a bounded page of GWAS Catalog associations for one rsID.

    This lookup reports statistical associations and their published effect
    context. Neither an associated variant nor a mapped gene establishes a
    causal mechanism.
    """
    normalized = rs_id.strip().lower()
    if len(normalized) > 24 or not _RS_ID_RE.fullmatch(normalized):
        return failed("rs_id must be an rs identifier such as rs334")

    bounded_size = max(1, min(size, MAX_PAGE_SIZE))
    bounded_page = max(0, min(page, MAX_PAGE))
    params: dict[str, str | int] = {
        "rs_id": normalized,
        "page": bounded_page,
        "size": bounded_size,
    }

    try:
        payload = await _request_page(params)
        page_info, records = _parse_page(payload, normalized, bounded_page, bounded_size)
        for record in records:
            record.update(
                {
                    "page": page_info,
                    "access_date": datetime.now(timezone.utc).date().isoformat(),
                    "service_terms": _FAQ_URL,
                }
            )
        return ok(records)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("GWAS Catalog lookup failed (%s)", failure_summary(exc))
        return failed(exc)
