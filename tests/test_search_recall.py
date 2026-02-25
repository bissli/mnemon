"""Tests for mnemon.search.recall -- beam search, traversal params, reranking."""

from mnemon.search.recall import RERANK_WEIGHTS, RERANK_WEIGHTS_NO_EMBED
from mnemon.search.recall import get_traversal_params


def test_get_traversal_params_known():
    """All known intents have valid params."""
    for intent in ['WHY', 'WHEN', 'ENTITY', 'GENERAL']:
        beam_width, max_depth, max_visited = get_traversal_params(intent)
        assert beam_width > 0
        assert max_depth > 0
        assert max_visited > 0


def test_get_traversal_params_why_larger_beam():
    """WHY has larger beam width than GENERAL."""
    why_beam, _why_depth, _why_vis = get_traversal_params('WHY')
    gen_beam, _gen_depth, _gen_vis = get_traversal_params('GENERAL')
    assert why_beam > gen_beam


def test_get_traversal_params_unknown_fallback():
    """Unknown intent falls back to GENERAL."""
    assert get_traversal_params('UNKNOWN') == get_traversal_params('GENERAL')


def test_rerank_weights_all_intents_present():
    """Both weight dicts cover all four intents."""
    for intent in ['WHY', 'WHEN', 'ENTITY', 'GENERAL']:
        assert intent in RERANK_WEIGHTS
        assert intent in RERANK_WEIGHTS_NO_EMBED


def test_rerank_weights_sum_to_one():
    """Each intent's weights sum to 1.0."""
    for intent, w in RERANK_WEIGHTS.items():
        assert abs(sum(w) - 1.0) < 1e-9, f'{intent} embed weights sum={sum(w)}'
    for intent, w in RERANK_WEIGHTS_NO_EMBED.items():
        assert abs(sum(w) - 1.0) < 1e-9, f'{intent} no-embed weights sum={sum(w)}'


def test_rerank_weights_no_embed_zero_similarity():
    """No-embed weights have zero similarity component."""
    for intent, w in RERANK_WEIGHTS_NO_EMBED.items():
        assert w[2] == 0.0, f'{intent} no-embed similarity weight should be 0'


def test_rerank_why_emphasizes_graph():
    """WHY intent weights graph score highest."""
    w_kw, w_ent, w_sim, w_gr = RERANK_WEIGHTS['WHY']
    assert w_gr > w_kw
    assert w_gr > w_ent
    assert w_gr > w_sim


def test_rerank_entity_emphasizes_entity():
    """ENTITY intent weights entity score highest."""
    w_kw, w_ent, w_sim, w_gr = RERANK_WEIGHTS['ENTITY']
    assert w_ent >= w_kw
    assert w_ent >= w_sim
    assert w_ent >= w_gr


def test_rerank_general_is_uniform():
    """GENERAL intent uses uniform weights."""
    w = RERANK_WEIGHTS['GENERAL']
    assert w[0] == w[1] == w[2] == w[3]
