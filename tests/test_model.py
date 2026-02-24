"""Tests for mnemon.model -- Insight/Edge dataclasses and helpers."""

from datetime import datetime, timezone

from mnemon.model import VALID_CATEGORIES, VALID_EDGE_TYPES, Edge, Insight
from mnemon.model import base_weight, format_float, format_timestamp
from mnemon.model import is_immune, parse_timestamp


def test_tags_json_roundtrip():
    """Verify tags serialize/deserialize correctly."""
    ins = Insight(tags=['go', 'memory', 'graph'])
    j = ins.tags_json()
    restored = Insight()
    restored.parse_tags(j)
    assert restored.tags == ['go', 'memory', 'graph']


def test_tags_json_empty():
    """Empty tags produce '[]' JSON."""
    ins = Insight(tags=[])
    j = ins.tags_json()
    restored = Insight()
    restored.parse_tags(j)
    assert restored.tags == []


def test_parse_tags_null():
    """Null JSON string produces empty list."""
    ins = Insight()
    ins.parse_tags('null')
    assert ins.tags == []


def test_parse_tags_invalid_json():
    """Invalid JSON produces empty list."""
    ins = Insight()
    ins.parse_tags('not json')
    assert ins.tags == []


def test_entities_json_roundtrip():
    """Verify entities serialize/deserialize correctly."""
    ins = Insight(entities=['Go', 'SQLite', 'MAGMA'])
    j = ins.entities_json()
    restored = Insight()
    restored.parse_entities(j)
    assert restored.entities == ['Go', 'SQLite', 'MAGMA']


def test_parse_entities_null():
    """Null JSON string produces empty list."""
    ins = Insight()
    ins.parse_entities('null')
    assert ins.entities == []


def test_valid_categories():
    """All 6 categories accepted, invalid rejected."""
    for cat in ('preference', 'decision', 'fact',
                'insight', 'context', 'general'):
        assert cat in VALID_CATEGORIES
    assert 'bogus' not in VALID_CATEGORIES


def test_metadata_json_roundtrip():
    """Verify edge metadata serialize/deserialize correctly."""
    e = Edge(metadata={'sub_type': 'backbone', 'direction': 'precedes'})
    j = e.metadata_json()
    restored = Edge()
    restored.parse_metadata(j)
    assert restored.metadata['sub_type'] == 'backbone'
    assert restored.metadata['direction'] == 'precedes'


def test_metadata_json_empty():
    """Empty metadata produces '{}' JSON."""
    e = Edge(metadata={})
    j = e.metadata_json()
    restored = Edge()
    restored.parse_metadata(j)
    assert restored.metadata == {}


def test_parse_metadata_null():
    """Null JSON string produces empty dict."""
    e = Edge()
    e.parse_metadata('null')
    assert e.metadata == {}


def test_parse_metadata_invalid_json():
    """Invalid JSON produces empty dict."""
    e = Edge()
    e.parse_metadata('not json')
    assert e.metadata == {}


def test_valid_edge_types():
    """All 4 edge types accepted, invalid rejected."""
    for et in ('temporal', 'semantic', 'causal', 'entity'):
        assert et in VALID_EDGE_TYPES
    assert 'narrative' not in VALID_EDGE_TYPES


def test_insight_defaults():
    """Verify default values of Insight dataclass."""
    ins = Insight()
    assert ins.id == ''
    assert ins.content == ''
    assert ins.category == 'general'
    assert ins.importance == 3
    assert ins.tags == []
    assert ins.entities == []
    assert ins.source == 'user'
    assert ins.access_count == 0
    assert ins.deleted_at is None


def test_edge_defaults():
    """Verify default values of Edge dataclass."""
    e = Edge()
    assert e.source_id == ''
    assert e.target_id == ''
    assert e.edge_type == 'semantic'
    assert e.weight == 0.5
    assert e.metadata == {}


def test_base_weight_values():
    """Verify base_weight maps importance correctly."""
    assert base_weight(5) == 1.0
    assert base_weight(4) == 0.8
    assert base_weight(3) == 0.5
    assert base_weight(2) == 0.3
    assert base_weight(1) == 0.15


def test_is_immune():
    """Verify immunity rules."""
    assert is_immune(4, 0) is True
    assert is_immune(5, 0) is True
    assert is_immune(1, 3) is True
    assert is_immune(3, 2) is False
    assert is_immune(1, 0) is False


def test_format_timestamp():
    """Verify Z-suffix timestamp format."""
    dt = datetime(2024, 1, 15, 14, 30, 45, tzinfo=timezone.utc)
    assert format_timestamp(dt) == '2024-01-15T14:30:45Z'


def test_parse_timestamp_z():
    """Parse Z-suffix timestamp."""
    dt = parse_timestamp('2024-01-15T14:30:45Z')
    assert dt.year == 2024
    assert dt.hour == 14


def test_parse_timestamp_offset():
    """Parse +00:00 suffix timestamp."""
    dt = parse_timestamp('2024-01-15T14:30:45+00:00')
    assert dt.year == 2024


def test_format_float():
    """Verify 4 decimal place formatting."""
    assert format_float(0.85) == '0.8500'
    assert format_float(1.0) == '1.0000'
