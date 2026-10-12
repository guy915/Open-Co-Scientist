from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from urllib.error import URLError
from urllib.request import Request

import httpx
import pytest
from pydantic import SecretStr

from ._client import make_client

SECRET = "synthetic-webhook-secret"
INSTALLATION = "00000000-1111-2222-3333-444444444444"
ORGANIZATION = "test-organization"
PATH = "/webhooks/sentry/alerts"


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from co_scientist.core.config import settings
    from co_scientist.platform import db

    # These clients skip lifespan; production initializes the store before
    # accepting requests with a sub-second acknowledgement deadline.
    with db.connect():
        pass

    monkeypatch.setattr(settings, "sentry_alert_client_secret", SecretStr(SECRET), raising=False)
    monkeypatch.setattr(settings, "sentry_alert_organization", ORGANIZATION)
    monkeypatch.setattr(
        settings, "honeycomb_marker_api_key", SecretStr("synthetic-marker-key"), raising=False
    )


def payload(rule: str = "new error — API", project: str = "co-scientist-api") -> dict[str, Any]:
    return {
        "action": "triggered",
        "installation": {"uuid": INSTALLATION},
        "data": {
            "triggered_rule": rule,
            "event": {
                "event_id": "a" * 32,
                "datetime": datetime.now(timezone.utc).isoformat(),
                "url": (
                    f"https://sentry.io/api/0/projects/{ORGANIZATION}/{project}/events/{'a' * 32}/"
                ),
                "request": {"data": "PRIVATE RESEARCH"},
                "user": {"email": "private@example.invalid"},
            },
        },
    }


def post(body: dict[str, Any], signature: str | None = None) -> int:
    raw = json.dumps(body).encode()
    digest = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return (
        make_client()
        .post(
            PATH,
            content=raw,
            headers={
                "Sentry-Hook-Signature": signature or digest,
                "Sentry-Hook-Resource": "event_alert",
            },
        )
        .status_code
    )


def test_receiver_is_disabled_without_configuration() -> None:
    assert post(payload()) == 503


