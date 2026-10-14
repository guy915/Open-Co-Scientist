from typing import Any


def obj(
    properties: dict[str, Any],
    *,
    optional: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Closed objects prevent providers adding undeclared fields."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": [name for name in properties if name not in optional],
    }


def str_array(description: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
    if description is not None:
        node["description"] = description
    return node
