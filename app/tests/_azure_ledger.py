from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from co_scientist.platform.db import default_db_path
from co_scientist.platform.llm.admission.spend import establish_allowance

EXPIRES_AT = "2099-01-04T00:00:00+00:00"


def record_azure_allowance(path: str | None = None, allowance: str = "1000000") -> None:
    """A store refuses Azure until an operator records an allowance."""
    establish_allowance(
        path or default_db_path() or "./coscientist.db",
        grant_eur=Decimal(allowance) + 1,
        prior_usage_eur=Decimal(0),
        buffer_eur=Decimal(1),
        expires_at=datetime.fromisoformat(EXPIRES_AT),
        cutoff_hours=Decimal(48),
        usd_to_eur=Decimal("0.88"),
    )
