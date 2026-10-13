import logging
from typing import Any

import httpx
import mcp_server.tools.biomedical_databases as databases
import pytest
from mcp_server.tests._httpx import (
    registered_tools,
    transport_responses,
)


@pytest.mark.parametrize(
    "tool_case",
    [
        {
            "tool_name": "search_chembl",
            "query": "aspirin",
            "source": "ChEMBL",
            "success_payload": {
                "molecules": [{"molecule_chembl_id": "CHEMBL25", "pref_name": "ASPIRIN"}]
            },
            "record": {"chembl_id": "CHEMBL25"},
        },
        {
            "tool_name": "search_uniprot",
            "query": "EGFR",
            "source": "UniProtKB/Swiss-Prot",
            "success_payload": {
                "results": [
                    {
                        "primaryAccession": "P00533",
                        "genes": [{"geneName": {"value": "EGFR"}}],
                        "comments": [
                            {
                                "commentType": "FUNCTION",
                                "texts": [{"value": "Kinase."}],
                            }
                        ],
                    }
                ]
            },
            "record": {
                "accession": "P00533",
                "gene": "EGFR",
                "functions": ["Kinase."],
            },
        },
    ],
)
async def test_registered_biomedical_tools_report_outcome_at_mcp_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tool_case: dict[str, Any],
) -> None:
    tool_name = tool_case["tool_name"]
    query = tool_case["query"]
    envelope = {"status": "ok", "records": []}
    requests = transport_responses(
        monkeypatch,
        httpx.Response(200, json=tool_case["success_payload"]),
        httpx.Response(200, json={"molecules": [], "results": []}),
        httpx.Response(503, json={}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={}),
        httpx.ReadTimeout("upstream timeout"),
        httpx.ConnectError("connection refused"),
    )

    async with registered_tools() as client:
        tool_names = {tool.name for tool in await client.list_tools()}
        results = [await client.call_tool(tool_name, {"query": query}) for _ in range(7)]

    success, empty, *failures = results
    assert tool_name in tool_names
    assert {key: success.data["records"][0][key] for key in tool_case["record"]} == tool_case[
        "record"
    ]
    assert "error" not in success.data
    assert empty.data == envelope
    assert [failure.data for failure in failures] == [
        {"status": "failed", "records": [], "error": error}
        for error in (
            "HTTP 503",
            "invalid_response",
            "invalid_response",
            "timeout",
            "network_error",
        )
    ]
    assert all(result.is_error is not True for result in results)
    assert len(requests) == 7


_RS_ID = "rs334"
_ASSOCIATION_URL = "https://www.ebi.ac.uk/gwas/rest/api/v2/associations/226290633"


def _payload() -> dict[str, Any]:
    return {
        "page": {
            "size": 1,
            "totalElements": 134,
            "totalPages": 134,
            "number": 0,
        },
        "_embedded": {
            "associations": [
                {
                    "association_id": 226290633,
                    "accession_id": "GCST90480652",
                    "p_value": 3e-31,
                    "beta": "0.1401 unit increase",
                    "risk_frequency": "0.9614",
                    "ci_lower": 0.12,
                    "ci_upper": 0.16,
                    "efo_traits": [{"efo_id": "EFO_0004309", "efo_trait": "platelet count"}],
                    "reported_trait": ["platelet count (minimum, inv-norm transformed)"],
                    "mapped_genes": ["HBB"],
                    "pubmed_id": "39024449",
                    "snp_effect_allele": ["rs334-T"],
                    "_links": {"self": {"href": _ASSOCIATION_URL}},
                }
            ]
        },
    }


async def test_association_lookup_preserves_source_and_effect_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = transport_responses(monkeypatch, _payload())

    result = await databases.search_gwas_catalog_associations(_RS_ID)

    (record,) = result["records"]
    assert result["status"] == "ok"
    assert record["rs_id"] == _RS_ID
    assert record["access_date"]
    assert record["association_id"] == 226290633
    assert record["study_accession"] == "GCST90480652"
    assert record["trait"] == "platelet count"
    assert record["p_value"] == 3e-31
    assert record["confidence_interval"] == [0.12, 0.16]
    assert record["mapped_genes"] == ["HBB"]
    assert record["source_url"] == _ASSOCIATION_URL
    assert "does not establish causality" in record["interpretation"]
    assert record["page"]["total_elements"] == 134
    assert dict(requests[0].url.params) == {
        "rs_id": _RS_ID,
        "page": "0",
        "size": "20",
    }


async def test_invalid_rs_id_is_rejected_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = transport_responses(monkeypatch)

    result = await databases.search_gwas_catalog_associations("rs334 OR 1=1")

    assert result["records"] == []
    assert result["error"] == "rs_id must be an rs identifier such as rs334"
    assert requests == []


def _without_accession() -> dict[str, Any]:
    payload = _payload()
    del payload["_embedded"]["associations"][0]["accession_id"]
    return payload


@pytest.mark.parametrize(
    ("response", "expected_error"),
    [
        (httpx.ConnectError("offline"), "network_error"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.Response(429, json={"message": "secret"}), "429"),
        (httpx.Response(500, json={"message": "secret"}), "500"),
        (_without_accession(), "invalid_response"),
        *(
            (
                {
                    "page": {"totalElements": total},
                    "_links": {"self": {"href": "x"}},
                },
                "invalid_response",
            )
            for total in (1, None)
        ),
    ],
)
async def test_failures_are_errors_never_a_lookup_with_no_associations(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    response: Any,
    expected_error: str,
) -> None:
    caplog.set_level(logging.WARNING, logger=databases.__name__)
    transport_responses(monkeypatch, response)

    result = await databases.search_gwas_catalog_associations(_RS_ID)

    assert result["records"] == []
    assert expected_error in result["error"]
    assert "secret" not in repr(result) + caplog.text
