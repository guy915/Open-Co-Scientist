from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

RULE_PROJECTS = {
    "new error — API": "co-scientist-api",
    "regressed error — API": "co-scientist-api",
    "error burst — API": "co-scientist-api",
    "new error — frontend": "co-scientist-ui",
    "regressed error — frontend": "co-scientist-ui",
}


@dataclass(frozen=True)
class AlertMarker:
    identity: str
    label: str
    timestamp: int


def parse_alert(raw: bytes, organization: str) -> AlertMarker:
    # Ignore private event content entirely, including issue titles and URLs
    # beyond the exact signed project path used to bind the fixed label.
    try:
        if not organization:
            raise ValueError
        body = json.loads(raw)
        installation = body["installation"]["uuid"]
        if body["action"] != "triggered" or str(UUID(installation)) != installation:
            raise ValueError
        data = body["data"]
        label = data["triggered_rule"]
        project = RULE_PROJECTS[label]
        event = data["event"]
        event_id = event["event_id"]
        if not isinstance(event_id, str) or not re.fullmatch(r"[a-f0-9]{32}", event_id):
            raise ValueError
        url = urlsplit(event["url"])
        if (
            url.scheme != "https"
            or url.netloc not in {"sentry.io", f"{organization}.sentry.io"}
            or url.path != f"/api/0/projects/{organization}/{project}/events/{event_id}/"
            or url.query
            or url.fragment
        ):
            raise ValueError
        when = datetime.fromisoformat(event["datetime"].replace("Z", "+00:00"))
        if when.tzinfo is None or not -300 <= time.time() - when.timestamp() <= 7 * 86400:
            raise ValueError
        # Request headers are unsigned; replay identity must come from the body.
        identity = hashlib.sha256(f"{installation}:{label}:{event_id}".encode()).hexdigest()
        return AlertMarker(identity, label, int(when.timestamp()))
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise ValueError("unsupported alert") from exc


def send_marker(marker: AlertMarker, key: str) -> None:
    request = Request(
        "https://api.eu1.honeycomb.io/1/markers/co-scientist-api",
        data=json.dumps(
            {
                "message": marker.label,
                "type": "Sentry issue alert",
                "start_time": marker.timestamp,
            }
        ).encode(),
        headers={"X-Honeycomb-Team": key, "Content-Type": "application/json"},
        method="POST",
    )
    # A redirect must never forward the credential to another endpoint.
    with build_opener(HTTPRedirectHandlerDisabled()).open(request, timeout=3) as response:
        if response.status != 201:
            raise OSError("marker rejected")


class HTTPRedirectHandlerDisabled(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        return None
