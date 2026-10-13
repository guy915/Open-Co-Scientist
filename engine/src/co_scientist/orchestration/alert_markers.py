from __future__ import annotations

import asyncio

from co_scientist.core.config import settings
from co_scientist.platform import db
from co_scientist.platform.db import alert_markers as outbox
from co_scientist.platform.telemetry.alert_markers import send_marker


def enabled() -> bool:
    return bool(
        settings.sentry_alert_organization
        and settings.sentry_alert_client_secret.get_secret_value()
        and settings.honeycomb_marker_api_key.get_secret_value()
    )


def drain_once() -> None:
    if not enabled():
        return
    pending = outbox.next_pending()
    if pending is None:
        return
    marker, attempts = pending
    try:
        send_marker(marker, settings.honeycomb_marker_api_key.get_secret_value())
    except OSError:
        # Upstream exception strings may include credentials or event details;
        # retain only retry state and never emit another Sentry error here.
        outbox.finish(marker.identity, attempts, delivered=False)
    else:
        outbox.finish(marker.identity, attempts, delivered=True)


async def delivery_loop() -> None:
    while True:
        work = asyncio.create_task(asyncio.to_thread(drain_once))
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            await work
            raise
        except db.Error:
            pass
        await asyncio.sleep(1)
