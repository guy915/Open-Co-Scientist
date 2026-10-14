from __future__ import annotations

from collections.abc import Mapping

import httpx


# The one place tool transport policy is set. No client follows redirects on
# its own (web_fetch screens each hop); tools whose requests must not leave
# through an environment proxy pass honour_proxy_env=False.
def make_client(
    timeout: float,
    *,
    headers: Mapping[str, str] | None = None,
    honour_proxy_env: bool = True,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        headers=headers,
        trust_env=honour_proxy_env,
        follow_redirects=False,
    )
