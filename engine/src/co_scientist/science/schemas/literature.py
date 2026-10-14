from typing import Any

from co_scientist.core.json_schema import obj, str_array

RESEARCH_STANCES_SCHEMA: dict[str, Any] = {
    "name": "research_stances",
    "strict": False,
    "schema": obj(
        {
            "stances": str_array(
                "Distinct perspectives to research the goal from, each a"
                " short noun phrase (e.g. mechanism, contradicting"
                " evidence, methodology, prior art)"
            )
        }
    ),
}

RESEARCH_QUESTIONS_SCHEMA: dict[str, Any] = {
    "name": "research_questions",
    "strict": False,
    "schema": obj(
        {
            "questions": str_array(
                "Answerable questions this stance needs settled, each"
                " narrow enough for one literature search"
            )
        }
    ),
}

RESEARCH_QUERY_SCHEMA: dict[str, Any] = {
    "name": "research_query",
    "strict": False,
    "schema": obj(
        {
            "query": {
                "type": "string",
                "description": (
                    "The question rendered as a literature search query:"
                    " the terms a source indexes on, not a sentence"
                ),
            }
        }
    ),
}

RESEARCH_PLAN_SCHEMA: dict[str, Any] = {
    "name": "research_plan",
    "strict": False,
    "schema": obj(
        {
            "perspectives": {
                "type": "array",
                "description": (
                    "Distinct research perspectives, each paired with one"
                    " answerable question and a keyword search query"
                ),
                "items": obj(
                    {
                        "stance": {
                            "type": "string",
                            "description": "A distinct reason to search, in three words or fewer",
                        },
                        "question": {
                            "type": "string",
                            "description": "One focused, literature-answerable question",
                        },
                        "query": {
                            "type": "string",
                            "description": "Three to six search terms, not a sentence",
                        },
                    },
                    optional=("question", "query"),
                ),
            }
        }
    ),
}

RESEARCH_EXTRACT_SCHEMA: dict[str, Any] = {
    "name": "research_extract",
    "strict": False,
    "schema": obj(
        {
            "findings": {
                "type": "array",
                "description": (
                    "What the documents actually say about the question."
                    " Omit anything you cannot quote."
                ),
                "items": obj(
                    {
                        "document": {
                            "type": "integer",
                            "description": (
                                "Index of the document this comes from, as numbered in the prompt"
                            ),
                        },
                        "claim": {
                            "type": "string",
                            "description": ("The finding in your own words, one sentence"),
                        },
                        "quote": {
                            "type": "string",
                            "description": (
                                "The passage from that document which supports the claim, verbatim"
                            ),
                        },
                    }
                ),
            },
            "follow_ups": str_array(
                "Questions this reading raised and did not answer. Leave"
                " empty when the documents settled the question."
            ),
        }
    ),
}

RESEARCH_COMPRESS_SCHEMA: dict[str, Any] = {
    "name": "research_compress",
    "strict": False,
    "schema": obj(
        {
            "summary": {
                "type": "string",
                "description": (
                    "What this question's reading established, in a few"
                    " sentences, including where the sources disagree"
                ),
            }
        }
    ),
}


# Query schemas are source-agnostic; callers pair them directly with source-
# specific templates.
LITERATURE_QUERY_SCHEMA: dict[str, Any] = {
    "name": "pubmed_query_generation",
    "strict": False,
    "schema": obj(
        {
            "queries": {
                "type": "array",
                "description": ("Natural language search queries for PubMed literature search"),
                "items": {
                    "type": "string",
                    "description": (
                        "A focused search phrase covering a specific aspect of the research goal"
                    ),
                },
            }
        }
    ),
}
LITERATURE_PAPER_ANALYSIS_SCHEMA: dict[str, Any] = {
    "name": "paper_analysis",
    "strict": False,
    "schema": obj(
        {
            "key_findings": {
                "type": "string",
                "description": "main contributions and results from this work",
            },
            "gaps_identified": {
                "type": "string",
                "description": ("limitations or gaps explicitly mentioned by authors"),
            },
            "future_work": {
                "type": "string",
                "description": "future research suggested by the authors",
            },
            "methodology_limitations": {
                "type": "string",
                "description": "constraints or limitations in their methods",
            },
            "unexplored_areas": {
                "type": "string",
                "description": "topics mentioned but not investigated",
            },
            "relevance": {
                "type": "string",
                "description": "how this paper relates to the research goal",
            },
        }
    ),
}

# Candidate indices bound response size without echoing abstracts.
LITERATURE_RELEVANCE_BATCH_SCHEMA: dict[str, Any] = {
    "name": "literature_relevance_batch",
    "strict": False,
    "schema": obj(
        {
            "judgments": {
                "type": "array",
                "description": "One judgment per candidate in the batch.",
                "items": obj(
                    {
                        "index": {
                            "type": "integer",
                            "description": (
                                "The number assigned to the candidate in"
                                " the prompt: 1 for Candidate 1, 2 for"
                                " Candidate 2, and so on"
                            ),
                        },
                        "relevance": {
                            "type": "number",
                            "description": (
                                "how well this candidate's title and"
                                " abstract bear on the research goal,"
                                " from 0.0 (unrelated) to 1.0 (directly"
                                " on point)"
                            ),
                        },
                        "rationale": {
                            "type": "string",
                            "description": (
                                "one sentence stating why this candidate"
                                " does or does not bear on the research"
                                " goal"
                            ),
                        },
                    }
                ),
            }
        }
    ),
}
