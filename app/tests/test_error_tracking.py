from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import sentry_sdk
from co_scientist.core.byok_scope import ByokCredential, scoped_byok
from co_scientist.platform.telemetry import error_tracking
from co_scientist.platform.telemetry.tracing import OTLP_HEADER_ENV_VARS

if TYPE_CHECKING:
    from sentry_sdk._types import Event
from sentry_sdk.attachments import Attachment
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport


def test_error_tracking_stays_off_without_a_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))

    assert error_tracking.init_error_tracking("  ", "production") is False
    assert calls == []


@pytest.mark.parametrize("name", OTLP_HEADER_ENV_VARS)
def test_error_reports_scrub_otlp_header_values_and_decoded_credentials(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "Authorization=Basic%20dummy-credential,x-honeycomb-team=dummy-team")
    secrets = error_tracking._deployment_secrets()
    event = {"message": "Basic%20dummy-credential Basic dummy-credential dummy-team"}
    assert error_tracking._scrub(event["message"], secrets) == ("[REDACTED] [REDACTED] [REDACTED]")


def test_reports_drop_request_headers_bodies_and_query_strings() -> None:
    event = {
        "request": {
            "method": "POST",
            "url": "https://api.example/api/runs",
            "headers": {"X-Client-ID": "owner-capability"},
            "cookies": {"session": "s"},
            "data": {"goal": "unpublished idea"},
            "query_string": "client_id=owner-capability",
        }
    }

    scrubbed = error_tracking.scrub_event(event, secrets=())

    assert scrubbed["request"] == {"method": "POST"}


def test_reports_redact_deployment_and_run_scoped_keys_everywhere() -> None:
    credential = ByokCredential(provider="openrouter", api_key="sk-run-scoped-key", model="m")
    event = {
        "exception": {"values": [{"value": "401 for sk-deploy-key-123 and sk-run-scoped-key"}]},
        "breadcrumbs": {"values": [{"message": "retrying with sk-deploy-key-123"}]},
    }

    with scoped_byok(credential):
        scrubbed = error_tracking.scrub_event(event, secrets=("sk-deploy-key-123",))

    assert scrubbed["exception"]["values"][0]["value"] == "Error details withheld for privacy"
    assert "breadcrumbs" not in scrubbed
    assert "sk-run-scoped-key" not in json.dumps(scrubbed)
    assert "sk-deploy-key-123" not in json.dumps(scrubbed)


def test_error_tracking_sends_no_pii_locals_bodies_or_traces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-deploy-secret")

    assert error_tracking.init_error_tracking("https://key@sentry.example/1", "production")

    (options,) = calls
    assert options["environment"] == "production"
    assert options["send_default_pii"] is False
    assert options["include_local_variables"] is False
    assert options["max_request_body_size"] == "never"
    assert options["traces_sample_rate"] is None
    assert options["before_send_transaction"]({}, {}) is None
    assert options["auto_enabling_integrations"] is False
    event = options["before_send"]({"message": "key sk-or-deploy-secret leaked"}, {})
    assert event["message"] == "Application error"
    assert options["max_breadcrumbs"] == 0
    assert options["before_breadcrumb"]({"message": "private"}, {}) is None


_PRIVATE = (
    "PRIVATE_GOAL",
    "PRIVATE_DOCUMENT",
    "researcher@example.test",
    "sk-unrecognized-private-key",
)


def _private_event() -> dict[str, Any]:
    private = " ".join(_PRIVATE)
    return {
        "event_id": "a" * 32,
        "message": private,
        "logentry": {"message": private},
        "request": {
            "method": "POST",
            "url": "https://api.example/" + private,
            "headers": {"X-Client-ID": private},
            "cookies": {"session": private},
            "data": private,
            "query_string": private,
            "env": private,
        },
        "user": {"email": _PRIVATE[2]},
        "extra": {"text": private},
        "contexts": {"private": {"goal": private}},
        "tags": {"private": private},
        "breadcrumbs": {"values": [{"message": private, "data": {"text": private}}]},
        "transaction": private,
        "fingerprint": [private],
        "exception": {
            "values": [
                {
                    "type": "ValueError",
                    "value": private,
                    "module": private,
                    "stacktrace": {
                        "frames": [
                            {
                                "abs_path": str(Path(error_tracking.__file__).resolve()),
                                "lineno": 42,
                                "colno": 3,
                                "function": private,
                                "vars": {"goal": private},
                                "pre_context": [private],
                                "context_line": private,
                                "post_context": [private],
                            },
                            {"filename": private, "lineno": 9},
                        ]
                    },
                }
            ]
        },
    }


def test_reports_keep_only_fixed_classification_method_and_trusted_source_positions() -> None:
    original = _private_event()
    before = json.dumps(original)
    event = error_tracking.scrub_event(original, secrets=())
    assert json.dumps(original) == before
    assert event["request"] == {"method": "POST"}
    assert event["message"] == "Application error"
    assert event["exception"]["values"] == [
        {
            "type": "ValueError",
            "value": "Error details withheld for privacy",
            "stacktrace": {
                "frames": [
                    {
                        "filename": "co_scientist/platform/telemetry/error_tracking.py",
                        "lineno": 42,
                        "colno": 3,
                    }
                ]
            },
        }
    ]
    for private in _PRIVATE:
        assert private not in json.dumps(event)


def test_reports_discard_arbitrary_exception_types_and_malformed_source_fields() -> None:
    event = error_tracking.scrub_event(
        {
            "event_id": _PRIVATE[2],
            "timestamp": 2**10000,
            "request": {"method": ["POST"]},
            "exception": {
                "values": [
                    {
                        "type": _PRIVATE[2],
                        "value": _PRIVATE[0],
                        "stacktrace": {"frames": [{"filename": [], "lineno": True}]},
                    }
                ]
            },
        },
        secrets=(),
    )
    assert event["exception"]["values"][0]["type"] == "Exception"
    assert event["exception"]["values"][0]["stacktrace"]["frames"] == []
    assert "request" not in event and "timestamp" not in event and "event_id" not in event
    assert _PRIVATE[2] not in json.dumps(event)


def test_real_sdk_outbound_envelope_excludes_private_text_and_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelopes: list[Envelope] = []

    class MemoryTransport(Transport):
        def capture_envelope(self, envelope: Envelope) -> None:
            envelopes.append(envelope)

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))
    assert error_tracking.init_error_tracking("https://key@sentry.example/1", "production")
    options = calls[0]
    options["transport"] = MemoryTransport
    options["default_integrations"] = False
    options["integrations"] = []
    client = sentry_sdk.Client(**options)
    try:
        client.capture_event(
            cast("Event", _private_event()),
            hint={
                "attachments": [Attachment(bytes=_PRIVATE[1].encode(), filename="private.txt")],
            },
        )
        client.flush()
        assert len(envelopes) == 1
        payload = envelopes[0].serialize().decode()
        assert "Application error" in payload and "ValueError" in payload
        assert len(envelopes[0].items) == 1
        for private in _PRIVATE:
            assert private not in payload
    finally:
        client.close()
