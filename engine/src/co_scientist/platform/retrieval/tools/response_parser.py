import json
import logging
import re
from typing import Any

from co_scientist.platform.retrieval.article import Article
from co_scientist.platform.retrieval.config.schema import ToolConfig
from co_scientist.platform.retrieval.tools.provider import parse_mcp_result as parse_mcp_result

logger = logging.getLogger(__name__)


def apply_transform(transform: str, value: Any) -> Any:
    # Default transformation precedes the None guard; non-None values pass
    # through.
    if transform.startswith("default:"):
        return _transform_default(transform, value)

    if value is None:
        return None

    return _apply_value_transform(transform, value)


def _apply_value_transform(transform: str, value: Any) -> Any:
    if transform.startswith("split:"):
        return _transform_split(transform, value)
    if transform.startswith("index:"):
        return _transform_index(transform, value)

    handlers = {
        "int": _transform_int,
        "float": _transform_float,
        "wrap_list": _transform_wrap_list,
    }
    handler = handlers.get(transform)
    if handler:
        return handler(value)

    logger.warning("unknown transform: %s", transform)
    return value


def _transform_default(transform: str, value: Any) -> Any:
    if value is not None:
        return value
    default_value = transform[8:]
    try:
        return int(default_value)
    except ValueError:
        return default_value


def _transform_split(transform: str, value: Any) -> Any:
    delimiter = transform[6:]
    if isinstance(value, str):
        return value.split(delimiter)
    return value


def _transform_index(transform: str, value: Any) -> Any:
    index = int(transform[6:])
    if isinstance(value, (list, tuple)) and len(value) > index:
        return value[index]
    return None


def _transform_int(value: Any) -> Any:
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _transform_float(value: Any) -> Any:
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _transform_wrap_list(value: Any) -> Any:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


_INDEXED_PART_RE = re.compile(r"(\w+)\[(\d+)\]")


def _is_quoted_literal(expr: str) -> bool:
    return expr.startswith("'") and expr.endswith("'")


def _url_from_key(dict_key: str | None) -> str | None:
    if dict_key:
        return f"https://pubmed.ncbi.nlm.nih.gov/{dict_key}/"
    return None


def _get_dict_field(current: Any, field: str) -> Any:
    return current.get(field) if isinstance(current, dict) else None


def _index_into_list(current: Any, index: str) -> Any:
    if current is None or not isinstance(current, list):
        return current
    idx = int(index)
    return current[idx] if idx < len(current) else None


