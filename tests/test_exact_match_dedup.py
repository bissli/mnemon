"""Exact-match dedup rung (F3) and corroboration count (F4).

The rung sits inside `_plan_fact`'s reconcile branch: when exactly
one shortlist row matches the fact byte-for-byte (modulo case and
whitespace), the plan skips without an LLM call and carries the
target id; `_apply_plan` then bumps the target's
`corroboration_count` and writes a `reconcile-corroborate` oplog row.
"""

import uuid
from datetime import datetime, timezone

from memman.embed.fingerprint import bound_embedder
from memman.pipeline.remember import run_remember
from memman.store.model import Insight
from tests.conftest import make_insight


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


def _spy_reconcile(monkeypatch):
    """Replace reconcile_memories with a recording ADD stub.

    Isolates the rung from the conftest mock's overlap heuristic
    (which would return UPDATE for identical content), so a deleted
    rung shows up as action 'add' plus a recorded call, never as a
    coincidentally-identical outcome.
    """
    calls = []

    def _fake(llm_client, fact, similar):
        calls.append((fact, similar))
        return {'action': 'ADD', 'targets': [], 'merged_text': None}

    monkeypatch.setattr(
        'memman.llm.extract.reconcile_memories', _fake)
    return calls


def _run(backend, content, **kwargs):
    kwargs.setdefault('store_name', 'test')
    return run_remember(
        backend, _new_insight(content), content,
        ec=bound_embedder(backend), **kwargs)


def test_exact_match_single_hit_skips_llm(tmp_backend, monkeypatch):
    """One byte-identical stored row skips reconcile entirely.

    Mutation: deleting the rung -- identical content reaches the
        reconcile LLM call.
    Oracle: the spy records zero reconcile calls and the fact lands
        as 'skipped'.
    """
    _store(tmp_backend, 'Redis caches session tokens')
    calls = _spy_reconcile(monkeypatch)
    res = _run(tmp_backend, 'Redis caches session tokens')
    assert res['facts'][0]['action'] == 'skipped'
    assert calls == []


def test_exact_match_two_hits_escalates_to_llm(
        tmp_backend, monkeypatch):
    """Two identical stored rows fall through to the LLM.

    With two identical rows the store is already inconsistent, and
    which one to merge into is exactly the judgement worth an LLM
    call.

    Mutation: flipping `== 1` to `>= 1`.
    Oracle: the spy records exactly one reconcile call.
    """
    _store(tmp_backend, 'Redis caches session tokens')
    _store(tmp_backend, 'Redis caches session tokens')
    calls = _spy_reconcile(monkeypatch)
    res = _run(tmp_backend, 'Redis caches session tokens')
    assert len(calls) == 1
    assert res['facts'][0]['action'] == 'add'


def test_exact_match_is_not_substring_match(tmp_backend, monkeypatch):
    """A superset fact is not swallowed by its stored subset.

    Mutation: replacing the equality with `in` -- every superset fact
        would silently skip against its stored prefix.
    Oracle: the spy records one reconcile call and the fact is added.
    """
    _store(tmp_backend, 'Redis caches session tokens')
    calls = _spy_reconcile(monkeypatch)
    res = _run(
        tmp_backend, 'Redis caches session tokens for the api gateway')
    assert len(calls) == 1
    assert res['facts'][0]['action'] == 'add'


def test_exact_match_is_whitespace_and_case_insensitive(
        tmp_backend, monkeypatch):
    """Case and whitespace differences still count as exact.

    Mutation: dropping `.lower()` or the whitespace collapse from the
        normalisation.
    Oracle: differently-cased, differently-spaced content skips with
        zero reconcile calls.
    """
    _store(tmp_backend, 'Redis  Caches \t Session Tokens')
    calls = _spy_reconcile(monkeypatch)
    res = _run(tmp_backend, 'redis caches session tokens')
    assert res['facts'][0]['action'] == 'skipped'
    assert calls == []


def test_no_reconcile_bypasses_the_rung(tmp_backend, monkeypatch):
    """`--no-reconcile` stores verbatim even for identical content.

    The documented contract is "store verbatim, no judgement", and
    many CLI tests write identical content under the flag.

    Mutation: hoisting the rung above the `not no_reconcile` guard.
    Oracle: identical content lands as 'add' under the flag.
    """
    _store(tmp_backend, 'Redis caches session tokens')
    _spy_reconcile(monkeypatch)
    res = _run(
        tmp_backend, 'Redis caches session tokens', no_reconcile=True)
    assert res['facts'][0]['action'] == 'add'


