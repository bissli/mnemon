"""Tests for mnemon.search.intent -- intent detection and weights."""

import pytest
from mnemon.search.intent import detect_intent, get_weights, intent_from_string


def test_detect_why():
    """Why-related queries detect WHY intent."""
    for q in ['why did we choose SQLite',
              'the reason we chose Go because of motivation']:
        assert detect_intent(q) == 'WHY'


def test_detect_when():
    """Time-related queries detect WHEN intent."""
    for q in ['when was the database migrated',
              'timeline of changes',
              'what happened before the release']:
        assert detect_intent(q) == 'WHEN'


def test_detect_entity():
    """Entity-related queries detect ENTITY intent."""
    for q in ['what is MAGMA',
              'who is responsible for the API',
              'tell me about the graph engine']:
        assert detect_intent(q) == 'ENTITY'


def test_detect_general():
    """Non-specific queries detect GENERAL intent."""
    for q in ['SQLite performance tuning',
              'graph traversal algorithm']:
        assert detect_intent(q) == 'GENERAL'


def test_intent_from_string_valid():
    """Valid intent strings parse correctly."""
    assert intent_from_string('WHY') == 'WHY'
    assert intent_from_string('why') == 'WHY'
    assert intent_from_string(' When ') == 'WHEN'
    assert intent_from_string('ENTITY') == 'ENTITY'
    assert intent_from_string('general') == 'GENERAL'


def test_intent_from_string_invalid():
    """Invalid intent string raises ValueError."""
    with pytest.raises(ValueError):
        intent_from_string('BOGUS')


def test_get_weights_known():
    """All intents have weights summing to ~1.0."""
    for intent in ['WHY', 'WHEN', 'ENTITY', 'GENERAL']:
        w = get_weights(intent)
        assert len(w) > 0
        total = sum(w.values())
        assert 0.99 < total < 1.01


def test_get_weights_why_prioritizes_causal():
    """WHY intent has highest causal weight."""
    w = get_weights('WHY')
    assert w['causal'] > w['temporal']
    assert w['causal'] > w['semantic']
    assert w['causal'] > w['entity']


def test_get_weights_when_prioritizes_temporal():
    """WHEN intent has highest temporal weight."""
    w = get_weights('WHEN')
    assert w['temporal'] > w['causal']
    assert w['temporal'] > w['semantic']
    assert w['temporal'] > w['entity']


def test_get_weights_entity_prioritizes_entity():
    """ENTITY intent has highest entity weight."""
    w = get_weights('ENTITY')
    assert w['entity'] > w['temporal']
    assert w['entity'] > w['causal']


def test_get_weights_unknown_fallback():
    """Unknown intent falls back to GENERAL weights."""
    w = get_weights('NONEXISTENT')
    general = get_weights('GENERAL')
    for k, v in general.items():
        assert w[k] == v
