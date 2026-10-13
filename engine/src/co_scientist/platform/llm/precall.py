from dataclasses import replace

from co_scientist.platform.llm.request.completion import _clamp_temperature
from co_scientist.platform.llm.values import LLMRequest


def _prepare_llm_call(request: LLMRequest) -> LLMRequest:
    return replace(
        request,
        temperature=_clamp_temperature(request.model_name, request.temperature),
    )
