from __future__ import annotations

import json
from typing import Any


def sse_frame(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"
