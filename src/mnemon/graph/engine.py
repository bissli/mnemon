"""Graph engine: orchestrates automatic edge creation when insights are stored."""

from mnemon.graph.causal import create_causal_edges
from mnemon.graph.entity import create_entity_edges, extract_entities
from mnemon.graph.entity import merge_entities
from mnemon.graph.semantic import create_semantic_edges
from mnemon.graph.temporal import create_temporal_edge
from mnemon.model import Insight


def on_insight_created(
        db: 'DB', insight: Insight,
        embed_cache: dict[str, list[float]] | None = None,
        ) -> dict[str, int]:
    """Run all edge generators for a newly created insight."""
    extracted = extract_entities(insight.content)
    insight.entities = merge_entities(insight.entities, extracted)

    stats = {
        'temporal': create_temporal_edge(db, insight),
        'entity': create_entity_edges(db, insight),
        'causal': create_causal_edges(db, insight),
        'semantic': create_semantic_edges(db, insight, embed_cache),
        }
    return stats
