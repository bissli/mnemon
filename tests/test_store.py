"""Store layer tests ported from Go store_test.go."""

import os
import pathlib

import pytest
from mnemon.model import base_weight, is_immune
from mnemon.store.db import DEFAULT_STORE_NAME, list_stores
from mnemon.store.db import open_db, read_active, store_dir, store_exists
from mnemon.store.db import valid_store_name, write_active
from mnemon.store.edge import find_insights_with_entity, get_edges_by_node
from mnemon.store.edge import get_edges_by_source_and_type, insert_edge
from mnemon.store.node import auto_prune, compute_effective_importance
from mnemon.store.node import get_all_active_insights, get_embedding
from mnemon.store.node import get_insight_by_id
from mnemon.store.node import get_insight_by_id_include_deleted
from mnemon.store.node import increment_access_count, insert_insight
from mnemon.store.node import query_insights, soft_delete_insight
from mnemon.store.node import update_embedding
from mnemon.store.oplog import get_oplog, log_op
from tests.conftest import make_edge, make_insight

# --- Insight CRUD ---


class TestInsertAndGetInsight:
    """Insert with tags/entities and verify round-trip."""

    def test_insert_and_get(self, tmp_db):
        """Insert insight with tags and entities, retrieve by id, verify fields."""
        ins = make_insight(
            id='ins-1',
            content='Go uses SQLite for storage',
            importance=3,
            tags=['go', 'sqlite'],
            entities=['Go', 'SQLite'])
        insert_insight(tmp_db, ins)

        got = get_insight_by_id(tmp_db, 'ins-1')
        assert got is not None
        assert got.content == ins.content
        assert got.importance == 3
        assert len(got.tags) == 2
        assert got.tags[0] == 'go'
        assert len(got.entities) == 2
        assert got.entities[0] == 'Go'


class TestGetInsightByIDNotFound:
    """Nonexistent ID returns None."""

    def test_not_found(self, tmp_db):
        """get_insight_by_id returns None for missing id."""
        got = get_insight_by_id(tmp_db, 'nonexistent')
        assert got is None


class TestSoftDeleteInsight:
    """Soft delete hides insight and removes edges."""

    def test_soft_delete(self, tmp_db):
        """Verify not found via get, found via include_deleted, edges deleted."""
        ins = make_insight(id='del-1', content='to be deleted', importance=2)
        insert_insight(tmp_db, ins)

        edge = make_edge(
            source_id='del-1', target_id='del-1',
            edge_type='temporal', weight=1.0)
        insert_edge(tmp_db, edge)

        soft_delete_insight(tmp_db, 'del-1')

        assert get_insight_by_id(tmp_db, 'del-1') is None

        got = get_insight_by_id_include_deleted(tmp_db, 'del-1')
        assert got is not None
        assert got.deleted_at is not None

        edges = get_edges_by_node(tmp_db, 'del-1')
        assert len(edges) == 0


class TestSoftDeleteInsightAlreadyDeleted:
    """Double delete raises ValueError."""

    def test_already_deleted(self, tmp_db):
        """Second soft_delete_insight raises ValueError."""
        ins = make_insight(
            id='del-2', content='already deleted', importance=2)
        insert_insight(tmp_db, ins)
        soft_delete_insight(tmp_db, 'del-2')

        with pytest.raises(ValueError):
            soft_delete_insight(tmp_db, 'del-2')


# --- Query ---


class TestQueryInsightsFilters:
    """Keyword, category, and importance filters."""

    def test_keyword_filter(self, tmp_db):
        """Keyword filter matches content via LIKE."""
        insert_insight(tmp_db, make_insight(
            id='q-1', content='Go language features',
            importance=5, category='fact'))
        insert_insight(tmp_db, make_insight(
            id='q-2', content='Python web framework',
            importance=2, category='decision'))
        insert_insight(tmp_db, make_insight(
            id='q-3', content='Go concurrency patterns',
            importance=4, category='fact'))

        results = query_insights(tmp_db, keyword='Go')
        assert len(results) == 2

    def test_category_filter(self, tmp_db):
        """Category filter matches exact category."""
        insert_insight(tmp_db, make_insight(
            id='q-1', content='Go language features',
            importance=5, category='fact'))
        insert_insight(tmp_db, make_insight(
            id='q-2', content='Python web framework',
            importance=2, category='decision'))
        insert_insight(tmp_db, make_insight(
            id='q-3', content='Go concurrency patterns',
            importance=4, category='fact'))

        results = query_insights(tmp_db, category='decision')
        assert len(results) == 1
        assert results[0].id == 'q-2'

    def test_min_importance_filter(self, tmp_db):
        """min_importance filter returns only high-importance insights."""
        insert_insight(tmp_db, make_insight(
            id='q-1', content='Go language features',
            importance=5, category='fact'))
        insert_insight(tmp_db, make_insight(
            id='q-2', content='Python web framework',
            importance=2, category='decision'))
        insert_insight(tmp_db, make_insight(
            id='q-3', content='Go concurrency patterns',
            importance=4, category='fact'))

        results = query_insights(tmp_db, min_importance=4)
        assert len(results) == 2


