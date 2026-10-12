from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from co_scientist.domains.chat import qa
from co_scientist.domains.chat.qa import build_evidence_manifest
from co_scientist.domains.chat.repository import messages as store
from co_scientist.domains.chat.repository.messages import NewMessage
from co_scientist.platform.llm.offline_guard import OfflineModeError

from tests._client import create_run as _create_run
from tests._client import drain as _drain
from tests._client import fake_litellm as _fake_litellm
from tests._client import make_client as _client
from tests._client import wait_for_status as _wait_status
from tests._llm_fake_backend import install_completion_backend
from tests._process_mode_helpers import FakeProcessMode
from tests._store_helpers import seed_run


def _thinking_litellm(reasoning: str, prose: str) -> SimpleNamespace:

    async def _chunk_stream() -> AsyncIterator[Any]:
        for field, text in (
            ("reasoning_content", reasoning),
            ("content", prose),
        ):
            delta = SimpleNamespace(content=None, reasoning_content=None)
            setattr(delta, field, text)
            yield SimpleNamespace(choices=[SimpleNamespace(delta=delta)])

    async def _acompletion(**_kwargs: Any) -> AsyncIterator[Any]:
        return _chunk_stream()

    return SimpleNamespace(acompletion=_acompletion)


def _stream_answer(db_path: str, manifest: list[dict[str, Any]]) -> tuple[str, Any]:
    run = seed_run("goal", profile="default", client_id="c1", db_path=db_path)
    question = store.append_message(NewMessage(run.id, "user", "Q?", "qa"))
    frames = _drain(
        qa.stream_answer(
            run.id,
            qa.QaQuestion(text="Q?", message_id=question.id),
            qa.QaAnswerInputs("sys prompt", manifest),
        )
    )
    return "".join(frames), store.list_messages(run.id, db_path=db_path)[-1]


def test_a_streamed_answer_relays_frames_and_persists_sources_or_reasoning(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    reachable_provider: None,
) -> None:
    manifest = [{"n": 1, "evidence_id": "e1", "title": "T", "state": "verified"}]
    install_completion_backend(monkeypatch, _fake_litellm(["Ans", "", "wer"]).acompletion)
    body, answer = _stream_answer(isolated_db, manifest)
    assert '"type": "sources"' in body
    assert '"type": "chunk"' in body
    assert '"type": "done"' in body
    assert (answer.content, answer.meta) == ("Answer", {"sources": manifest})

    install_completion_backend(
        monkeypatch,
        _thinking_litellm("Checking the evidence", "Answer.").acompletion,
    )
    body, answer = _stream_answer(isolated_db, [])
    assert body.index('"type": "reasoning"') < body.index('"type": "chunk"')
    assert '"type": "sources"' not in body
    assert answer.content == "Answer."
    assert answer.meta == {"reasoning": "Checking the evidence"}


def test_a_failed_model_call_persists_and_emits_the_fallback_answer(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_completion_backend(
        monkeypatch,
        _fake_litellm([], raise_exc=RuntimeError("no key")).acompletion,
    )

    body, answer = _stream_answer(isolated_db, [])

    assert '"type": "error"' in body
    assert answer.content.startswith("Q&A requires")


def _completed_run_id() -> str:
    c = _client()
    rid = _create_run(c, "Investigate ferroptosis in cancer", tier="express").json()["id"]
    c.post(f"/api/runs/{rid}/start", json={})
    assert _wait_status(c, rid, "completed", timeout=30.0)
    return str(rid)


def test_ask_uses_real_llm_when_provider_key_present(
    monkeypatch: pytest.MonkeyPatch, fake_process_mode: FakeProcessMode
) -> None:
    rid = _completed_run_id()
    fake_process_mode.online()
    install_completion_backend(monkeypatch, (_fake_litellm(["Model ", "text"])).acompletion)

    c = _client()
    res = c.post(f"/api/runs/{rid}/messages/ask", json={"question": "Summary?"})

    assert res.status_code == 200
    body = res.text
    assert "Model " in body and "text" in body
    assert "offline mode" not in body.lower()

    msgs = store.list_messages(rid)
    answer = next(m for m in reversed(msgs) if m.kind == "qa" and m.sender == "system")
    assert answer.content == "Model text"


def _evidence(eid: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": eid,
        "title": f"Title {eid}",
        "source": "PubMed",
        "url": f"https://example/{eid}",
        "year": 2023,
        "available": True,
    }
    base.update(over)
    return base


def _citation(eid: str, state: str) -> dict[str, Any]:
    return {"evidence_id": eid, "state": state, "claim": "c"}


@pytest.mark.parametrize(
    ("evidence", "citations", "expected"),
    [
        (
            [_evidence("e1"), _evidence("e2"), _evidence("e3")],
            [_citation("e3", "verified"), _citation("e1", "partial")],
            [("e3", "verified"), ("e1", "partial"), ("e2", "available")],
        ),
        (
            [_evidence("e1"), _evidence("e2")],
            [_citation("e1", "unsupported"), _citation("e2", "verified")],
            [("e2", "verified")],
        ),
        (
            [_evidence("e1")],
            [
                _citation("ghost", "verified"),
                {"claim": "no evidence id", "state": "verified"},
                _citation("e1", "partial"),
            ],
            [("e1", "partial")],
        ),
    ],
    ids=[
        "cited-first",
        "unsupported-withheld",
        "unknown-or-anonymous-citations-ignored",
    ],
)
def test_manifest_lists_only_citable_evidence_strongest_state_first(
    evidence: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    expected: list[tuple[str, str]],
) -> None:
    manifest = build_evidence_manifest(evidence, citations)

    assert [(m["evidence_id"], m["state"]) for m in manifest] == expected
    assert [m["n"] for m in manifest] == list(range(1, len(expected) + 1))


def _failing_backend(exc: Exception) -> Any:
    async def _acompletion(**_kwargs: Any) -> AsyncIterator[Any]:
        raise exc

    return SimpleNamespace(acompletion=_acompletion)


def test_a_provider_outage_is_not_reported_as_a_missing_api_key(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    reachable_provider: None,
) -> None:
    """Every Q&A failure used to read as "set an API key", including a
    timeout against a configured provider.
    """
    install_completion_backend(
        monkeypatch, _failing_backend(TimeoutError("provider timed out")).acompletion
    )

    body, answer = _stream_answer(isolated_db, [])

    assert "API key" not in body
    assert "API key" not in answer.content
    # The reader is told the turn failed, and the persisted reply matches it.
    assert answer.content in body
    assert answer.content.strip()


def test_an_unconfigured_provider_still_says_so(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    reachable_provider: None,
) -> None:
    install_completion_backend(
        monkeypatch,
        _failing_backend(OfflineModeError("Q&A is not calling a provider")).acompletion,
    )

    body, answer = _stream_answer(isolated_db, [])

    assert "API key" in body
    assert answer.content in body


def test_a_rejected_key_says_the_provider_rejected_it(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    reachable_provider: None,
) -> None:
    from litellm.exceptions import AuthenticationError

    rejected = AuthenticationError("bad key", llm_provider="openrouter", model="model")
    install_completion_backend(monkeypatch, _failing_backend(rejected).acompletion)

    body, answer = _stream_answer(isolated_db, [])

    assert "rejected" in body
    assert answer.content in body
