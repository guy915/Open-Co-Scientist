from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from typing import Any

from co_scientist.domains.chat.seed import _read_snapshot
from co_scientist.domains.report import repository as reports
from co_scientist.domains.report.content import _normalize_research_directions
from co_scientist.platform.db import use_conn
from co_scientist.platform.db.models import DEMO_CLIENT_ID

# Fingerprint shipped summaries so curated corrections never replace owner edits.
_LEGACY_SUMMARIES = {
    "8b4bb024-4314-44ec-a8ef-48108bda7113": {
        "topic-4": "c1a2aee89e1093896d0f38ba6f6ce0c6d43ab206b6e633fef30de9363911829a",
        "topic-5": "f542c53cd4cc0b4dace3c0f908def1757ffb4fda0623360176aa210ca17e0937",
        "topic-6": "57f0cd1ae657b555bd9b476d1cd5441ca7487bc17e3533f4554c98ef9134ee9c",
    },
    "8b8b5183-6667-441c-8991-2eb8d35c7ec4": {
        "topic-4": "904151af6c6c98a4fc888b0aafffb403f6656626ed1541d3eb4619b8f9353d3f",
        "topic-5": "37b525c2e3527b62dffc0ca535d804f98628f40c1b65a1bdd26b2d468c2e7c1b",
        "topic-6": "86c7de3d92291eebaa8da98ba23a23d8dc682acd7b5c2561a79113668aa614b3",
    },
    "34bc7dbc-6c3c-4332-a061-f453c8d58020": {
        "topic-4": "e2ba3c500d432d8f8da254c54f3d82cb481947bc516430d876895f330ceadbf4",
        "topic-5": "870385ba03de4e12cc74fc09119c2aa36a9d4e5c5115862eab45b06ae2669abd",
        "topic-6": "24f51d9ee11a380204dd4d9624f47bd768aad5d8e27f5d3e276f5e9b97d32410",
    },
}
_V16_SUMMARIES = {
    "8b4bb024-4314-44ec-a8ef-48108bda7113": {
        "topic-1": "61313a789a692cc6aafb20db097c2d9a184ed88501f98b663610e7b74c4dadf9",
        "topic-2": "0845db3021cebc64971259a7877212f8429de44f9a6f67a0cf30e0b8d7358486",
        "topic-3": "e70aedae0b5f7be9680ae909d42b3061b2d2e9f8dad8530d68514a231c180c0d",
        "topic-4": "254f2afc066490d06e7871fe4992e08a3158d13041786eb924c15d5a5f3c1c0a",
        "topic-5": "524a534f56a6882f35c7c7b98afa67b71c685111fafc03bdb7a28ee9423407ed",
        "topic-6": "4bb396e5f0d40d59f9e3a3d65e17063233e5d21b0c060e24d7e2e20b1e80fa8d",
    },
    "8b8b5183-6667-441c-8991-2eb8d35c7ec4": {
        "topic-1": "01b0b5d3525e63f68eb54563c91333f7964a14673a6fe5b0a21e2f84455613da",
        "topic-2": "7486e462e07a898bf7f9b55353f26f60ae8f16169c8f71e8aaba12fde5103e6d",
        "topic-3": "48507337f9bacd7d26d5be04780485feda5acdc18ab80e10159e67c3ff3b53e4",
        "topic-4": "8a79973709be1d99129d4ecdadaa7c0fbece4e544dde1b3bdcf146df67a513bd",
        "topic-5": "b9a75d602536f9603c004b08e1e6bc6daaad949334f306c4553e9c5ce5e3ed65",
        "topic-6": "39f0793faa64d4c6fb6d252573dbe9386ccffaf3cf276a3e52edac41c1b98fa4",
    },
    "34bc7dbc-6c3c-4332-a061-f453c8d58020": {
        "topic-1": "ba991816ccdbd6ef8a04f3aef01e2ba798c077238aef176abbb0fe16ebcee6fa",
        "topic-2": "b44d9e01eb9fa96c50f46bdad0251589465fd61e1153f74a9d5fc7d49097583b",
        "topic-3": "8a57caa2fbe77e4459f9fc5dc957c110e32365a64c0bf1461ecdf87bfa4b44ae",
        "topic-4": "a60eb5720a959fa3ed97f48bdb74b3e1dddc971b280f289723cb8d478e84e560",
        "topic-5": "be91c3a5256f09e7befde7f7fe5c8dcf652cfec7162349d4fb31437fe8d0b7df",
        "topic-6": "c84116a8ecb0a5293dc273ed2495c38ea788988dcee22517e0f5af3f8df5968b",
    },
}
for _source, _topics in _V16_SUMMARIES.items():
    _LEGACY_SUMMARIES[_source].update(
        {key: value for key, value in _topics.items() if key in ("topic-1", "topic-2", "topic-3")}
    )