# --- Edges ---


class TestInsertAndGetEdges:
    """Insert edge and verify visibility from both sides."""

    def test_insert_and_get(self, tmp_db):
        """Edge visible from both source and target via get_edges_by_node."""
        insert_insight(tmp_db, make_insight(id='e-1', content='source'))
        insert_insight(tmp_db, make_insight(id='e-2', content='target'))

        edge = make_edge(
            source_id='e-1', target_id='e-2',
            edge_type='semantic', weight=0.85,
            metadata={'cosine': '0.8500'})
        insert_edge(tmp_db, edge)

        edges = get_edges_by_node(tmp_db, 'e-1')
        assert len(edges) == 1
        assert edges[0].edge_type == 'semantic'
        assert edges[0].metadata['cosine'] == '0.8500'

        edges = get_edges_by_node(tmp_db, 'e-2')
        assert len(edges) == 1


class TestGetEdgesBySourceAndType:
    """Filter edges by source and type."""

    def test_filter_by_type(self, tmp_db):
        """get_edges_by_source_and_type returns only matching type."""
        insert_insight(tmp_db, make_insight(id='st-1', content='a'))
        insert_insight(tmp_db, make_insight(id='st-2', content='b'))
        insert_insight(tmp_db, make_insight(id='st-3', content='c'))

        insert_edge(tmp_db, make_edge(
            source_id='st-1', target_id='st-2',
            edge_type='temporal', weight=1.0))
        insert_edge(tmp_db, make_edge(
            source_id='st-1', target_id='st-3',
            edge_type='semantic', weight=0.9))

        edges = get_edges_by_source_and_type(tmp_db, 'st-1', 'temporal')
        assert len(edges) == 1
        assert edges[0].target_id == 'st-2'


class TestFindInsightsWithEntity:
    """json_each entity lookup across insights."""

    def test_find_entity(self, tmp_db):
        """find_insights_with_entity returns ids matching entity, excluding self."""
        insert_insight(tmp_db, make_insight(
            id='fe-1', content='uses Go',
            entities=['Go', 'SQLite']))
        insert_insight(tmp_db, make_insight(
            id='fe-2', content='uses Python',
            entities=['Python']))
        insert_insight(tmp_db, make_insight(
            id='fe-3', content='also uses Go',
            entities=['Go']))

        ids = find_insights_with_entity(tmp_db, 'Go', 'fe-3', 10)
        assert len(ids) == 1
        assert ids[0] == 'fe-1'


# --- Transactions ---


class TestInTransactionCommit:
    """Committed transaction data persists."""

    def test_commit(self, tmp_db):
        """Insight inserted inside transaction is readable after commit."""
        def fn():
            insert_insight(
                tmp_db,
                make_insight(id='tx-1', content='in transaction'))

        tmp_db.in_transaction(fn)
        got = get_insight_by_id(tmp_db, 'tx-1')
        assert got is not None


class TestInTransactionRollback:
    """Rolled-back transaction data discarded."""

    def test_rollback(self, tmp_db):
        """Insight inserted inside a failing transaction is not readable."""
        def fn():
            insert_insight(
                tmp_db,
                make_insight(id='tx-2', content='will be rolled back'))
            raise RuntimeError('rollback')

        with pytest.raises(RuntimeError):
            tmp_db.in_transaction(fn)

        got = get_insight_by_id(tmp_db, 'tx-2')
        assert got is None


class TestInTransactionNested:
    """Nested transactions are rejected."""

    def test_nested(self, tmp_db):
        """Calling in_transaction inside another raises RuntimeError."""
        def fn():
            tmp_db.in_transaction(lambda: None)

        with pytest.raises(RuntimeError):
            tmp_db.in_transaction(fn)


