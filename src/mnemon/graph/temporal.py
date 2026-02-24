"""Temporal edge creation (backbone chain + proximity)."""

from datetime import datetime, timezone

from mnemon.model import Edge, Insight
from mnemon.store.edge import insert_edge
from mnemon.store.node import get_latest_insight_by_source
from mnemon.store.node import get_recent_insights_in_window

TEMPORAL_WINDOW_HOURS = 24.0
MAX_PROXIMITY_EDGES = 10


def create_temporal_edge(db: 'DB', insight: Insight) -> int:
    """Create backbone and proximity temporal edges for a new insight."""
    now = datetime.now(timezone.utc)
    count = 0

    prev = get_latest_insight_by_source(db, insight.source, insight.id)
    if prev is not None:
        try:
            insert_edge(db, Edge(
                source_id=prev.id, target_id=insight.id,
                edge_type='temporal', weight=1.0,
                metadata={'sub_type': 'backbone', 'direction': 'precedes'},
                created_at=now))
            count += 1
        except Exception:
            pass
        try:
            insert_edge(db, Edge(
                source_id=insight.id, target_id=prev.id,
                edge_type='temporal', weight=1.0,
                metadata={'sub_type': 'backbone', 'direction': 'succeeds'},
                created_at=now))
            count += 1
        except Exception:
            pass

    recent = get_recent_insights_in_window(
        db, insight.id, TEMPORAL_WINDOW_HOURS, MAX_PROXIMITY_EDGES)
    if not recent:
        return count

    backbone_id = prev.id if prev else ''

    for near in recent:
        if near.id == backbone_id:
            continue

        hours_diff = abs(
            (insight.created_at - near.created_at).total_seconds() / 3600)
        weight = 1.0 / (1.0 + hours_diff)

        meta = {
            'sub_type': 'proximity',
            'hours_diff': f'{hours_diff:.2f}',
            }
        try:
            insert_edge(db, Edge(
                source_id=insight.id, target_id=near.id,
                edge_type='temporal', weight=weight,
                metadata=meta, created_at=now))
            count += 1
        except Exception:
            pass
        try:
            insert_edge(db, Edge(
                source_id=near.id, target_id=insight.id,
                edge_type='temporal', weight=weight,
                metadata=meta, created_at=now))
            count += 1
        except Exception:
            pass

    return count
