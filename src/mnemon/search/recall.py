"""Intent-aware recall with beam search, RRF, Kahn's topological sort."""

import heapq

from mnemon.embed.vector import cosine_similarity, deserialize_vector
from mnemon.model import Insight
from mnemon.search.intent import detect_intent, get_weights
from mnemon.search.keyword import insight_tokens, keyword_search, tokenize
from mnemon.store.edge import get_edges_by_node, get_edges_by_source_and_type
from mnemon.store.node import get_all_active_insights, get_all_embeddings
from mnemon.store.node import get_insight_by_id

ANCHOR_TOP_K = 20
LAMBDA1 = 1.0
LAMBDA2 = 0.4
RRF_K = 60
VECTOR_SEARCH_MIN_SIM = 0.10

TRAVERSAL_PARAMS: dict[str, tuple[int, int, int]] = {
    'WHY': (15, 5, 500),
    'WHEN': (10, 5, 400),
    'ENTITY': (10, 4, 400),
    'GENERAL': (10, 4, 500),
    }

RERANK_WITH_EMBED = (0.30, 0.15, 0.35, 0.20)
RERANK_NO_EMBED = (0.45, 0.25, 0.0, 0.30)


def get_traversal_params(intent: str) -> tuple[int, int, int]:
    """Return (beam_width, max_depth, max_visited) for the given intent."""
    return TRAVERSAL_PARAMS.get(intent, TRAVERSAL_PARAMS['GENERAL'])


def vector_search_from_cache(
        embed_cache: dict[str, list[float]],
        query_vec: list[float],
        limit: int) -> list[tuple[str, float]]:
    """Cosine similarity search over pre-loaded embeddings."""
    heap_list: list[tuple[float, str]] = []
    for id, vec in embed_cache.items():
        sim = cosine_similarity(query_vec, vec)
        if sim <= VECTOR_SEARCH_MIN_SIM:
            continue
        if limit <= 0 or len(heap_list) < limit:
            heapq.heappush(heap_list, (sim, id))
        elif sim > heap_list[0][0]:
            heapq.heapreplace(heap_list, (sim, id))

    if not heap_list:
        return []

    result = []
    while heap_list:
        sim, id = heapq.heappop(heap_list)
        result.append((id, sim))
    result.reverse()
    return result


def vector_search(
        db: 'DB', query_vec: list[float],
        limit: int) -> list[tuple[str, float]] | None:
    """Brute-force cosine similarity search, loading embeddings from DB."""
    db_embeds = get_all_embeddings(db)
    if not db_embeds:
        return None
    cache: dict[str, list[float]] = {}
    for eid, _content, blob in db_embeds:
        v = deserialize_vector(blob)
        if v is not None:
            cache[eid] = v
    return vector_search_from_cache(cache, query_vec, limit)


def beam_search_from_anchor(
        db: 'DB',
        start_id: str,
        start_score: float,
        query_vec: list[float] | None,
        weights: dict[str, float],
        params: tuple[int, int, int],
        score_map: dict[str, float],
        via_map: dict[str, str],
        insight_map: dict[str, Insight],
        embed_cache: dict[str, list[float]] | None) -> None:
    """Perform beam search from a single anchor node."""
    beam_width, max_depth, max_visited = params
    visited = {start_id: True}
    total_visited = 1

    current = [(-start_score, start_id, 0)]

    for depth in range(max_depth):
        if not current or total_visited >= max_visited:
            break

        next_items: list[tuple[float, str, int]] = []

        new_current = []
        for neg_score, nid, d in current:
            if d != depth:
                new_current.append((neg_score, nid, d))
                continue

            cur_score = -neg_score
            edges = get_edges_by_node(db, nid)

            for e in edges:
                if total_visited >= max_visited:
                    break
                neighbor_id = e.target_id
                if neighbor_id == nid:
                    neighbor_id = e.source_id

                structural = weights.get(e.edge_type, 0.0) * e.weight
                semantic = 0.0
                if query_vec is not None and embed_cache is not None:
                    n_vec = embed_cache.get(neighbor_id)
                    if n_vec is not None:
                        cos_sim = cosine_similarity(query_vec, n_vec)
                        if cos_sim > 0:
                            semantic = cos_sim
                neighbor_score = (
                    cur_score + LAMBDA1 * structural
                    + LAMBDA2 * semantic)

                existing = score_map.get(neighbor_id)
                if existing is None or neighbor_score > existing:
                    score_map[neighbor_id] = neighbor_score
                    via_map[neighbor_id] = e.edge_type
                    if neighbor_id not in insight_map:
                        ins = get_insight_by_id(db, neighbor_id)
                        if ins is not None:
                            insight_map[neighbor_id] = ins

                if neighbor_id not in visited:
                    visited[neighbor_id] = True
                    total_visited += 1
                    heapq.heappush(
                        next_items,
                        (-neighbor_score, neighbor_id, depth + 1))

        current = new_current

        pruned = []
        count = 0
        while next_items and count < beam_width:
            item = heapq.heappop(next_items)
            pruned.append(item)
            count += 1
        current = pruned