# --- Lifecycle ---


class TestComputeEffectiveImportance:
    """EI formula: base * access_factor * decay * edge_factor."""

    def test_new_insight(self):
        """Brand new insight: importance=3, 0 accesses, 0 days, 0 edges."""
        ei = compute_effective_importance(3, 0, 0, 0)
        assert abs(ei - 0.5) < 0.01

    def test_max_importance(self):
        """Max importance (5) with no decay yields 1.0."""
        ei = compute_effective_importance(5, 0, 0, 0)
        assert ei == 1.0

    def test_decay(self):
        """After 30 days (one half-life), EI drops to ~50%."""
        fresh = compute_effective_importance(3, 0, 0, 0)
        decayed = compute_effective_importance(3, 0, 30, 0)
        ratio = decayed / fresh
        assert abs(ratio - 0.5) < 0.01

    def test_high_access(self):
        """Higher access count increases EI."""
        low = compute_effective_importance(3, 0, 0, 0)
        high = compute_effective_importance(3, 10, 0, 0)
        assert high > low

    def test_edge_bonus(self):
        """Edges increase EI."""
        no_edge = compute_effective_importance(3, 0, 0, 0)
        with_edge = compute_effective_importance(3, 0, 0, 5)
        assert with_edge > no_edge

    def test_edge_capped(self):
        """Edge factor caps at 5 edges."""
        e5 = compute_effective_importance(3, 0, 0, 5)
        e10 = compute_effective_importance(3, 0, 0, 10)
        assert e5 == e10


class TestIsImmune:
    """Immunity based on importance and access_count thresholds."""

    def test_importance_4_immune(self):
        """Importance >= 4 is immune."""
        assert is_immune(4, 0) is True

    def test_importance_5_immune(self):
        """Importance = 5 is immune."""
        assert is_immune(5, 0) is True

    def test_access_3_immune(self):
        """access_count >= 3 is immune."""
        assert is_immune(1, 3) is True

    def test_not_immune(self):
        """importance=3, access=2 is not immune."""
        assert is_immune(3, 2) is False

    def test_lowest_not_immune(self):
        """importance=1, access=0 is not immune."""
        assert is_immune(1, 0) is False


class TestBaseWeight:
    """Map importance level to base weight."""

    def test_weights(self):
        """Verify all importance-to-weight mappings."""
        cases = [
            (5, 1.0),
            (4, 0.8),
            (3, 0.5),
            (2, 0.3),
            (1, 0.15),
            ]
        for importance, want in cases:
            got = base_weight(importance)
            assert got == want, (
                f'base_weight({importance}): want {want}, got {got}')


# --- AutoPrune ---


class TestAutoPrune:
    """Capacity-based soft-delete of lowest-EI non-immune insights."""

    def test_prunes_lowest_ei(self, tmp_db):
        """Inserts 5 insights with max=3, expects 2 pruned."""
        for i in range(5):
            ins = make_insight(
                id=f'prune-{chr(ord("a") + i)}',
                content='content', importance=2)
            insert_insight(tmp_db, ins)

        pruned = auto_prune(tmp_db, 3)
        assert pruned == 2

        all_active = get_all_active_insights(tmp_db)
        assert len(all_active) == 3

    def test_respects_immune(self, tmp_db):
        """Immune insights (importance >= 4) are not pruned."""
        insert_insight(tmp_db, make_insight(
            id='immune-1', content='important', importance=4))
        insert_insight(tmp_db, make_insight(
            id='immune-2', content='also important', importance=5))
        insert_insight(tmp_db, make_insight(
            id='weak-1', content='low importance', importance=1))

        pruned = auto_prune(tmp_db, 1)
        assert pruned == 1

        got = get_insight_by_id(tmp_db, 'weak-1')
        assert got is None

    def test_respects_exclude_ids(self, tmp_db):
        """Excluded IDs survive even when eligible for pruning."""
        insert_insight(tmp_db, make_insight(
            id='ex-1', content='content a', importance=1))
        insert_insight(tmp_db, make_insight(
            id='ex-2', content='content b', importance=1))

        pruned = auto_prune(tmp_db, 0, exclude_ids=['ex-1'])
        assert pruned == 1

        got = get_insight_by_id(tmp_db, 'ex-1')
        assert got is not None

    def test_nothing_to_prune(self, tmp_db):
        """Under capacity returns 0 pruned."""
        insert_insight(tmp_db, make_insight(
            id='ok-1', content='content', importance=3))

        pruned = auto_prune(tmp_db, 10)
        assert pruned == 0


