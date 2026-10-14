from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from co_scientist.api.operator_access import has_admin_token
from co_scientist.core import inflight
from co_scientist.orchestration.task_worker import process_tag

router = APIRouter(tags=["deployment-operations"])
_QUIESCE = "/api/deployment/quiesce"


@router.get(_QUIESCE)
@router.post(_QUIESCE)
def quiesce(request: Request, response: Response) -> dict[str, bool | int | str]:
    if not has_admin_token(request):
        raise HTTPException(status_code=403, detail="Operator token required")
    response.headers["Cache-Control"] = "no-store"
    return {
        **inflight.quiesce_status(begin=request.method == "POST"),
        "process_tag": process_tag(),
    }


class DeploymentAdmissionMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] in {"GET", "HEAD", "OPTIONS"}
            or scope["path"] == _QUIESCE
        ):
            await self.app(scope, receive, send)
            return
        # Keep streamed replies and their final persistence inside the same
        # admission as ordinary writes; response headers are not completion.
        with inflight.operation_scope() as admitted:
            if not admitted:
                await JSONResponse(
                    {"detail": "Deployment is in progress. Retry shortly."},
                    status_code=503,
                    headers={"Retry-After": "30", "Cache-Control": "no-store"},
                )(scope, receive, send)
                return
            await self.app(scope, receive, send)
