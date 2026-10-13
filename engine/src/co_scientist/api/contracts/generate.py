from __future__ import annotations

import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

from pydantic import BaseModel, TypeAdapter
from typing_extensions import is_typeddict

from co_scientist.api.contracts import common, interviews, reports, runs, science
from co_scientist.api.diagnostics_api import Connector, ProbeStatus, SystemStatusResponse
from co_scientist.api.feedback_api import FeedbackRequest
from co_scientist.api.logs_api import ClientLogRecord

GROUPS: tuple[ModuleType, ...] = (common, interviews, reports, runs, science)

# Endpoint models that are already the validated wire shape. FastAPI writes
# every field of a response model, so their fields are all required; request
# models keep their defaults optional.
MODEL_GROUPS: dict[str, dict[str, tuple[type[BaseModel], bool]]] = {
    "system": {
        "Connector": (Connector, True),
        "ProbeStatus": (ProbeStatus, True),
        "SystemStatusResponse": (SystemStatusResponse, True),
        "ClientLogRecord": (ClientLogRecord, False),
        "FeedbackRequest": (FeedbackRequest, False),
    },
}


def contracts() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for module in GROUPS:
        aliases = module.__annotations__
        owned = {
            name: value
            for name, value in vars(module).items()
            if not name.startswith("_")
            and (name in aliases or (is_typeddict(value) and value.__module__ == module.__name__))
        }
        result[module.__name__.rsplit(".", 1)[-1]] = owned
    result["common"].update(RunStatus=common.RunStatus, JsonValue=common.JsonValue)
    return result


def all_required(schema: dict[str, Any]) -> dict[str, Any]:
    for value in (schema, *schema.get("$defs", {}).values()):
        if "properties" in value:
            value["required"] = list(value["properties"])
    return schema


def model_schema(model: type[BaseModel], response: bool) -> dict[str, Any]:
    if response:
        return all_required(model.model_json_schema(mode="serialization"))
    return model.model_json_schema(mode="validation")


def schemas() -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    definitions: dict[str, dict[str, Any]] = {}
    owners: dict[str, str] = {}
    for group, values in contracts().items():
        for name, value in values.items():
            schema = TypeAdapter(value).json_schema()
            definitions.update(schema.pop("$defs", {}))
            if "$ref" not in schema:
                definitions[name] = schema
            owners[name] = group
    for group, models in MODEL_GROUPS.items():
        for name, (model, response) in models.items():
            schema = model_schema(model, response)
            definitions.update(schema.pop("$defs", {}))
            definitions[name] = schema
            owners[name] = group
    return definitions, owners


def reference(schema: dict[str, Any]) -> str:
    return str(schema["$ref"]).rsplit("/", 1)[-1]


def properties(schema: dict[str, Any]) -> list[str]:
    required = schema.get("required", [])
    fields = []
    for name, value in schema.get("properties", {}).items():
        key = name if re.fullmatch(r"[A-Za-z_$][\w$]*", name) else json.dumps(name)
        optional = "" if name in required else "?"
        fields.append(f"  {key}{optional}: {type_expression(value)};")
    if schema.get("x-open-config"):
        # Persisted config permits arbitrary extra keys alongside typed setup
        # fields; preserve that openness in the generated contract.
        fields.append("  [key: string]: JsonValue | RunSetupConfig | undefined;")
    return fields


def type_expression(schema: dict[str, Any]) -> str:
    if "$ref" in schema:
        return reference(schema)
    if "anyOf" in schema:
        return " | ".join(type_expression(value) for value in schema["anyOf"])
    if "enum" in schema or "const" in schema:
        values = schema.get("enum", [schema.get("const")])
        return " | ".join(json.dumps(value) for value in values)
    return structured_type(schema)


def structured_type(schema: dict[str, Any]) -> str:
    kind = schema.get("type")
    scalars = {
        "string": "string",
        "integer": "number",
        "number": "number",
        "boolean": "boolean",
        "null": "null",
    }
    if kind in scalars:
        return scalars[kind]
    if kind == "array":
        element = type_expression(schema["items"])
        element = f"({element})" if " | " in element else element
        return f"{element}[]"
    if kind == "object":
        return object_type(schema)
    if not schema or set(schema) <= {"title", "description"}:
        return "unknown"
    raise ValueError(f"Unsupported wire schema: {schema}")


def object_type(schema: dict[str, Any]) -> str:
    if "properties" in schema:
        return "{\n" + "\n".join(properties(schema)) + "\n}"
    extra = schema.get("additionalProperties", {})
    value = type_expression(extra) if isinstance(extra, dict) else "unknown"
    # Recursive aliases through Record trigger TypeScript TS2456; use an object
    # index instead.
    if value == "JsonValue":
        return f"{{[key: string]: {value}}}"
    return f"Record<string, {value}>"


def references(schema: Any) -> set[str]:
    if isinstance(schema, dict):
        found = {reference(schema)} if "$ref" in schema else set()
        for value in schema.values():
            found.update(references(value))
        return found
    if isinstance(schema, list):
        return set().union(*(references(value) for value in schema))
    return set()


def declaration(name: str, schema: dict[str, Any]) -> str:
    if "properties" in schema:
        body = "\n".join(properties(schema))
        return f"export interface {name} {{\n{body}\n}}"
    return f"export type {name} = {type_expression(schema)};"


API_DIR = Path(__file__).resolve().parents[5] / "app/frontend/src/shared/api"
HEADER = "// Generated from co_scientist.api.contracts; edit the backend models.\n"


def generated_files() -> dict[str, str]:
    definitions, owners = schemas()
    result = {}
    for group in sorted(set(owners.values())):
        names = sorted(name for name, owner in owners.items() if owner == group)
        needed = set().union(*(references(definitions[name]) for name in names))
        if "RunConfig" in names:
            needed.update({"JsonValue", "RunSetupConfig"})
        imports = []
        for owner in sorted(set(owners.values()) - {group}):
            refs = sorted(name for name in needed if owners[name] == owner)
            if refs:
                imports.append(f"import type {{{', '.join(refs)}}} from './wire_{owner}';")
        body = "\n\n".join(declaration(name, definitions[name]) for name in names)
        result[f"wire_{group}.ts"] = HEADER + "\n".join(imports) + "\n\n" + body + "\n"
    return result


def main() -> None:
    for name, content in generated_files().items():
        (API_DIR / name).write_text(content)


if __name__ == "__main__":
    main()
