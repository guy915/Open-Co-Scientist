from typing import Any

from co_scientist.platform.llm.admission.service import reserve_physical as reserve_physical
from co_scientist.platform.llm.admission.service import scoped_client as scoped_client


def reserve(request: dict[str, Any]) -> None:
    reserve_physical(request, app=True)
