"""Node keys persist in tasks and checkpoints; retain their values.
One registry supplies task callables and agent ownership."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.science import (
    evolution,
    generation,
    meta_review,
    proximity,
    ranking,
    reflection,
    supervisor,
)
from co_scientist.science import safety_screen as safety

NodeCallable = Callable[[WorkflowState], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class NodeSpec:
    agent: str
    node: NodeCallable


NODE_REGISTRY: dict[str, NodeSpec] = {
    "supervisor": NodeSpec("supervisor", supervisor.supervisor_node),
    "orchestrator": NodeSpec("supervisor", supervisor.orchestrator_node),
    "generate": NodeSpec("generation", generation.generate_node),
    "literature_review": NodeSpec("generation", generation.literature_review_node),
    "review": NodeSpec("reflection", reflection.review_node),
    "reflection": NodeSpec("reflection", reflection.reflection_node),
    "comprehensive_reflection": NodeSpec("reflection", reflection.comprehensive_reflection_node),
    "deep_verification": NodeSpec("reflection", reflection.deep_verification_node),
    "ranking": NodeSpec("ranking", ranking.ranking_node),
    "evolve": NodeSpec("evolution", evolution.evolve_node),
    "proximity": NodeSpec("proximity", proximity.proximity_node),
    "meta_review": NodeSpec("meta_review", meta_review.meta_review_node),
    "research_overview": NodeSpec("meta_review", meta_review.research_overview_node),
    "safety_screen": NodeSpec("safety", safety.safety_screen_node),
}

NODE_TO_AGENT: dict[str, str] = {key: spec.agent for key, spec in NODE_REGISTRY.items()}

__all__ = [
    "NODE_REGISTRY",
    "NODE_TO_AGENT",
    "NodeCallable",
    "NodeSpec",
    "evolution",
    "generation",
    "meta_review",
    "proximity",
    "ranking",
    "reflection",
    "safety",
    "supervisor",
]
