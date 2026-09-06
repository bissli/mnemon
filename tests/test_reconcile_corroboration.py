"""The reconcile NONE rung (D2) and the corroboration it records.

`_plan_fact`'s reconcile branch asks the LLM which stored memory
already captures a fact. A `NONE` verdict names that memory, so the
plan must carry the id: `_apply_plan` then bumps the named row's
`corroboration_count` rather than filing the restatement as
`already captured` and recording nothing.
"""

import uuid
from datetime import datetime, timezone

from memman.embed.fingerprint import bound_embedder
from memman.pipeline.remember import run_remember
from memman.store.model import Insight
from tests.conftest import make_insight

STORED = 'Redis caches session tokens for the web tier'
RESTATEMENT = 'Session tokens are cached in Redis'


def _new_insight(content):
    now = datetime.now(timezone.utc)
    return Insight(
        id=str(uuid.uuid4()), content=content, category='fact',
        importance=3, entities=[], source='test', access_count=0,
        created_at=now, updated_at=now)


def _store(backend, content):
    iid = str(uuid.uuid4())
    backend.nodes.insert(make_insight(id=iid, content=content))
    return iid


def _stub_none(monkeypatch, target_id):
    """Return a NONE verdict naming `target_id`, recording each call.

    The conftest mock decides NONE from word overlap, so a stub is
    what makes the verdict the fixture rather than the fixture's
    phrasing. The recorded calls separate a real failure from an
    empty shortlist, where `reconcile_memories` never runs at all.
    """
    calls = []

    def _fake(llm_client, fact, similar):
        calls.append((fact, similar))
        return {'action': 'NONE', 'targets': [(target_id, 'none')],
                'merged_text': None}

    monkeypatch.setattr('memman.llm.extract.reconcile_memories', _fake)
    return calls


def _run(backend, content, **kwargs):
    kwargs.setdefault('store_name', 'test')
    return run_remember(
        backend, _new_insight(content), content,
        ec=bound_embedder(backend), **kwargs)


def test_none_verdict_corroborates_the_named_memory(
        tmp_backend, monkeypatch):
    """A NONE verdict bumps the named row instead of storing a copy.

    Mutation: dropping `target_id` from the NONE branch's FactPlan --
        the restatement is filed as 'already captured' while the
        counter stays 0, which is the shipped defect.
    Oracle: the target's `corroboration_count` read back from the
        store, the active row count, and the `reconcile-corroborate`
        oplog rows.
    """
    tid = _store(tmp_backend, STORED)
    calls = _stub_none(monkeypatch, tid)
    res = _run(tmp_backend, RESTATEMENT)
    assert calls, 'reconcile never ran: the shortlist was empty'
    fact = res['facts'][0]
    assert fact['action'] == 'skipped'
    assert fact['reason'] == 'already captured'
    assert fact['target_id'] == tid
    assert tmp_backend.nodes.get(tid).corroboration_count == 1
    assert len(tmp_backend.nodes.get_all_active()) == 1
    logged = tmp_backend._db._query(
        'select insight_id from oplog'
        " where operation = 'reconcile-corroborate'").fetchall()
    assert [r[0] for r in logged] == [tid]


def test_none_verdict_counts_a_repeated_fact_once(
        tmp_backend, monkeypatch):
    """Two occurrences of one fact bump the target once.

    Mutation: dropping the `corroborated_ids` dedup argument, or
        seeding it after the bump rather than before -- an extractor
        emitting the same fact twice inflates one row's count to 2.
    Oracle: the counter read back, against the two facts the stubbed
        extractor emitted.
    """
    tid = _store(tmp_backend, STORED)
    monkeypatch.setattr(
        'memman.llm.extract.extract_facts',
        lambda *a, **kw: [
            {'text': RESTATEMENT, 'category': 'fact',
             'importance': 3, 'entities': []}
            for _ in range(2)])
    calls = _stub_none(monkeypatch, tid)
    res = _run(tmp_backend, RESTATEMENT)
    assert calls, 'reconcile never ran: the shortlist was empty'
    assert len(res['facts']) == 2
    assert tmp_backend.nodes.get(tid).corroboration_count == 1


def test_none_target_dead_at_apply_degrades_to_an_embedded_add(
        tmp_backend, monkeypatch):
    """A dead NONE target degrades to an add that keeps its vector.

    An external forget between planning and apply leaves the bump
    with nothing to increment, so the fact falls through to a plain
    add. That add reads `plan.embed_vec`, and the vector is already
    computed by the time the NONE branch returns.

    Mutation: dropping `embed_vec` from the NONE branch's FactPlan --
        the degraded add lands unembedded, invisible to the vector
        channel and never repaired, since the store has no
        null-embedding repair path.
    Oracle: the stored row's embedding read back from the store,
        against the same row's presence on the active list.
    """
    tid = _store(tmp_backend, STORED)
    stale_cache = {i.id: i for i in tmp_backend.nodes.get_all_active()}
    tmp_backend.nodes.soft_delete(tid)
    calls = _stub_none(monkeypatch, tid)
    res = _run(tmp_backend, RESTATEMENT, insights_by_id=stale_cache)
    assert calls, 'reconcile never ran: the shortlist was empty'
    fact = res['facts'][0]
    assert fact['action'] == 'add'
    assert fact['target_id'] == tid
    stored = tmp_backend.nodes.get(fact['id'])
    assert stored is not None
    assert tmp_backend.nodes.get_embedding(fact['id']) is not None
