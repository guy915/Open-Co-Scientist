# mypy: disable-error-code="import-untyped,misc"
from __future__ import annotations

from typing import Any

from requests.adapters import HTTPAdapter
from urllib3 import PoolManager
from urllib3.util.retry import Retry


class PeerAdapter(HTTPAdapter):
    def __init__(self, peer: str) -> None:
        self.peer = peer
        # Browsers repeat a safe request when an idle socket closes before
        # headers. Never replay a write, response status or streaming body.
        super().__init__(
            max_retries=Retry(
                total=1,
                read=1,
                connect=0,
                other=0,
                status=0,
                allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
            )
        )

    def init_poolmanager(
        self, connections: int, maxsize: int, block: bool = False, **kwargs: Any
    ) -> None:
        self.poolmanager = PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            source_address=(self.peer, 0),
            **kwargs,
        )
