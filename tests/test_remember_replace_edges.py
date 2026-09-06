"""Defensive edge sweep in `_apply_plan` for the replace/update flow.

When the LLM plan emits a causal edge that targets the same insight
being replaced, the order of operations in `_apply_plan` (supersede
first, edge inserts last) would leave a dangling edge pointing at the
just-superseded predecessor. The sweep at the end of the apply phase
removes any such edges.

The sweep must not touch edges to retained (current) history nodes.
"""

from datetime import datetime, timezone

from memman.pipeline.remember import FactPlan, _apply_plan
from memman.store.edge import get_edges_by_node, insert_edge
from memman.store.model import Edge
from memman.store.node import get_insight_by_id_include_deleted, insert_insight
from tests.conftest import make_edge, make_insight


def _make_plan(new_id, target_id, causal_edges):
    """Build a minimal replace FactPlan that points at `target_id`."""
    new_insight = make_insight(
        id=new_id, content='replacement content', importance=3)
    return FactPlan(
        action='replace',
        fact_text='replacement content',
        fact_insight=new_insight,
        targets=[(target_id, 'replace')],
        embed_vec=None,
        enrichment={},
        causal_edges=causal_edges,
        )


def test_apply_plan_sweeps_edges_pointing_at_replaced_target(tmp_db, tmp_backend):
    """Verify a replace supersedes the target and leaves it edgeless.

    Mutation: writing the pointer after `nodes.insert` (the successor
        then chains its temporal backbone to the row it replaced), or
        dropping the trailing `delete_by_node` sweep, which leaves the
        plan's own causal edge dangling into the predecessor.
    Oracle: the predecessor read back current-but-superseded
        (`deleted_at` null, `superseded_by` = the successor) with no
        edges, and the far endpoint present on the successor.
    """
    insert_insight(tmp_db, make_insight(id='old-1', content='original'))
    insert_insight(tmp_db, make_insight(id='ctx-1', content='context'))
    insert_edge(tmp_db, make_edge(
        source_id='ctx-1', target_id='old-1',
        edge_type='semantic', weight=0.6))

    now = datetime.now(timezone.utc)
    leaked = Edge(
        source_id='ctx-1', target_id='old-1',
        edge_type='causal', weight=1.0,
        metadata={}, created_at=now)

    plan = _make_plan(
        new_id='new-1', target_id='old-1', causal_edges=[leaked])
    _apply_plan(tmp_backend, plan, embed_cache={}, store_name='test')

    old = get_insight_by_id_include_deleted(tmp_db, 'old-1')
    assert old is not None
    assert old.deleted_at is None
    assert old.superseded_by == 'new-1'
    assert get_edges_by_node(tmp_db, 'old-1') == []
    moved = {(e.source_id, e.target_id, e.edge_type)
             for e in get_edges_by_node(tmp_db, 'new-1')}
    assert ('ctx-1', 'new-1', 'semantic') in moved


def test_apply_plan_preserves_edges_to_retained_history_nodes(tmp_db, tmp_backend):
    """Sweep targets only the replaced id, not other retained nodes."""
    insert_insight(tmp_db, make_insight(id='old-2', content='original'))
    insert_insight(
        tmp_db, make_insight(id='history-1', content='retained history'))

    pre_existing = make_edge(
        source_id='history-1', target_id='history-1',
        edge_type='temporal', weight=1.0)
    insert_edge(tmp_db, pre_existing)

    now = datetime.now(timezone.utc)
    new_edge = Edge(
        source_id='new-2', target_id='history-1',
        edge_type='causal', weight=1.0,
        metadata={}, created_at=now)

    plan = _make_plan(
        new_id='new-2', target_id='old-2', causal_edges=[new_edge])
    _apply_plan(tmp_backend, plan, embed_cache={}, store_name='test')

    edges = get_edges_by_node(tmp_db, 'history-1')
    edge_keys = {(e.source_id, e.target_id, e.edge_type) for e in edges}
    assert ('history-1', 'history-1', 'temporal') in edge_keys
    assert ('new-2', 'history-1', 'causal') in edge_keys

    deleted_edges = get_edges_by_node(tmp_db, 'old-2')
    assert len(deleted_edges) == 0