def test_receiver_is_disabled_without_an_organization(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from co_scientist.core.config import settings

    monkeypatch.setattr(settings, "sentry_alert_organization", "")
    assert post(payload()) == 503


@pytest.mark.parametrize("host", ["sentry.io", f"{ORGANIZATION}.sentry.io"])
def test_receiver_accepts_only_the_configured_organization_hosts(enabled: None, host: str) -> None:
    body = payload()
    body["data"]["event"]["url"] = body["data"]["event"]["url"].replace("sentry.io", host)
    assert post(body) == 202


def test_invalid_signature_cannot_enqueue(enabled: None) -> None:
    assert post(payload(), "0" * 64) == 401


@pytest.mark.parametrize(
    "change",
    [
        "installation",
        "organization",
        "project",
        "host",
        "scheme",
        "userinfo",
        "port",
        "query",
        "fragment",
        "encoded_organization",
        "rule",
        "expired",
        "future",
        "action",
    ],
)
def test_signed_unexpected_or_stale_events_are_rejected(enabled: None, change: str) -> None:
    body = payload()
    if change == "installation":
        body["installation"]["uuid"] = "other"
    elif change == "organization":
        body["data"]["event"]["url"] = (
            "https://sentry.io/api/0/projects/other/co-scientist-api/events/" + "a" * 32 + "/"
        )
    elif change in {"project", "encoded_organization"}:
        old, new = (
            ("co-scientist-api", "co-scientist-ui")
            if change == "project"
            else (ORGANIZATION, "%74est-organization")
        )
        body["data"]["event"]["url"] = body["data"]["event"]["url"].replace(old, new)
    elif change in {"query", "fragment"}:
        body["data"]["event"]["url"] += "?private=" if change == "query" else "#private"
    elif change in {"host", "scheme", "userinfo", "port"}:
        old, new = {
            "host": ("sentry.io", "other.sentry.io"),
            "scheme": ("https:", "http:"),
            "userinfo": ("sentry.io", "user@sentry.io"),
            "port": ("sentry.io", "sentry.io:443"),
        }[change]
        body["data"]["event"]["url"] = body["data"]["event"]["url"].replace(old, new)
    elif change == "rule":
        body["data"]["triggered_rule"] = "arbitrary private rule"
    elif change == "action":
        body["action"] = "created"
    else:
        epoch = time.time() + (600 if change == "future" else -8 * 86400)
        body["data"]["event"]["datetime"] = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    assert post(body) == 400


def test_body_limit_applies_before_json_parsing(enabled: None) -> None:
    response = make_client().post(PATH, content=b"x" * 256001)
    assert response.status_code == 413


@pytest.mark.parametrize(
    "rule,project",
    [
        ("new error — API", "co-scientist-api"),
        ("regressed error — API", "co-scientist-api"),
        ("error burst — API", "co-scientist-api"),
        ("new error — frontend", "co-scientist-ui"),
        ("regressed error — frontend", "co-scientist-ui"),
    ],
)
def test_valid_alert_delivers_only_fixed_marker_fields_and_deduplicates(
    enabled: None,
    monkeypatch: pytest.MonkeyPatch,
    rule: str,
    project: str,
) -> None:
    from co_scientist.orchestration.alert_markers import drain_once
    from co_scientist.platform.telemetry import alert_markers

    sent: list[Request] = []

    class Opener:
        @contextmanager
        def open(self, request: Request, timeout: float) -> Iterator[Any]:
            sent.append(request)
            yield type("Response", (), {"status": 201})()

    monkeypatch.setattr(alert_markers, "build_opener", lambda *args: Opener())
    body = payload(rule, project)
    assert post(body) == 202
    assert post(body) == 202
    drain_once()
    drain_once()
    assert len(sent) == 1
    assert sent[0].full_url == "https://api.eu1.honeycomb.io/1/markers/co-scientist-api"
    raw = sent[0].data
    assert isinstance(raw, bytes)
    data = json.loads(raw)
    assert data == {
        "message": rule,
        "type": "Sentry issue alert",
        "start_time": int(datetime.fromisoformat(body["data"]["event"]["datetime"]).timestamp()),
    }
    assert post(body) == 202
    drain_once()
    assert len(sent) == 1


def test_undelivered_alert_survives_restart_and_retries(
    enabled: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from co_scientist.orchestration.alert_markers import drain_once
    from co_scientist.platform import db
    from co_scientist.platform.telemetry import alert_markers

    attempts: list[Request] = []

    class Opener:
        @contextmanager
        def open(self, request: Request, timeout: float) -> Iterator[Any]:
            attempts.append(request)
            if len(attempts) == 1:
                raise URLError("do not log upstream details")
            yield type("Response", (), {"status": 201})()

    monkeypatch.setattr(alert_markers, "build_opener", lambda *args: Opener())
    assert post(payload()) == 202
    drain_once()
    db._initialized.clear()
    now = time.time()
    monkeypatch.setattr(db, "current_time", lambda: now + 301)
    drain_once()
    drain_once()
    assert len(attempts) == 2


async def test_timed_out_enqueues_keep_the_receiver_bounded(
    enabled: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from co_scientist.api import sentry_alerts
    from co_scientist.main import app

    release = threading.Event()
    entered = threading.Barrier(5)
    exited = threading.Barrier(5)

    def blocked_enqueue(marker: object) -> None:
        entered.wait(timeout=5)
        release.wait(timeout=5)
        exited.wait(timeout=5)

    monkeypatch.setattr(sentry_alerts, "enqueue", blocked_enqueue)
    raw = json.dumps(payload()).encode()
    headers = {
        "Sentry-Hook-Signature": hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest(),
        "Sentry-Hook-Resource": "event_alert",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        requests = [
            asyncio.create_task(client.post(PATH, content=raw, headers=headers)) for _ in range(4)
        ]
        await asyncio.to_thread(entered.wait, 5)
        try:
            responses = await asyncio.wait_for(asyncio.gather(*requests), 2)
            assert [response.status_code for response in responses] == [503] * 4
            assert (await client.post(PATH, content=raw, headers=headers)).status_code == 503
            assert not sentry_alerts._slots.acquire(blocking=False)
        finally:
            release.set()
            await asyncio.to_thread(exited.wait, 5)