def causal_topological_sort(
        db: 'DB',
        results: list[dict]) -> list[dict]:
    """Reorder results so causes appear before effects using Kahn's algorithm."""
    if len(results) <= 1:
        return results

    id_set = {r['insight'].id for r in results}
    id_to_result = {r['insight'].id: r for r in results}

    adj: dict[str, list[str]] = {}
    in_degree: dict[str, int] = {r['insight'].id: 0 for r in results}

    for r in results:
        edges = get_edges_by_source_and_type(db, r['insight'].id, 'causal')
        for e in edges:
            if e.target_id in id_set:
                adj.setdefault(e.source_id, []).append(e.target_id)
                in_degree[e.target_id] += 1

    heap_list: list[tuple[float, str]] = []
    for r in results:
        rid = r['insight'].id
        if in_degree[rid] == 0:
            heapq.heappush(
                heap_list, (-id_to_result[rid]['score'], rid))

    ordered = []
    while heap_list:
        _neg_score, nid = heapq.heappop(heap_list)
        ordered.append(id_to_result[nid])
        for target in adj.get(nid, []):
            in_degree[target] -= 1
            if in_degree[target] == 0:
                heapq.heappush(
                    heap_list,
                    (-id_to_result[target]['score'], target))

    if len(ordered) < len(results):
        covered = {r['insight'].id for r in ordered}
        ordered.extend(r for r in results if r['insight'].id not in covered)

    return ordered


