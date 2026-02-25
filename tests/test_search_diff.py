"""Tests for mnemon.search.diff -- duplicate/conflict detection."""

from mnemon.model import Insight
from mnemon.search.diff import classify_suggestion, diff


def test_classify_add():
    """Low similarity classifies as ADD."""
    assert classify_suggestion(0.3, 'completely new', 'existing') == 'ADD'


def test_classify_duplicate():
    """High similarity classifies as DUPLICATE."""
    assert classify_suggestion(0.95, 'very similar', 'very similar indeed') == 'DUPLICATE'


def test_classify_update():
    """Medium similarity classifies as UPDATE."""
    assert classify_suggestion(0.7, 'Go uses SQLite', 'Go uses PostgreSQL') == 'UPDATE'


def test_classify_conflict_negation():
    """Negation words with medium similarity classify as CONFLICT."""
    cases = [
        ('do not use Redis', 'use Redis for caching'),
        ('no longer supports Python 2', 'supports Python 2'),
        ('replaced Flask with FastAPI', 'uses Flask for API'),
    ]
    for new_text, existing in cases:
        assert classify_suggestion(0.7, new_text, existing) == 'CONFLICT'


def test_classify_boundary():
    """Boundary values: 0.65 not ADD, 0.9 not DUPLICATE."""
    got = classify_suggestion(0.65, 'some content', 'other content')
    assert got != 'ADD'
    got = classify_suggestion(0.9, 'some content', 'other content')
    assert got != 'DUPLICATE'


def test_classify_below_new_threshold():
    """Similarity below 0.65 classifies as ADD."""
    assert classify_suggestion(0.5, 'some text', 'other text') == 'ADD'
    assert classify_suggestion(0.6, 'some text', 'other text') == 'ADD'


def test_diff_token_only():
    """Diff finds matches via token similarity."""
    insights = [
        Insight(id='1', content='Go uses SQLite for persistent memory storage'),
        Insight(id='2', content='Python machine learning with TensorFlow'),
        Insight(id='3', content='Go uses SQLite for memory persistence'),
    ]
    result = diff(insights, 'Go uses SQLite for persistent memory storage')
    assert result['suggestion'] != 'ADD'
    assert len(result['matches']) > 0
    assert result['matches'][0]['id'] == '1'


def test_diff_no_matches():
    """No matching content returns ADD."""
    insights = [
        Insight(id='1', content='something about cooking recipes'),
    ]
    result = diff(insights, 'Go database library benchmarks')
    assert result['suggestion'] == 'ADD'


def test_diff_duplicate_overrides():
    """DUPLICATE in any match overrides overall suggestion."""
    insights = [
        Insight(id='1', content='Go uses SQLite for storage', importance=5),
        Insight(
            id='2', importance=3,
            content='Go uses SQLite for storage exactly'
                    ' the same content repeated verbatim'),
    ]
    result = diff(insights, 'Go uses SQLite for storage')
    assert result['suggestion'] == 'DUPLICATE'


def test_diff_limit_default():
    """Default limit caps matches at 5."""
    words = ['shared', 'words', 'database', 'memory', 'alpha', 'beta',
             'gamma', 'delta', 'epsilon', 'zeta']
    insights = [
        Insight(id=str(i),
                content=' '.join(words[:4 + (i % len(words))]),
                importance=i + 1)
        for i in range(20)
    ]
    result = diff(insights, 'shared words database memory')
    assert len(result['matches']) <= 5
