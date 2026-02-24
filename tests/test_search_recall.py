"""Tests for mnemon.search.recall -- beam search and traversal params."""

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
