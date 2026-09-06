"""Tests for `pipeline.remember`'s planning and prompt pinning.

Covers the two invariants the drain's write path cannot express in
its own output: the reconcile candidate list must carry the
strongest near-duplicate rather than the first ones the cache
happened to yield, and `compute_prompt_version` must not move
except deliberately.
"""


def test_reconcile_candidates_ranked_by_similarity(monkeypatch):
    """The strongest near-duplicate must reach the reconcile candidate list.

    Regression: the cosine candidates were appended in embed_cache order
    and capped at MAX_SIMILAR_FOR_RECONCILE, so a high-cosine insight that
    sorts last could be crowded out by weaker earlier ones.
    """
    import math
    from unittest.mock import MagicMock

    from memman.llm import extract as llm_extract
    from memman.pipeline import remember as rem
    from tests.conftest import make_insight

    captured = {}

    def _fake_reconcile(client, fact, similar):
        captured['similar'] = list(similar)
        return {'action': 'ADD', 'targets': [], 'merged_text': None}

    monkeypatch.setattr(llm_extract, 'reconcile_memories', _fake_reconcile)

    fact_vec = [1.0, 0.0]
    med = [0.6, math.sqrt(1 - 0.6 * 0.6)]
    top = [0.95, math.sqrt(1 - 0.95 * 0.95)]

    insights_by_id = {}
    embed_cache = {}
    for i in range(10):
        ins = make_insight(id=f'dec{i}', content=f'decoy body number {i}')
        insights_by_id[ins.id] = ins
        embed_cache[ins.id] = list(med)
    topins = make_insight(id='TOP', content='topmost candidate body')
    insights_by_id[topins.id] = topins
    embed_cache[topins.id] = list(top)

    fact = {'text': 'zzqq alpha brandnew', 'category': 'fact',
            'importance': 3, 'entities': []}
    parent = make_insight(id='parent', content='zzqq alpha brandnew')
    ec = MagicMock()
    ec.embed.return_value = fact_vec

    rem._plan_fact(
        fact, parent, '', False, False,
        insights_by_id, embed_cache, set(),
        MagicMock(), MagicMock(), ec, MagicMock(), MagicMock())

    ids = [cid for cid, _content in captured.get('similar', [])]
    assert 'TOP' in ids, f'top-cosine insight crowded out; candidates={ids}'


def test_prompt_version_unchanged_by_length_caps():
    """The length caps live post-parse; the prompt hash is pinned.

    The pin is a tripwire, not a constant: any deliberate change to a
    hashed input moves it, and re-pinning is the right answer once the
    author has weighed the cost. That cost is what the tripwire
    surfaces -- every stored row in every store goes stale at once,
    and only a `graph rebuild --stale` clears it.

    Two inputs now move this value and neither is a length cap: the
    enrichment prompt and the causal prompt. So does the configured
    `MEMMAN_LLM_MODEL_SLOW_METADATA`, which the key folds in and which
    the suite seeds from `INSTALL_DEFAULTS` -- changing that default
    re-pins this test, deliberately.

    Mutation: "fixing" the length caps inside a system prompt, or any
        other incidental edit to a hashed input -- the hash moves and
        every stored row goes stale for a change nobody intended.
    Oracle: the hash of the two replayable prompts plus the seeded
        metadata model, pinned.
    """
    from memman.pipeline.remember import compute_prompt_version
    assert compute_prompt_version() == '6a60ef0080b1ab9f'


class _FixedEmbedder:
    """An embed provider returning one fixed vector for every text."""

    model = 'fixed'

    def __init__(self, vec):
        self.vec = vec

    def available(self):
        return False

    def embed(self, text):
        return list(self.vec)


