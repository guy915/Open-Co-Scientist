from __future__ import annotations

import asyncio
import hashlib
import hmac
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException, Request, Response

from co_scientist.core.config import settings
from co_scientist.orchestration.alert_markers import enabled
from co_scientist.platform import db
from co_scientist.platform.db.alert_markers import QueueFullError, enqueue
from co_scientist.platform.telemetry.alert_markers import parse_alert

router = APIRouter(tags=["alert delivery"])
_slots = threading.BoundedSemaphore(4)
_writers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="alert-enqueue")


@router.post("/webhooks/sentry/alerts", status_code=202)
async def receive_alert(request: Request) -> Response:
    if not enabled():
        raise HTTPException(503, "alert delivery is disabled")
    if not _slots.acquire(blocking=False):
        raise HTTPException(503, "alert receiver busy")
    handed_off = False
    try:
        async with asyncio.timeout(0.8):
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 256000:
                    raise HTTPException(413, "alert body too large")
            expected = hmac.new(
                settings.sentry_alert_client_secret.get_secret_value().encode(),
                raw,
                hashlib.sha256,
            ).hexdigest()
            supplied = request.headers.get("Sentry-Hook-Signature", "")
            if not hmac.compare_digest(expected.encode(), supplied.encode()):
                raise HTTPException(401, "invalid alert signature")
            if request.headers.get("Sentry-Hook-Resource") != "event_alert":
                raise HTTPException(400, "unsupported alert resource")
            try:
                marker = parse_alert(bytes(raw), settings.sentry_alert_organization)
            except ValueError:
                raise HTTPException(400, "unsupported alert") from None
            future = _writers.submit(enqueue, marker)
            handed_off = True
            future.add_done_callback(lambda _: _slots.release())
            await asyncio.wrap_future(future)
    except (TimeoutError, db.Error, QueueFullError):
        raise HTTPException(503, "alert receiver busy") from None
    finally:
        if not handed_off:
            _slots.release()
    return Response(status_code=202)
