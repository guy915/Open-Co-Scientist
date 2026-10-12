from __future__ import annotations


# Persisted research records round-trip exactly, so they keep blank entries;
# model output is normalized, so it drops them.
def string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def stripped_string_items(value: object) -> list[str]:
    return [item.strip() for item in string_items(value) if item.strip()]
