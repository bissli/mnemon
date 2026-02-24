"""Shared fixtures for mnemon tests."""

from datetime import datetime, timezone

import pytest
from mnemon.model import Edge, Insight


@pytest.fixture
def tmp_db(tmp_path):
    """Fresh SQLite database in temp directory."""
    from mnemon.store.db import open_db
    db = open_db(str(tmp_path))
    yield db
    db.close()


@pytest.fixture
def populated_db(tmp_db):
    """DB pre-loaded with 5 insights for query/graph tests."""
    from mnemon.store.node import insert_insight
    now = datetime.now(timezone.utc)
    insights = [
        make_insight(id='pop-1', content='Go uses SQLite for storage',
                     importance=3, tags=['go', 'sqlite'],
                     entities=['Go', 'SQLite']),
        make_insight(id='pop-2', content='Python web framework comparison',
                     importance=2, category='decision'),
        make_insight(id='pop-3', content='Graph traversal algorithm for knowledge',
                     importance=4, tags=['graph'],
                     entities=['MAGMA']),
        make_insight(id='pop-4', content='Docker deployment strategy',
                     importance=5, category='preference',
                     entities=['Docker']),
        make_insight(id='pop-5', content='Go concurrency patterns',
                     importance=3, entities=['Go']),
    ]
    for ins in insights:
        insert_insight(tmp_db, ins)
    return tmp_db


def make_insight(**overrides) -> Insight:
    """Factory for test Insight instances."""
    now = datetime.now(timezone.utc)
    defaults = {
        'id': 'test-id',
        'content': 'test content',
        'category': 'fact',
        'importance': 3,
        'tags': [],
        'entities': [],
        'source': 'test',
        'access_count': 0,
        'created_at': now,
        'updated_at': now,
        'deleted_at': None,
        'last_accessed_at': None,
        'effective_importance': 0.0,
    }
    defaults.update(overrides)
    if 'tags' in overrides and overrides['tags'] is None:
        defaults['tags'] = []
    if 'entities' in overrides and overrides['entities'] is None:
        defaults['entities'] = []
    return Insight(**defaults)


def make_edge(**overrides) -> Edge:
    """Factory for test Edge instances."""
    now = datetime.now(timezone.utc)
    defaults = {
        'source_id': 'src',
        'target_id': 'tgt',
        'edge_type': 'semantic',
        'weight': 0.5,
        'metadata': {},
        'created_at': now,
    }
    defaults.update(overrides)
    return Edge(**defaults)
