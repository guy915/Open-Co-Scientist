from typing import Any

from co_scientist.core.json_schema import obj
from co_scientist.science.schemas.literature import LITERATURE_PAPER_ANALYSIS_SCHEMA

# Each entry is the single-paper analysis plus the paper's prompt number, so
# synthesis and observation read the same fields either way.
LITERATURE_PAPER_ANALYSIS_BATCH_SCHEMA: dict[str, Any] = {
    "name": "paper_analysis_batch",
    "strict": False,
    "schema": obj(
        {
            "analyses": {
                "type": "array",
                "items": obj(
                    {
                        "paper_index": {
                            "type": "integer",
                            "description": "The paper's number in the prompt: 1 for Paper 1.",
                        },
                        **LITERATURE_PAPER_ANALYSIS_SCHEMA["schema"]["properties"],
                    }
                ),
            }
        }
    ),
}