def test_replace_of_identical_content_still_replaces(
        tmp_backend, monkeypatch):
    """`replace` with identical content must still replace.

    The CLI routes replace with `no_reconcile=... or
    bool(hint_replaced_id)`, so the rung must never intercept it.

    Mutation: the same hoist, reached via the replace route.
    Oracle: action is 'replace', the target row is gone, and the new
        row exists.
    """
    tid = _store(tmp_backend, 'Redis caches session tokens')
    _spy_reconcile(monkeypatch)
    res = _run(
        tmp_backend, 'Redis caches session tokens',
        no_reconcile=True, replaced_id=tid)
    assert res['facts'][0]['action'] == 'replace'
    assert tmp_backend.nodes.get(tid) is None
    assert tmp_backend.nodes.get(res['facts'][0]['id']) is not None


def test_exact_match_skip_bumps_corroboration_on_target(
        tmp_backend, monkeypatch):
    """Each exact-match skip bumps the TARGET's corroboration_count.

    Mutation: dropping the increment, or bumping the new fact's id
        instead of `plan.target_id`.
    Oracle: two identical writes leave the stored target at
        corroboration_count == 2; no other row exists to absorb a
        misdirected bump.
    """
    tid = _store(tmp_backend, 'Redis caches session tokens')
    _spy_reconcile(monkeypatch)
    _run(tmp_backend, 'Redis caches session tokens')
    _run(tmp_backend, 'Redis caches session tokens')
    stored = tmp_backend.nodes.get(tid)
    assert stored.corroboration_count == 2


def test_corroborate_writes_oplog_row(tmp_backend, monkeypatch):
    """The skip leaves a `reconcile-corroborate` oplog row.

    Mutation: dropping the `backend.oplog.log` call.
    Oracle: exactly one row with the operation name, carrying the
        target id.
    """
    tid = _store(tmp_backend, 'Redis caches session tokens')
    _spy_reconcile(monkeypatch)
    _run(tmp_backend, 'Redis caches session tokens')
    rows = tmp_backend._db._query(
        'select insight_id from oplog'
        " where operation = 'reconcile-corroborate'").fetchall()
    assert [r[0] for r in rows] == [tid]


def test_corroboration_does_not_inflate_access_count(
        tmp_backend, monkeypatch):
    """Corroboration never touches `access_count`.

    `access_count` means "times this row was returned" and nothing
    else, so a restatement must leave it alone -- otherwise the one
    counter that records retrieval starts recording writes too.

    Mutation: bumping `access_count` instead of (or alongside)
        `corroboration_count`.
    Oracle: after three exact-match skips the target's access_count
        is still 0 while corroboration_count reads 3.
    """
    tid = _store(tmp_backend, 'Redis caches session tokens')
    _spy_reconcile(monkeypatch)
    for _ in range(3):
        _run(tmp_backend, 'Redis caches session tokens')
    stored = tmp_backend.nodes.get(tid)
    assert stored.corroboration_count == 3
    assert stored.access_count == 0


def test_corroborate_adopts_restating_queue_uuid(
        tmp_backend, monkeypatch):
    """The corroborated target adopts the restating row's queue_uuid.

    An all-skips queue row inserts nothing carrying its uuid, so a
    worker crash between the commit and `mark_done` reclaims the row
    and the replay guard (`has_active_with_queue_uuid`) finds
    nothing -- the bump repeats on every reclaim.

    Mutation: dropping the queue_uuid adoption from the bump.
    Oracle: after the skip, the target carries the restating uuid
        and the replay guard fires for it.
    """
    tid = _store(tmp_backend, 'Redis caches session tokens')
    _spy_reconcile(monkeypatch)
    parent = _new_insight('Redis caches session tokens')
    parent.queue_uuid = 'q-restate-1'
    run_remember(
        tmp_backend, parent, 'Redis caches session tokens',
        ec=bound_embedder(tmp_backend), store_name='test')
    assert tmp_backend.nodes.get(tid).queue_uuid == 'q-restate-1'
    assert tmp_backend.nodes.has_active_with_queue_uuid(
        'q-restate-1') is True