class ResponseParser:
    """Source-specific response differences belong in YAML mappings rather than
    parsing branches.
    """

    def __init__(self, tool_config: ToolConfig):
        self.tool_config = tool_config
        self.response_format = tool_config.response_format

    def parse_response(self, response: Any) -> Any:
        if isinstance(response, str):
            response = response.strip()
            # Availability tools can return literal true/false rather than JSON
            # objects.
            if self.response_format.type == "boolean_string":
                return response.lower() == "true"
            try:
                return json.loads(response)
            except json.JSONDecodeError:
                logger.warning("failed to parse JSON response: %s...", response[:100])
                return response

        return response

    def parse_to_articles(self, response: Any) -> list[Article]:
        data = self.parse_response(response)

        if data is None:
            return []

        if isinstance(data, dict) and data.get("status") == "failed":
            return []
        if isinstance(data, dict) and data.get("status") == "ok":
            # IDs formerly carried by mapping keys now travel with each record.
            records = data["records"]
            if self.response_format.is_dict:
                return self._map_dict_results(
                    {
                        str(record.get("source_id") or index): record
                        for index, record in enumerate(records)
                    }
                )
            return self._map_list_results(records)
        results = self._navigate_path(data, self.response_format.results_path)

        if results is None:
            logger.warning("results_path returned None")
            return []

        articles = self._map_results(results, self.response_format.is_dict)

        logger.debug("parsed %s articles from response", len(articles))
        return articles

    def _map_results(self, results: Any, is_dict: bool) -> list[Article]:
        """One malformed item must not discard valid sibling results."""
        if is_dict:
            return self._map_dict_results(results)
        return self._map_list_results(results)

    def _map_dict_results(self, results: Any) -> list[Article]:
        """PubMed PMIDs live in mapping keys, needed by @key/@url_from_key."""
        articles = []

        if not isinstance(results, dict):
            logger.warning("expected dict but got %s", type(results))
            return []

        for key, item in results.items():
            try:
                article = self._map_item_to_article(item, dict_key=key)
                if article:
                    articles.append(article)
            except Exception as e:
                logger.error("failed to map item %s: %s", key, e)

        return articles

    def _map_list_results(self, results: Any) -> list[Article]:
        articles = []

        if not isinstance(results, list):
            results = [results]

        for i, item in enumerate(results):
            try:
                article = self._map_item_to_article(item)
                if article:
                    articles.append(article)
            except Exception as e:
                logger.error("failed to map item %s: %s", i, e)

        return articles

    def _navigate_path(self, data: Any, path: str) -> Any:
        if path == "." or not path:
            return data

        current = data
        for part in path.split("."):
            if current is None:
                return None
            current = self._navigate_path_part(current, part)

        return current

    def _navigate_path_part(self, current: Any, part: str) -> Any:
        match = _INDEXED_PART_RE.match(part)
        if match:
            return self._navigate_indexed_part(current, match)
        if isinstance(current, dict):
            return current.get(part)
        return None

    def _navigate_indexed_part(self, current: Any, match: "re.Match[str]") -> Any:
        field, index = match.groups()
        if field:
            current = _get_dict_field(current, field)
        return _index_into_list(current, index)

    def _map_item_to_article(
        self, item: dict[str, Any], dict_key: str | None = None
    ) -> Article | None:
        if not isinstance(item, dict):
            logger.warning("expected dict item but got %s", type(item))
            return None

        kwargs = self._map_fields(item, dict_key)

        if not kwargs.get("title"):
            logger.warning("article missing title, skipping")
            return None

        return Article(
            title=kwargs.get("title", ""),
            url=kwargs.get("url"),
            authors=kwargs.get("authors", []),
            year=kwargs.get("year"),
            venue=kwargs.get("venue"),
            citations=kwargs.get("citations", 0),
            abstract=kwargs.get("abstract"),
            content=kwargs.get("content"),
            source_id=kwargs.get("source_id"),
            # Use the configured source type when YAML provides no source
            # mapping.
            source=kwargs.get("source", self.tool_config.source_type),
            pdf_links=kwargs.get("pdf_links", []),
            used_in_analysis=True,
        )

    def _map_fields(self, item: dict[str, Any], dict_key: str | None) -> dict[str, Any]:
        mapping = self.response_format.field_mapping
        kwargs: dict[str, Any] = {}

        for article_field, expr in mapping.items():
            try:
                kwargs[article_field] = self._evaluate_expression(expr, item, dict_key)
            except Exception as e:
                logger.debug("failed to evaluate %s=%s: %s", article_field, expr, e)
                # A failed field mapping must not discard valid sibling results.
                kwargs[article_field] = None

        return kwargs

    def _evaluate_expression(
        self, expr: str, item: dict[str, Any], dict_key: str | None = None
    ) -> Any:
        # Quoted YAML values are constants, not response-field lookups.
        if _is_quoted_literal(expr):
            return expr[1:-1]

        if expr == "@key":
            return dict_key

        if expr == "@url_from_key":
            return _url_from_key(dict_key)

        if "|" in expr:
            return self._evaluate_transform_chain(expr, item, dict_key)

        return self._get_field_value(expr, item, dict_key)

    def _evaluate_transform_chain(
        self, expr: str, item: dict[str, Any], dict_key: str | None
    ) -> Any:
        parts = expr.split("|")
        field_expr = parts[0]
        transforms = parts[1:]

        value = self._get_field_value(field_expr, item, dict_key)
        for transform in transforms:
            value = apply_transform(transform, value)

        return value

    def _get_field_value(
        self, field_expr: str, item: dict[str, Any], dict_key: str | None = None
    ) -> Any:
        if field_expr == "@key":
            return dict_key

        if "." in field_expr:
            return self._navigate_path(item, field_expr)

        return item.get(field_expr)
