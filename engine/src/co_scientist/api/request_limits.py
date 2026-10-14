from __future__ import annotations

import asyncio
import tempfile
import threading
import time
from collections import Counter

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from co_scientist.api.auth import principal_for_request
from co_scientist.api.operator_access import has_admin_token
from co_scientist.core.exceptions import StorageAdmissionError
from co_scientist.platform.db.admission import connecting_host
from co_scientist.platform.db.storage_admission import reserve_write, scoped_peer

_lock = threading.Lock()
_buffering: Counter[str] = Counter()
_MAX_JSON_BYTES = 256_000
_MAX_UPLOAD_BYTES = 26 * 1024 * 1024


async def storage_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse({"detail": "storage admission exhausted"}, status_code=429)


def _reserve_input(owner: str, peer: str, size: int) -> None:
    try:
        reserve_write(owner, peer, size)
    except StorageAdmissionError:
        raise HTTPException(status_code=429, detail="input admission exhausted") from None


class RequestLimitsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        operator_control = path.rstrip("/") == "/api/launch-control" and has_admin_token(request)
        try:
            owner = "" if operator_control else principal_for_request(request).subject
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(
                scope, receive, send
            )
            return
        peer = connecting_host(request.client.host if request.client else None)
        limit, bucket, ceilings = _body_limits(path)
        if operator_control:
            limit, bucket, ceilings = 4096, "operator-control", (2, 2, 2)
        raw_length = request.headers.get("content-length")
        try:
            declared = int(raw_length) if raw_length is not None else 0
        except ValueError:
            await JSONResponse({"detail": "invalid content length"}, status_code=400)(
                scope, receive, send
            )
            return
        if declared < 0 or declared > limit:
            await JSONResponse({"detail": "request body is too large"}, status_code=413)(
                scope, receive, send
            )
            return
        keys = (f"global:{bucket}", f"owner:{bucket}:{owner}", f"host:{bucket}:{peer}")
        with _lock:
            admitted = all(
                _buffering[key] < ceiling for key, ceiling in zip(keys, ceilings, strict=True)
            )
            if admitted:
                _buffering.update(keys)
        if not admitted:
            await JSONResponse({"detail": "request buffering limit reached"}, status_code=429)(
                scope, receive, send
            )
            return
        try:
            with tempfile.SpooledTemporaryFile(max_size=65_536) as body:
                try:
                    size = await _read_body(body, receive, limit)
                except HTTPException as exc:
                    await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(
                        scope, receive, send
                    )
                    return
                if size is None:
                    return
                if not path.endswith("/adjudicate") and not operator_control:
                    try:
                        await asyncio.to_thread(_reserve_input, owner, peer, size)
                    except HTTPException as exc:
                        await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(
                            scope, receive, send
                        )
                        return
                # Keep the spool slot through admission so queued SQLite writers
                # cannot grow an unbounded backlog.
                self._release(keys)
                admitted = False
                body.seek(0)
                with scoped_peer(peer):
                    await self.app(scope, _body_replay(body, receive, size), send)
        finally:
            if admitted:
                self._release(keys)

    @staticmethod
    def _release(keys: tuple[str, ...]) -> None:
        with _lock:
            for key in keys:
                _buffering[key] -= 1
                if not _buffering[key]:
                    del _buffering[key]


async def _read_body(
    body: tempfile.SpooledTemporaryFile[bytes], receive: Receive, limit: int
) -> int | None:
    size = 0
    deadline = time.monotonic() + 30
    while True:
        try:
            message = await asyncio.wait_for(receive(), max(0, deadline - time.monotonic()))
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=408, detail="request body timed out") from exc
        if message["type"] == "http.disconnect":
            return None
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail="request body is too large")
        body.write(chunk)
        if not message.get("more_body", False):
            return size


def _body_replay(
    body: tempfile.SpooledTemporaryFile[bytes], receive: Receive, size: int
) -> Receive:
    replayed = False

    async def replay() -> Message:
        nonlocal replayed
        if replayed:
            return await receive()
        chunk = body.read(65_536)
        more = body.tell() < size
        replayed = not more
        if replayed:
            body.close()
        return {"type": "http.request", "body": chunk, "more_body": more}

    return replay


def _body_limits(path: str) -> tuple[int, str, tuple[int, int, int]]:
    if path == "/api/documents" or path.endswith("/attachments/upload"):
        return _MAX_UPLOAD_BYTES, "upload", (8, 2, 2)
    limit = 1024 * 1024 if path.endswith("/attachments") else _MAX_JSON_BYTES
    return limit, "json", (64, 16, 32)