def test_corroborate_preserves_creating_rows_queue_uuid(
        tmp_backend, monkeypatch):
    """A populated queue_uuid survives corroboration.

    The creating row's replay guard outranks the restating row's:
    clobbering it lets a crash-reclaimed creator re-process its row,
    and an LLM re-extraction is not guaranteed to re-hit the rung --
    it can insert the duplicate the guard exists to prevent.

    Mutation: flipping the coalesce back to adopt-over (the 0.19.0
        form, `coalesce(?, queue_uuid)`).
    Oracle: after a restatement the target still carries the
        creating row's uuid and the replay guard fires for it.
    """
    tid = str(uuid.uuid4())
    tmp_backend.nodes.insert(make_insight(
        id=tid, content='Redis caches session tokens',
        queue_uuid='q-create-1'))
    _spy_reconcile(monkeypatch)
    parent = _new_insight('Redis caches session tokens')
    parent.queue_uuid = 'q-restate-2'
    run_remember(
        tmp_backend, parent, 'Redis caches session tokens',
        ec=bound_embedder(tmp_backend), store_name='test')
    assert tmp_backend.nodes.get(tid).queue_uuid == 'q-create-1'
    assert tmp_backend.nodes.has_active_with_queue_uuid(
        'q-create-1') is True


def test_corroborate_dead_target_degrades_to_add(
        tmp_backend, monkeypatch):
    """A target soft-deleted after planning degrades to an add.

    `_drain_queue` builds `insights_by_id` once per store per drain;
    an external forget soft-deletes rows without evicting them, so an
    exact match against a stale entry must not drop the incoming
    fact.

    Mutation: returning the skip on a zero-row bump (the 0.19.0
        form) -- the restated fact is stored nowhere and a phantom
        oplog row names a dead id.
    Oracle: the fact lands as a live 'add' row, the dead target's
        counter stays 0, and no corroborate oplog row is written.
    """
    tid = _store(tmp_backend, 'Redis caches session tokens')
    stale_cache = {
        i.id: i for i in tmp_backend.nodes.get_all_active()}
    tmp_backend.nodes.soft_delete(tid)
    _spy_reconcile(monkeypatch)
    res = run_remember(
        tmp_backend, _new_insight('Redis caches session tokens'),
        'Redis caches session tokens',
        ec=bound_embedder(tmp_backend), store_name='test',
        insights_by_id=stale_cache)
    assert res['facts'][0]['action'] == 'add'
    assert tmp_backend.nodes.get(res['facts'][0]['id']) is not None
    # The degraded add supersedes nothing: it must name the vanished
    # row as target_id, never claim a replace of it.
    assert res['facts'][0].get('replaced_id') is None
    assert res['facts'][0]['target_id'] == tid
    dead = tmp_backend.nodes.get_include_deleted(tid)
    assert dead.corroboration_count == 0
    count = tmp_backend._db._query(
        'select count(*) from oplog'
        " where operation = 'reconcile-corroborate'").fetchone()[0]
    assert count == 0


def test_degrade_repairs_the_stale_drain_caches(
        tmp_backend, monkeypatch):
    """One dead target yields ONE live copy across later rows.

    The drain builds `insights_by_id`/`embed_cache` once per store;
    without apply-time repair every later row exact-matches the
    same stale dead entry and inserts another copy -- recreating
    exactly the two-identical-rows state the rung refuses to
    auto-resolve.

    Mutation: dropping the cache eviction/registration in
        `apply_all`'s degrade handling.
    Oracle: three restatements sharing one stale cache leave exactly
        one live row; calls 2 and 3 skip against the surviving copy,
        whose counter reads 2.
    """
    tid = _store(tmp_backend, 'Redis caches session tokens')
    stale_cache = {
        i.id: i for i in tmp_backend.nodes.get_all_active()}
    shared_embeds = dict(tmp_backend.nodes.iter_embeddings_as_vecs())
    tmp_backend.nodes.soft_delete(tid)
    _spy_reconcile(monkeypatch)
    actions = []
    for _ in range(3):
        res = run_remember(
            tmp_backend, _new_insight('Redis caches session tokens'),
            'Redis caches session tokens',
            ec=bound_embedder(tmp_backend), store_name='test',
            insights_by_id=stale_cache, embed_cache=shared_embeds)
        actions.append(res['facts'][0]['action'])
    live = [i for i in tmp_backend.nodes.get_all_active()
            if i.content == 'Redis caches session tokens']
    assert len(live) == 1
    assert actions == ['add', 'skipped', 'skipped']
    assert live[0].corroboration_count == 2


