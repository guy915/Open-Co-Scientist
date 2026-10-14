from __future__ import annotations

from fastapi import (
    APIRouter,
    Query,
    Request,
    Response,
)
from fastapi.responses import JSONResponse

import co_scientist.api.runs.crud as runs_deletion
from co_scientist.api.auth import client_id
from co_scientist.api.contracts import RunsResponse
from co_scientist.api.contracts.runs import Run, RunWithSummary
from co_scientist.api.runs import chat as runs_chat
from co_scientist.api.runs import collections as runs_collections
from co_scientist.api.runs import contrib as runs_contrib
from co_scientist.api.runs import crud as runs_crud
from co_scientist.api.runs import lifecycle as runs_lifecycle
from co_scientist.api.runs.events import _event_stream
from co_scientist.api.runs.lifecycle import (
    resume_interrupted_runs as resume_interrupted_runs,
)
from co_scientist.api.runs.stream_admission import AdmittedEventStream
from co_scientist.api.runs.support import (
    _run_or_404 as _run_or_404,
)
from co_scientist.core.async_bridge import off_loop
from co_scientist.orchestration.repository import events as store
from co_scientist.platform.db.admission import connecting_host

router = APIRouter(prefix="/api/runs", tags=["runs"])


# Literal /demo routes precede /{run_id} so first-match routing cannot consume
# them as
# IDs.
router.post("", response_model=Run)(runs_crud.create_run)
router.get("", response_model=RunsResponse)(runs_crud.list_runs)
router.get("/demo", response_model=RunsResponse)(runs_crud.list_demo_runs)
router.get("/{run_id}", response_model=RunWithSummary)(runs_crud.get_run)
router.patch("/{run_id}", response_model=RunWithSummary)(runs_crud.rename_run)

router.include_router(runs_lifecycle.router)

router.include_router(runs_deletion.router)


@router.get("/{run_id}/events")
@off_loop
def stream_events(
    run_id: str,
    request: Request,
    after: int = Query(0, ge=0),
    stream: bool = Query(True),
) -> Response:
    """Stream a run's events as Server-Sent Events, or list them as JSON.

    The default (``stream=true``) is the SSE contract the run views
    consume: replay history, then tail live events. ``stream=false``
    returns the persisted event log as a one-shot JSON snapshot for
    consumers that must not hold a connection open (e.g. the workbench
    diagnostics popover).

    Args:
        run_id: Path identifier of the run to stream.
        request: Incoming HTTP request, used to detect client disconnects.
        after: Only return events with a sequence number greater than this.
        stream: When false, return ``{"events": [...]}`` instead of SSE.

    Returns:
        A StreamingResponse that replays history then tails live events,
        or a JSONResponse snapshot when ``stream`` is false.
    """
    run = _run_or_404(run_id)
    if not stream:
        return JSONResponse({"events": store.list_events(run_id, after)})
    return AdmittedEventStream(
        _event_stream(run_id, request, after, run),
        client_id(request),
        connecting_host(request.client.host if request.client else None),
    )


router.include_router(runs_collections.router)
router.include_router(runs_contrib.router)
router.include_router(runs_chat.router)
