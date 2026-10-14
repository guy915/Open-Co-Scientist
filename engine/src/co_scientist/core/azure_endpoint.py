from __future__ import annotations

import re
from urllib.parse import urlsplit

# The v1 docs name the first two; Foundry resources report the third as their
# endpoint. Stdlib only: the paid benchmark imports this before configuring.
RESOURCE_SUFFIXES = (
    ".openai.azure.com",
    ".services.ai.azure.com",
    ".cognitiveservices.azure.com",
)
_RESOURCE_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")


def resource_origin(endpoint: str) -> str | None:
    endpoint = endpoint.strip().rstrip("/")
    url = urlsplit(endpoint)
    host = (url.hostname or "").lower()
    suffix = next((suffix for suffix in RESOURCE_SUFFIXES if host.endswith(suffix)), None)
    try:
        port = url.port
    except ValueError:
        return None
    if (
        url.scheme != "https"
        or suffix is None
        or not _RESOURCE_NAME.fullmatch(host.removesuffix(suffix))
        or url.path
        or url.query
        or url.fragment
        or url.username is not None
        or url.password is not None
        or port not in (None, 443)
    ):
        return None
    return f"https://{host}"