def test_same_row_duplicate_against_dead_target_adds_once(
        tmp_backend, monkeypatch):
    """A row whose extraction repeats the fact adds ONE copy.

    Mutation: guarding only the bump with `corroborated_ids`, not
        the degrade -- the second identical fact inserts a second
        copy in the same transaction.
    Oracle: extraction emitting the same fact twice against a dead
        target leaves exactly one live row with that content.
    """
    tid = _store(tmp_backend, 'Redis caches session tokens')
    stale_cache = {
        i.id: i for i in tmp_backend.nodes.get_all_active()}
    tmp_backend.nodes.soft_delete(tid)
    _spy_reconcile(monkeypatch)
    fact = {'text': 'Redis caches session tokens', 'category': 'fact',
            'importance': 3, 'entities': []}
    monkeypatch.setattr(
        'memman.llm.extract.extract_facts',
        lambda client, content: [dict(fact), dict(fact)])
    run_remember(
        tmp_backend, _new_insight('Redis caches session tokens'),
        'Redis caches session tokens',
        ec=bound_embedder(tmp_backend), store_name='test',
        insights_by_id=stale_cache)
    live = [i for i in tmp_backend.nodes.get_all_active()
            if i.content == 'Redis caches session tokens']
    assert len(live) == 1


def test_same_fact_twice_in_one_row_bumps_once(
        tmp_backend, monkeypatch):
    """One queue row restating a fact twice bumps its target once.

    The counter's semantics are per-restatement across writes, not
    per-extracted-fact: an over-eager extractor emitting a duplicate
    pair must not double-count.

    Mutation: dropping the per-invocation `corroborated_ids` set.
    Oracle: corroboration_count == 1 and exactly one oplog row after
        a single run whose extraction yields the same fact twice.
    """
    tid = _store(tmp_backend, 'Redis caches session tokens')
    _spy_reconcile(monkeypatch)
    fact = {'text': 'Redis caches session tokens', 'category': 'fact',
            'importance': 3, 'entities': []}
    monkeypatch.setattr(
        'memman.llm.extract.extract_facts',
        lambda client, content: [dict(fact), dict(fact)])
    _run(tmp_backend, 'Redis caches session tokens')
    assert tmp_backend.nodes.get(tid).corroboration_count == 1
    count = tmp_backend._db._query(
        'select count(*) from oplog'
        " where operation = 'reconcile-corroborate'").fetchone()[0]
    assert count == 1


def test_skip_result_carries_target_id(tmp_backend, monkeypatch):
    """The skip result names the row that absorbed the restatement.

    The result's 'id' is a never-inserted uuid; without 'target_id'
    the corroborated row is unreachable from the response.

    Mutation: dropping 'target_id' from the skipped result dict.
    Oracle: the result's target_id equals the stored row's id.
    """
    tid = _store(tmp_backend, 'Redis caches session tokens')
    _spy_reconcile(monkeypatch)
    res = _run(tmp_backend, 'Redis caches session tokens')
    assert res['facts'][0]['action'] == 'skipped'
    assert res['facts'][0]['target_id'] == tid


def test_corroboration_count_reaches_the_json_read_path(
        tmp_backend, monkeypatch):
    """The counter is visible through the full-dict serializer.

    `insight_to_full_dict` is the consumer-facing read path that
    carries this counter (`recall` and `get` both serialize through
    it; `recall --brief` projects it away deliberately); dropping the
    key makes F4 write-only while every write-side test stays green.

    Mutation: deleting the corroboration_count line from
        `insight_to_full_dict`.
    Oracle: after one restatement the serialized target carries
        corroboration_count == 1.
    """
    from memman.store.model import insight_to_full_dict
    tid = _store(tmp_backend, 'Redis caches session tokens')
    _spy_reconcile(monkeypatch)
    _run(tmp_backend, 'Redis caches session tokens')
    assert insight_to_full_dict(
        tmp_backend.nodes.get(tid))['corroboration_count'] == 1
