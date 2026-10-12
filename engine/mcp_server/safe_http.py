from __future__ import annotations

import asyncio
import ipaddress
import socket
import zlib
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from mcp_server.workers import run_dns_worker


class UnsafeUrlError(ValueError):
    pass


class ResponseTooLargeError(httpx.HTTPError):
    pass


class _ZlibDecoder(Protocol):
    @property
    def eof(self) -> bool: ...

    @property
    def unconsumed_tail(self) -> bytes: ...

    @property
    def unused_data(self) -> bytes: ...

    def decompress(self, data: bytes, max_length: int = 0) -> bytes: ...


def _append_decompressed(
    decoder: _ZlibDecoder,
    pending: bytes,
    encoding: str,
    body: bytearray,
    max_bytes: int,
    allow_raw_deflate: bool,
) -> tuple[_ZlibDecoder, bool]:
    while pending:
        if encoding == "gzip" and decoder.eof:
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        remaining = max_bytes - len(body)
        try:
            decoded = decoder.decompress(pending, remaining + 1)
        except zlib.error as exc:
            if not allow_raw_deflate:
                raise httpx.DecodingError("invalid compressed response") from exc
            decoder = zlib.decompressobj(-zlib.MAX_WBITS)
            allow_raw_deflate = False
            try:
                decoded = decoder.decompress(pending, remaining + 1)
            except zlib.error as raw_exc:
                raise httpx.DecodingError("invalid compressed response") from raw_exc
        else:
            allow_raw_deflate = False
        if len(decoded) > remaining or decoder.unconsumed_tail:
            raise ResponseTooLargeError("response body exceeds byte limit")
        body.extend(decoded)
        pending = decoder.unused_data
        if pending and encoding != "gzip":
            raise httpx.DecodingError("trailing data after compressed response")
    return decoder, allow_raw_deflate


async def _bounded_body(response: httpx.Response, max_bytes: int) -> bytes:
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding not in {"", "identity", "gzip", "deflate"}:
        raise httpx.DecodingError(f"unsupported content encoding: {encoding}")
    decoder: _ZlibDecoder | None = None
    if encoding == "gzip":
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        decoder = zlib.decompressobj()
    allow_raw_deflate = encoding == "deflate"

    raw_bytes = 0
    body = bytearray()
    async for chunk in response.aiter_raw(chunk_size=64 * 1024):
        raw_bytes += len(chunk)
        if raw_bytes > max_bytes:
            raise ResponseTooLargeError("response body exceeds byte limit")
        if decoder is not None:
            decoder, allow_raw_deflate = _append_decompressed(
                decoder, chunk, encoding, body, max_bytes, allow_raw_deflate
            )
        else:
            if len(body) + len(chunk) > max_bytes:
                raise ResponseTooLargeError("response body exceeds byte limit")
            body.extend(chunk)
    if decoder is not None and not decoder.eof:
        raise httpx.DecodingError("incomplete compressed response")
    return bytes(body)


def _public_ip(host: str) -> str:
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            addresses = [
                ipaddress.ip_address(info[4][0]) for info in socket.getaddrinfo(host, None)
            ]
        except (OSError, ValueError) as exc:
            raise UnsafeUrlError("hostname does not resolve") from exc
    if not addresses:
        raise UnsafeUrlError("hostname does not resolve")
    for address in addresses:
        if not address.is_global:
            raise UnsafeUrlError("host resolves to a non-public address")
    return str(addresses[0])


def validate_http_url(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    try:
        valid_port = parsed.port is None or 1 <= parsed.port <= 65535
    except ValueError:
        valid_port = False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or not valid_port
    ):
        raise UnsafeUrlError("only absolute public http(s) URLs are allowed")
    host = httpx.URL(url).host
    if host.lower() in {"169.254.169.254", "metadata.google.internal"}:
        raise UnsafeUrlError("metadata hosts are blocked")
    ip = _public_ip(host)
    return host, ip


async def get_with_screened_redirects(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_redirects: int = 5,
    max_bytes: int = 10 * 1024 * 1024,
    timeout_seconds: float | None = 30,
) -> httpx.Response:
    try:
        async with asyncio.timeout(timeout_seconds):
            current = url
            for _ in range(max_redirects):
                host, ip = await run_dns_worker(validate_http_url, current)
                parsed = httpx.URL(current)
                authority = f"[{host}]" if ":" in host else host
                if parsed.port is not None:
                    authority = f"{authority}:{parsed.port}"
                # The URL names the pinned IP; the source keeps the validated
                # hostname so attribution survives (transports ignore the key).
                source = {"source_host": host, "source_url": current}
                request = client.build_request(
                    "GET",
                    parsed.copy_with(host=ip),
                    headers={"Host": authority, "Accept-Encoding": "identity"},
                    extensions={"sni_hostname": host, **source},
                )
                response = await client.send(request, stream=True, follow_redirects=False)
                try:
                    location = response.headers.get("location")
                    if response.is_redirect and location:
                        current = str(httpx.URL(current).join(location))
                        continue
                    if response.status_code >= 400:
                        return httpx.Response(
                            response.status_code,
                            headers=response.headers,
                            content=b"",
                            request=request,
                            extensions=source,
                        )
                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            declared_bytes = int(content_length)
                        except ValueError:
                            declared_bytes = 0
                        if declared_bytes > max_bytes:
                            raise ResponseTooLargeError("response body exceeds byte limit")
                    body = await _bounded_body(response, max_bytes)
                    headers = httpx.Headers(response.headers)
                    headers.pop("content-encoding", None)
                    headers.pop("content-length", None)
                    return httpx.Response(
                        response.status_code,
                        headers=headers,
                        content=body,
                        request=request,
                        extensions=source,
                    )
                finally:
                    await response.aclose()
            raise UnsafeUrlError("too many redirects")
    except TimeoutError as exc:
        raise httpx.ReadTimeout("aggregate request deadline exceeded") from exc
