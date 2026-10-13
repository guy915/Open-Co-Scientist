"""Only exhausted durable retries may degrade optional science sections;
control-flow errors must propagate to the worker."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from co_scientist.core.exceptions import TASK_CONTROL_FLOW_ERRORS, short_error_text
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.telemetry.progress import record_schema_degradation

logger = logging.getLogger(__name__)


async def run_or_degrade(
    state: WorkflowState,
    synthesize: Callable[[], Awaitable[dict[str, Any]]],
    *,
    schema_name: str,
    fallback: Callable[[], dict[str, Any]],
    lost: str,
) -> dict[str, Any]:
    try:
        return await synthesize()
    except TASK_CONTROL_FLOW_ERRORS:
        raise
    except Exception as exc:
        # The worker supplies this attempt flag; absent state degrades.
        if state.get("durable_retries_remain"):
            raise
        # Provider messages may embed full completions; bound error text and
        # report its actual cause rather than labeling every failure an outage.
        logger.error(
            "Node '%s' failed and published nothing (%s); %s",
            schema_name,
            short_error_text(exc),
            lost,
        )
        record_schema_degradation(schema_name, state)
        return fallback()