def _plant_shortlist(backend):
    """Two stored rows: one the keyword rung finds, one only cosine finds.

    Returns the drain-scope caches `run_remember` takes, so the cosine
    rung reads vectors this test controls rather than the mock embedder's.
    """
    from tests.conftest import make_insight
    kw = make_insight(id='kw-1', content='zulu yankee xray whiskey victor uniform')
    cos = make_insight(id='cos-1', content='gardening tulips bloom in spring soil')
    backend.nodes.insert(kw)
    backend.nodes.insert(cos)
    insights_by_id = {kw.id: kw, cos.id: cos}
    embed_cache = {'kw-1': [0.0, 1.0], 'cos-1': [1.0, 0.0]}
    return insights_by_id, embed_cache


def _candidates_rows(backend):
    import json
    return [(e.insight_id, json.loads(e.detail))
            for e in backend.oplog.recent(limit=20)
            if e.operation == 'reconcile-candidates']


def test_reconcile_candidates_are_logged(tmp_backend, monkeypatch):
    """Verify a write logs the reconciler's shortlist with each row's rung.

    Mutation: dropping the `reconcile-candidates` row, or logging ids
        without their rung, so the F2 replay cannot tell a keyword hit
        from a cosine one.
    Oracle: one row keyed on the inserted insight whose detail lists the
        planted keyword row as `keyword` and the planted cosine row as
        `cosine`, against the rows this test planted.
    """
    from memman.pipeline.remember import run_remember
    from tests.conftest import make_insight

    insights_by_id, embed_cache = _plant_shortlist(tmp_backend)
    monkeypatch.setattr(
        'memman.llm.extract.extract_facts',
        lambda client, content: [
            {'text': content, 'category': 'fact', 'entities': []}])
    monkeypatch.setattr(
        'memman.llm.extract.reconcile_memories',
        lambda client, fact, similar: {
            'action': 'ADD', 'targets': [], 'merged_text': None})

    fact_text = 'zulu yankee xray whiskey victor'
    res = run_remember(
        tmp_backend, make_insight(id='parent', content=fact_text),
        fact_text, ec=_FixedEmbedder([1.0, 0.0]),
        embed_cache=embed_cache, insights_by_id=insights_by_id, store_name='test')

    new_id = res['facts'][0]['id']
    rows = _candidates_rows(tmp_backend)
    assert [key for key, _ in rows] == [new_id]
    detail = rows[0][1]
    assert detail['fact_id'] == new_id
    assert {(c['id'], c['rung']) for c in detail['candidates']} == {
        ('kw-1', 'keyword'), ('cos-1', 'cosine')}


def test_reconcile_candidates_are_logged_for_a_none_skip(tmp_backend, monkeypatch):
    """Verify a NONE skip logs the shortlist keyed on the row it corroborated.

    Mutation: logging on the write path only, so every NONE and
        exact-match case is missing from the replay's 2x2.
    Oracle: the row read back by the NONE target's id, listing both
        planted candidates.
    """
    from memman.pipeline.remember import run_remember
    from tests.conftest import make_insight

    insights_by_id, embed_cache = _plant_shortlist(tmp_backend)
    monkeypatch.setattr(
        'memman.llm.extract.extract_facts',
        lambda client, content: [
            {'text': content, 'category': 'fact', 'entities': []}])
    monkeypatch.setattr(
        'memman.llm.extract.reconcile_memories',
        lambda client, fact, similar: {
            'action': 'NONE', 'targets': [('cos-1', 'none')], 'merged_text': None})

    fact_text = 'zulu yankee xray whiskey victor'
    res = run_remember(
        tmp_backend, make_insight(id='parent', content=fact_text),
        fact_text, ec=_FixedEmbedder([1.0, 0.0]),
        embed_cache=embed_cache, insights_by_id=insights_by_id, store_name='test')

    assert res['facts'][0]['action'] == 'skipped'
    rows = _candidates_rows(tmp_backend)
    assert [key for key, _ in rows] == ['cos-1']
    assert {c['id'] for c in rows[0][1]['candidates']} == {'kw-1', 'cos-1'}