def intent_aware_recall(
        db: 'DB', query: str,
        query_vec: list[float] | None,
        query_entities: list[str],
        limit: int,
        intent_override: str | None = None) -> dict:
    """Perform MAGMA-aligned intent-aware retrieval."""
    if intent_override:
        intent = intent_override
        intent_source = 'override'
    else:
        intent = detect_intent(query)
        intent_source = 'auto'

    weights = get_weights(intent)
    params = get_traversal_params(intent)

    all_insights = get_all_active_insights(db)

    embed_cache: dict[str, list[float]] | None = None
    if query_vec is not None:
        db_embeds = get_all_embeddings(db)
        if db_embeds:
            embed_cache = {}
            for eid, _content, blob in db_embeds:
                v = deserialize_vector(blob)
                if v is not None:
                    embed_cache[eid] = v
    has_embeddings = embed_cache is not None and len(embed_cache) > 0

    anchor_map: dict[str, tuple[Insight, float, str]] = {}

    token_cache: dict[str, set[str]] = {}
    keyword_anchors = keyword_search(
        all_insights, query, ANCHOR_TOP_K, token_cache)
    for rank, (ins, _score) in enumerate(keyword_anchors):
        anchor_map[ins.id] = (
            ins, 1.0 / (RRF_K + rank + 1), 'keyword')

    if has_embeddings:
        vector_hits = vector_search_from_cache(
            embed_cache, query_vec, ANCHOR_TOP_K)
        for rank, (vid, _sim) in enumerate(vector_hits):
            rrf_score = 1.0 / (RRF_K + rank + 1)
            if vid in anchor_map:
                ins, old_score, _via = anchor_map[vid]
                anchor_map[vid] = (
                    ins, old_score + rrf_score, 'hybrid')
            else:
                ins = get_insight_by_id(db, vid)
                if ins is not None:
                    anchor_map[vid] = (ins, rrf_score, 'vector')

    time_sorted = sorted(
        all_insights, key=lambda i: i.created_at, reverse=True)
    time_limit = min(ANCHOR_TOP_K, len(time_sorted))
    for rank in range(time_limit):
        ins = time_sorted[rank]
        rrf_score = 1.0 / (RRF_K + rank + 1)
        if ins.id in anchor_map:
            a_ins, old_score, old_via = anchor_map[ins.id]
            new_via = old_via
            if old_via in {'keyword', 'vector'}:
                new_via = 'hybrid'
            anchor_map[ins.id] = (
                a_ins, old_score + rrf_score, new_via)
        else:
            anchor_map[ins.id] = (ins, rrf_score, 'time')

    max_anchor_score = max(
        (s for _, s, _ in anchor_map.values()), default=0)
    if max_anchor_score > 0:
        anchor_map = {
            k: (ins, s / max_anchor_score, via)
            for k, (ins, s, via) in anchor_map.items()
            }

    anchor_count = len(anchor_map)

    score_map: dict[str, float] = {}
    via_map: dict[str, str] = {}
    insight_map: dict[str, Insight] = {}

    for aid, (ins, score, via) in anchor_map.items():
        score_map[aid] = score
        via_map[aid] = via
        insight_map[aid] = ins

    for aid, (ins, score, via) in anchor_map.items():
        beam_search_from_anchor(
            db, aid, score, query_vec, weights, params,
            score_map, via_map, insight_map, embed_cache)

    traversed_count = len(score_map)

    query_tokens = tokenize(query)
    query_entity_set = {e.lower() for e in query_entities}

    candidates = []
    graph_min = None
    graph_max = None
    for cid, graph_raw in score_map.items():
        ins = insight_map.get(cid)
        if ins is None:
            continue
        if graph_min is None:
            graph_min = graph_raw
            graph_max = graph_raw
        else:
            graph_min = min(graph_min, graph_raw)
            graph_max = max(graph_max, graph_raw)
        candidates.append({
            'id': cid, 'ins': ins, 'via': via_map.get(cid, ''),
            'graph_raw': graph_raw,
            })

    if graph_min is None:
        graph_min = 0.0
        graph_max = 0.0
    graph_range = graph_max - graph_min
    if graph_range == 0:
        graph_range = 1.0

    for c in candidates:
        kw_score = 0.0
        if query_tokens:
            ct = token_cache.get(c['id'])
            if ct is None:
                ct = insight_tokens(c['ins'])
            intersection = sum(1 for t in query_tokens if t in ct)
            kw_score = intersection / len(query_tokens)

        ent_score = 0.0
        if query_entity_set:
            matched = sum(
                1 for e in c['ins'].entities
                if e.lower() in query_entity_set)
            ent_score = matched / max(1, len(query_entity_set))

        sim_score = 0.0
        if has_embeddings:
            n_vec = embed_cache.get(c['id'])
            if n_vec is not None:
                sim = cosine_similarity(query_vec, n_vec)
                if sim > 0:
                    sim_score = sim

        graph_score = (c['graph_raw'] - graph_min) / graph_range

        c['kw_score'] = kw_score
        c['ent_score'] = ent_score
        c['sim_score'] = sim_score
        c['graph_score'] = graph_score

    if has_embeddings:
        w_kw, w_ent, w_sim, w_gr = RERANK_WITH_EMBED
    else:
        w_kw, w_ent, w_sim, w_gr = RERANK_NO_EMBED

    results = []
    for c in candidates:
        final_score = (
            w_kw * c['kw_score'] + w_ent * c['ent_score']
            + w_sim * c['sim_score'] + w_gr * c['graph_score'])
        results.append({
            'insight': c['ins'],
            'score': final_score,
            'intent': intent,
            'via': c['via'],
            'signals': {
                'keyword': c['kw_score'],
                'entity': c['ent_score'],
                'similarity': c['sim_score'],
                'graph': c['graph_score'],
                },
            })

    results.sort(
        key=lambda r: (-r['score'], -r['insight'].importance))
    if limit > 0 and len(results) > limit:
        results = results[:limit]

    if intent == 'WHY':
        results = causal_topological_sort(db, results)

    hint = ''
    if not results or (limit > 0 and len(results) < limit // 2):
        hint = 'sparse_results'

    meta = {
        'intent': intent,
        'intent_source': intent_source,
        'anchor_count': anchor_count,
        'traversed': traversed_count,
        }
    if hint:
        meta['hint'] = hint

    return {'results': results, 'meta': meta}
