"""Duplicate/conflict detection for new content."""

from mnemon.embed.vector import cosine_similarity
from mnemon.model import Insight
from mnemon.search.keyword import content_similarity, keyword_search

NEGATION_WORDS = [
    'not', 'no longer', "don't", "doesn't", 'never',
    'switched from', 'instead of', 'rather than', 'replaced', 'deprecated',
    ]


def classify_suggestion(
        similarity: float, new_text: str,
        existing_text: str) -> str:
    """Classify the relationship based on similarity and negation signals."""
    if similarity < 0.5:
        return 'ADD'

    new_lower = new_text.lower()
    exist_lower = existing_text.lower()
    for neg in NEGATION_WORDS:
        if neg in new_lower or neg in exist_lower:
            return 'CONFLICT'

    if similarity > 0.9:
        return 'DUPLICATE'
    return 'UPDATE'


def diff(insights: list[Insight], new_content: str,
         limit: int = 5,
         new_embedding: list[float] | None = None,
         existing_embed: list[tuple[str, list[float]]] | None = None,
         ) -> dict:
    """Compare new content against existing insights and return a suggestion."""
    if limit <= 0:
        limit = 5

    candidates = keyword_search(insights, new_content, limit)

    embed_map: dict[str, list[float]] = {}
    if existing_embed:
        embed_map.update(dict(existing_embed))

    matches = []
    for ins, _kw_score in candidates:
        token_sim = content_similarity(new_content, ins.content)

        cosine_sim = 0.0
        if new_embedding is not None:
            exist_vec = embed_map.get(ins.id)
            if exist_vec is not None:
                cosine_sim = cosine_similarity(new_embedding, exist_vec)

        similarity = token_sim
        if cosine_sim >= 0.7 and cosine_sim > similarity:
            similarity = cosine_sim

        suggestion = classify_suggestion(
            similarity, new_content, ins.content)
        matches.append({
            'id': ins.id,
            'content': ins.content,
            'token_similarity': token_sim,
            'cosine_similarity': cosine_sim,
            'similarity': similarity,
            'suggestion': suggestion,
            })

    if new_embedding is not None and existing_embed:
        seen = {m['id'] for m in matches}
        cosine_pairs = []
        for eid, vec in existing_embed:
            if eid in seen:
                continue
            cs = cosine_similarity(new_embedding, vec)
            if cs >= 0.7:
                cosine_pairs.append((eid, cs))

        cosine_pairs.sort(key=lambda x: x[1], reverse=True)
        if len(cosine_pairs) > limit:
            cosine_pairs = cosine_pairs[:limit]

        insight_map = {ins.id: ins for ins in insights}
        for eid, cs in cosine_pairs:
            ins = insight_map.get(eid)
            if ins is None:
                continue
            token_sim = content_similarity(new_content, ins.content)
            similarity = token_sim
            if cs >= 0.7 and cs > similarity:
                similarity = cs
            suggestion = classify_suggestion(
                similarity, new_content, ins.content)
            if suggestion != 'ADD':
                matches.append({
                    'id': ins.id,
                    'content': ins.content,
                    'token_similarity': token_sim,
                    'cosine_similarity': cs,
                    'similarity': similarity,
                    'suggestion': suggestion,
                    })

    overall = 'ADD'
    if matches:
        overall = matches[0]['suggestion']
        for m in matches:
            if m['suggestion'] == 'DUPLICATE':
                overall = 'DUPLICATE'
                break

    return {'suggestion': overall, 'matches': matches}