_SUMMARIES_BY_VERSION = {15: _LEGACY_SUMMARIES, 16: _V16_SUMMARIES}
_REFERENCE_FIELDS = ("title", "source", "url", "doi", "pmid")


def get_display_report(
    run_id: str, *, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    with use_conn(conn, None) as active:
        report = reports.get_latest_report(run_id, conn=active)
        if report:
            overview = report["payload"].get("research_overview")
            summary = overview.get("overview") if isinstance(overview, dict) else None
            if (
                isinstance(summary, dict)
                and "research_directions" in summary
                and summary["research_directions"] is None
            ):
                summary["research_directions"] = _normalize_research_directions(
                    summary["research_directions"]
                )
        return project_example_summaries(report, active) if report else None


def project_example_summaries(report: dict[str, Any], conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT config_json,client_id,status FROM runs WHERE id=?", (report["run_id"],)
    ).fetchone()
    if row is None or row["client_id"] == DEMO_CLIENT_ID or row["status"] != "completed":
        return report
    config = json.loads(row["config_json"])
    version = config.get("example_chat_version")
    if version not in (15, 16) or report["payload"].get("demo_seed_version") != version:
        return report
    snapshot = _read_snapshot()
    source = next(
        (
            item
            for item in snapshot["tables"]["reports"]
            if item["run_id"] == config.get("example_source_id")
        ),
        None,
    )
    if source is None:
        return report
    expected = json.loads(source["payload_json"])
    evidence = {
        item["id"]: item
        for item in snapshot["tables"]["evidence"]
        if item["run_id"] == source["run_id"]
    }
    projected = copy.deepcopy(report)
    payload = projected["payload"]
    topics = payload.get("knowledge_base", [])
    overview = payload.get("research_overview")
    markdown = projected["markdown_text"]
    if (
        not isinstance(topics, list)
        or not isinstance(overview, dict)
        or not isinstance(markdown, str)
    ):
        return report
    raw_topics = overview.get("knowledge_base", [])
    if not isinstance(raw_topics, list):
        return report
    head, marker, body = markdown.partition("## Knowledge Base\n")
    if not marker:
        return report
    knowledge, separator, tail = body.partition("\n## ")
    for topic, raw in zip(
        expected["knowledge_base"], expected["research_overview"]["knowledge_base"], strict=True
    ):
        candidates = [
            item
            for item in topics
            if isinstance(item, dict) and item.get("title") == topic["title"]
        ]
        raw_candidates = [
            item
            for item in raw_topics
            if isinstance(item, dict) and item.get("title") == topic["title"]
        ]
        if len(candidates) != 1 or len(raw_candidates) != 1:
            continue
        actual, actual_raw = candidates[0], raw_candidates[0]
        summary = actual.get("summary")
        if not isinstance(summary, str) or hashlib.sha256(
            summary.encode()
        ).hexdigest() != _SUMMARIES_BY_VERSION[version].get(source["run_id"], {}).get(topic["id"]):
            continue
        if actual_raw.get("summary") != summary or knowledge.count(summary) != 1:
            continue
        if {
            key: value for key, value in actual.items() if key not in ("summary", "reference_ids")
        } != {
            key: value for key, value in topic.items() if key not in ("summary", "reference_ids")
        }:
            continue
        if {key: value for key, value in actual_raw.items() if key != "summary"} != {
            key: value for key, value in raw.items() if key != "summary"
        }:
            continue
        if not _references_match(
            actual.get("reference_ids"), topic["reference_ids"], evidence, conn, report["run_id"]
        ):
            continue
        actual["summary"] = actual_raw["summary"] = topic["summary"]
        knowledge = knowledge.replace(summary, topic["summary"], 1)
    projected["markdown_text"] = head + marker + knowledge + separator + tail
    return projected


def _references_match(
    actual: Any,
    expected: list[str],
    evidence: dict[str, Any],
    conn: sqlite3.Connection,
    run_id: str,
) -> bool:
    if not isinstance(actual, list) or len(actual) != len(expected):
        return False
    for actual_id, expected_id in zip(actual, expected, strict=True):
        if not isinstance(actual_id, str) or expected_id not in evidence:
            return False
        reference = conn.execute(
            "SELECT title,source,url,doi,pmid FROM evidence WHERE id=? AND run_id=?",
            (actual_id, run_id),
        ).fetchone()
        if reference is None or tuple(reference) != tuple(
            evidence[expected_id].get(key) for key in _REFERENCE_FIELDS
        ):
            return False
    return True
