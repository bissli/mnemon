"""Semantic edge creation and candidate discovery."""

from datetime import datetime, timezone

from mnemon.embed.vector import cosine_similarity, deserialize_vector
from mnemon.model import Edge, Insight, format_float
from mnemon.search.keyword import content_similarity
from mnemon.store.edge import insert_edge
from mnemon.store.node import get_all_active_insights, get_all_embeddings
from mnemon.store.node import get_insight_by_id

MIN_SEMANTIC_SIMILARITY = 0.10
REVIEW_SEMANTIC_THRESHOLD = 0.40
AUTO_SEMANTIC_THRESHOLD = 0.80
MAX_SEMANTIC_CANDIDATES = 5
MAX_AUTO_SEMANTIC_EDGES = 3


def build_embed_cache(db: 'DB') -> dict[str, list[float]] | None:
    """Load all embeddings from DB into a map."""
    all_embedded = get_all_embeddings(db)
    if not all_embedded:
        return None
    cache: dict[str, list[float]] = {}
    for eid, _content, blob in all_embedded:
        v = deserialize_vector(blob)
        if v is not None:
            cache[eid] = v
    return cache or None


def create_semantic_edges(
        db: 'DB', insight: Insight,
        embed_cache: dict[str, list[float]] | None = None) -> int:
    """Auto-create semantic edges for insights with high cosine similarity."""
    if embed_cache is None:
        embed_cache = build_embed_cache(db)
    if embed_cache is None:
        return 0

    insight_vec = embed_cache.get(insight.id)
    if insight_vec is None:
        return 0

    scored = []
    for eid, other_vec in embed_cache.items():
        if eid == insight.id:
            continue
        cos_sim = cosine_similarity(insight_vec, other_vec)
        if cos_sim >= AUTO_SEMANTIC_THRESHOLD:
            scored.append((eid, cos_sim))

    if not scored:
        return 0

    scored.sort(key=lambda x: x[1], reverse=True)
    if len(scored) > MAX_AUTO_SEMANTIC_EDGES:
        scored = scored[:MAX_AUTO_SEMANTIC_EDGES]

    now = datetime.now(timezone.utc)
    count = 0
    for eid, sim in scored:
        meta = {
            'created_by': 'auto',
            'cosine': format_float(sim),
            }
        try:
            insert_edge(db, Edge(
                source_id=insight.id, target_id=eid,
                edge_type='semantic', weight=sim,
                metadata=meta, created_at=now))
            count += 1
        except Exception:
            pass
        try:
            insert_edge(db, Edge(
                source_id=eid, target_id=insight.id,
                edge_type='semantic', weight=sim,
                metadata=meta, created_at=now))
            count += 1
        except Exception:
            pass

    return count


def find_semantic_candidates(
        db: 'DB', insight: Insight,
        embed_cache: dict[str, list[float]] | None = None,
        ) -> list[dict]:
    """Return insights that are potential semantic matches."""
    if embed_cache is None:
        embed_cache = build_embed_cache(db)

    candidates = _find_candidates_by_embedding(db, insight, embed_cache)
    if candidates is not None:
        return candidates
    return _find_candidates_by_token_overlap(db, insight)


def _find_candidates_by_embedding(
        db: 'DB', insight: Insight,
        embed_cache: dict[str, list[float]] | None,
        ) -> list[dict] | None:
    """Use cosine similarity over the embed cache."""
    if embed_cache is None:
        return None

    insight_vec = embed_cache.get(insight.id)
    if insight_vec is None:
        return None

    scored = []
    for eid, other_vec in embed_cache.items():
        if eid == insight.id:
            continue
        cos_sim = cosine_similarity(insight_vec, other_vec)
        if cos_sim >= REVIEW_SEMANTIC_THRESHOLD:
            scored.append((eid, cos_sim))

    if not scored:
        return None

    scored.sort(key=lambda x: x[1], reverse=True)
    if len(scored) > MAX_SEMANTIC_CANDIDATES:
        scored = scored[:MAX_SEMANTIC_CANDIDATES]

    result = []
    for eid, sim in scored:
        ins = get_insight_by_id(db, eid)
        if ins is None:
            continue
        result.append({
            'id': ins.id,
            'content': ins.content,
            'category': ins.category,
            'similarity': sim,
            'auto_linked': sim >= AUTO_SEMANTIC_THRESHOLD,
            })

    return result or None


def _find_candidates_by_token_overlap(
        db: 'DB', insight: Insight) -> list[dict]:
    """Fallback: token overlap when embeddings unavailable."""
    all_insights = get_all_active_insights(db)
    if not all_insights:
        return []

    scored = []
    for other in all_insights:
        if other.id == insight.id:
            continue
        sim = content_similarity(insight.content, other.content)
        if sim >= MIN_SEMANTIC_SIMILARITY:
            scored.append((other, sim))

    scored.sort(key=lambda x: x[1], reverse=True)
    if len(scored) > MAX_SEMANTIC_CANDIDATES:
        scored = scored[:MAX_SEMANTIC_CANDIDATES]

    return [
        {
            'id': ins.id,
            'content': ins.content,
            'category': ins.category,
            'similarity': sim,
            'auto_linked': False,
            }
        for ins, sim in scored
        ]