# --- Oplog ---


class TestOplog:
    """Operation log insert and retrieval."""

    def test_log_and_get(self, tmp_db):
        """Log two operations, verify order and fields."""
        log_op(tmp_db, 'remember', 'ins-1', 'test detail')
        log_op(tmp_db, 'recall', '', 'query: test')

        entries = get_oplog(tmp_db, 10)
        assert len(entries) == 2
        assert entries[0]['operation'] == 'recall'
        assert entries[1]['operation'] == 'remember'


# --- Embedding ---


class TestUpdateAndGetEmbedding:
    """Store and retrieve embedding blobs."""

    def test_round_trip(self, tmp_db):
        """Stored embedding blob is returned identically."""
        insert_insight(tmp_db, make_insight(
            id='emb-1', content='content'))

        blob = bytes([1, 2, 3, 4, 5, 6, 7, 8])
        update_embedding(tmp_db, 'emb-1', blob)

        got = get_embedding(tmp_db, 'emb-1')
        assert got is not None
        assert len(got) == 8


# --- GetAllActiveInsights ---


class TestGetAllActiveInsights:
    """Active insights excludes soft-deleted."""

    def test_excludes_deleted(self, tmp_db):
        """Soft-deleted insight is not returned."""
        insert_insight(tmp_db, make_insight(id='all-1', content='a'))
        insert_insight(tmp_db, make_insight(id='all-2', content='b'))
        insert_insight(tmp_db, make_insight(id='all-3', content='c'))
        soft_delete_insight(tmp_db, 'all-2')

        all_active = get_all_active_insights(tmp_db)
        assert len(all_active) == 2


# --- IncrementAccessCount ---


class TestIncrementAccessCount:
    """Bump access_count on an insight."""

    def test_increment(self, tmp_db):
        """Two increments result in access_count = 2."""
        insert_insight(tmp_db, make_insight(
            id='acc-1', content='content'))

        increment_access_count(tmp_db, 'acc-1')
        increment_access_count(tmp_db, 'acc-1')

        got = get_insight_by_id(tmp_db, 'acc-1')
        assert got.access_count == 2


# --- Store management ---


class TestValidStoreName:
    """Regex-based store name validation."""

    def test_valid_names(self):
        """Accepted name patterns."""
        assert valid_store_name('default') is True
        assert valid_store_name('my-store') is True
        assert valid_store_name('work_2024') is True
        assert valid_store_name('A') is True
        assert valid_store_name('a1') is True

    def test_invalid_names(self):
        """Rejected name patterns."""
        assert valid_store_name('') is False
        assert valid_store_name('-bad') is False
        assert valid_store_name('_bad') is False
        assert valid_store_name('has space') is False
        assert valid_store_name('has/slash') is False
        assert valid_store_name('has.dot') is False
        assert valid_store_name('.hidden') is False


class TestReadWriteActive:
    """Active store name persistence."""

    def test_default_when_missing(self, tmp_path):
        """No active file returns default store name."""
        got = read_active(str(tmp_path))
        assert got == DEFAULT_STORE_NAME

    def test_write_and_read(self, tmp_path):
        """Written name is read back correctly."""
        base = str(tmp_path)
        write_active(base, 'work')
        got = read_active(base)
        assert got == 'work'


class TestListStores:
    """Enumerate store directories."""

    def test_empty(self, tmp_path):
        """No data dir returns empty list."""
        names = list_stores(str(tmp_path))
        assert len(names) == 0

    def test_two_stores(self, tmp_path):
        """Two created stores returned sorted."""
        base = str(tmp_path)
        db1 = open_db(store_dir(base, 'alpha'))
        db1.close()
        db2 = open_db(store_dir(base, 'beta'))
        db2.close()

        names = list_stores(base)
        assert len(names) == 2
        assert names[0] == 'alpha'
        assert names[1] == 'beta'


class TestStoreExists:
    """Check existence of named store directory."""

    def test_does_not_exist(self, tmp_path):
        """Missing store returns False."""
        assert store_exists(str(tmp_path), 'nope') is False

    def test_exists_after_open(self, tmp_path):
        """Store exists after open_db creates it."""
        base = str(tmp_path)
        db = open_db(store_dir(base, 'yes'))
        db.close()
        assert store_exists(base, 'yes') is True


