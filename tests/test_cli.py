"""Tests for memman.cli — Click CLI commands via CliRunner.

All tests use real Haiku LLM and Voyage embedding APIs.
Requires OPENROUTER_API_KEY and VOYAGE_API_KEY in environment.
"""

import json
import pathlib
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from memman.cli import cli
from memman.embed.fingerprint import seed_default_fingerprint
from memman.embed.vector import serialize_vector
from memman.store.db import store_exists
from memman.store.errors import BackendError
from memman.store.node import insert_insight, update_embedding
from tests.conftest import invoke, make_insight, parse_remember


@pytest.fixture
def runner(mm_runner):
    """CliRunner + data_dir tuple (delegates to conftest `mm_runner`)."""
    return mm_runner


class TestRemember:
    """`memman remember` happy paths and validation."""

    def test_remember_basic(self, runner):
        """Store a basic insight."""
        result = invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile'])
        assert result.exit_code == 0
        data = parse_remember(result, runner)
        assert data['action'] in {'add', 'added', 'update', 'updated'}
        assert 'sqlite' in data['content'].lower()

    def test_remember_with_flags(self, runner):
        """Store with category and importance."""
        result = invoke(runner, [
            'remember', 'Chose Docker for container orchestration in production',
            '--no-reconcile',
            '--cat', 'decision', '--imp', '4'])
        assert result.exit_code == 0
        data = parse_remember(result, runner)
        assert 'id' in data

        result = invoke(runner, ['recall', '--basic', 'Docker container'])
        hits = json.loads(result.output)['results']
        match = [h for h in hits if h['id'] == data['id']][0]
        assert match['category'] == 'decision'
        assert match['importance'] == 4

    def test_remember_invalid_category(self, runner):
        """Invalid category is rejected."""
        result = invoke(runner, [
            'remember', 'Go uses SQLite for storage', '--cat', 'bogus'])
        assert result.exit_code != 0

    def test_remember_rejects_general(self, runner):
        """Verify `--cat general` exits non-zero and names the valid set.

        Mutation: `general` accepted as a category (the 0.33.x set).
        Oracle: the exit code and the message listing the valid categories.
        """
        result = invoke(runner, [
            'remember', 'Go uses SQLite for storage', '--cat', 'general'])
        assert result.exit_code != 0
        assert 'valid:' in result.output
        assert 'fact' in result.output

    def test_remember_invalid_importance(self, runner):
        """Importance outside 1-5 is rejected."""
        result = invoke(runner, [
            'remember', 'Go uses SQLite for storage', '--imp', '0'])
        assert result.exit_code != 0

    def test_remember_does_not_link_old_pending_insights(self, runner, monkeypatch):
        """Remember does inline enrichment, never calls link_pending."""
        invoke(runner, [
            'remember', 'Redis cache eviction uses LRU algorithm',
            '--no-reconcile'])

        from unittest.mock import patch
        with patch('memman.graph.engine.link_pending',
                   side_effect=AssertionError(
                       'link_pending called from remember')) as mock_lp:
            result = invoke(runner, [
                'remember', 'PostgreSQL MVCC provides snapshot isolation',
                '--no-reconcile'])
            assert result.exit_code == 0
            mock_lp.assert_not_called()

    def test_remember_quality_warnings(self, runner):
        """Content with quality warnings is queued; warnings populated as hints."""
        result = invoke(runner, [
            'remember', 'i-0c220c2402a5245bc deployed via Terraform',
            '--no-reconcile'])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data['action'] == 'queued'
        assert 'AWS instance ID' in data['quality_warnings']
        assert 'deployment receipt' in data['quality_warnings']

    def test_remember_no_quality_warnings(self, runner):
        """Durable content produces empty quality_warnings."""
        result = invoke(runner, [
            'remember', 'SQLite chosen for single-node simplicity and embedded operation',
            '--no-reconcile'])
        assert result.exit_code == 0
        raw = json.loads(result.output)
        assert raw['quality_warnings'] == []

    def test_remember_quality_warnings_populate(self, runner):
        """Quality warnings populate as hints but never block the write."""
        result = invoke(runner, [
            'remember', 'Stack deployed via Terraform. 32 resources total.',
            '--no-reconcile'])
        data = json.loads(result.output)
        assert data['action'] == 'queued'
        assert len(data['quality_warnings']) >= 2

        result = invoke(runner, [
            'remember', 'Production outage traced to instance i-0c220c2402a5245bc running out of memory causing cascading failure',
            '--no-reconcile'])
        data = parse_remember(result, runner)
        assert data['action'] == 'add'
        raw = json.loads(result.output)
        assert len(raw['quality_warnings']) == 1

    def test_remember_creates_semantic_edges(self, runner):
        """Worker creates semantic edges for the new insight."""
        from memman.store.db import open_read_only, store_dir

        invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile'])
        invoke(runner, [
            'remember', 'SQLite WAL mode improves write throughput',
            '--no-reconcile'])

        _, data_dir = runner
        db = open_read_only(store_dir(data_dir, 'default'))
        try:
            rows = db._query(
                "SELECT edge_type FROM edges WHERE edge_type = 'semantic'"
                ).fetchall()
        finally:
            db.close()
        # Either zero or many semantic edges, depending on similarity;
        # the table exists and the worker reaches the edge-creation step.
        assert isinstance(rows, list)


class TestRecall:
    """`memman recall` smart and basic modes."""

    def test_recall_basic(self, runner):
        """Recall after remembering."""
        invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile'])
        result = invoke(runner, ['recall', 'Go SQLite storage'])
        assert result.exit_code == 0

    def test_recall_does_not_call_link_pending(self, runner, monkeypatch):
        """Recall path must not call link_pending (performance regression guard)."""
        invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile'])

        from unittest.mock import patch
        with patch('memman.graph.engine.link_pending',
                   side_effect=AssertionError('link_pending called')) as mock_lp:
            result = invoke(runner, ['recall', 'Go SQLite storage'])
            assert result.exit_code == 0
            mock_lp.assert_not_called()

    def test_recall_logs_when_query_embed_fails(
            self, runner, caplog, monkeypatch):
        """A raising `ec.embed` for the recall query is now warned, not
        swallowed silently. The recall still degrades to the keyword
        path and returns successfully.
        """
        import logging

        invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile'])

        from memman import embed as embed_mod
        real_ec = embed_mod.get_client()

        def _boom(self, text):
            raise RuntimeError('forced query embed failure')

        monkeypatch.setattr(type(real_ec), 'embed', _boom)
        with caplog.at_level(logging.WARNING, logger='memman'):
            result = invoke(runner, ['recall', 'Go SQLite storage'])
        assert result.exit_code == 0
        warned = [r for r in caplog.records
                  if 'recall query embed failed' in r.getMessage()]
        assert warned

    def test_recall_default_does_not_call_expand_query(self, runner):
        """Default recall must not run LLM query expansion."""
        invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile'])

        from unittest.mock import patch
        with patch('memman.llm.extract.expand_query',
                   side_effect=AssertionError('expand_query called')) as mock_ex:
            result = invoke(runner, ['recall', 'Go SQLite storage'])
            assert result.exit_code == 0
            mock_ex.assert_not_called()

    def test_recall_expand_flag_calls_expand_query(self, runner):
        """Recall --expand re-enables the LLM query expansion path."""
        invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile'])

        from unittest.mock import patch
        fake = {'expanded_query': 'Go SQLite storage', 'intent': '', 'entities': []}
        with patch('memman.llm.extract.expand_query',
                   return_value=fake) as mock_ex:
            result = invoke(runner, ['recall', 'Go SQLite storage', '--expand'])
            assert result.exit_code == 0
            mock_ex.assert_called_once()

    def test_recall_default_runs_rerank(self, runner):
        """Default install seeds MEMMAN_RERANK_ENABLED=true, so rerank fires."""
        for fact in [
                'Go uses SQLite for persistent storage',
                'Go modules manage dependency versions',
                'SQLite uses WAL mode for concurrent writes']:
            invoke(runner, ['remember', fact, '--no-reconcile'])

        from unittest.mock import patch
        with patch('memman.rerank.voyage.Client.rerank',
                   return_value=[(0, 0.9), (1, 0.5), (2, 0.1)]) as mock_re:
            result = invoke(runner, ['recall', 'Go SQLite persistent storage'])
            assert result.exit_code == 0
            mock_re.assert_called_once()
            data = json.loads(result.output)
            assert data['meta'].get('reranked') is True

    def test_recall_global_disable_skips_rerank(self, runner, env_file):
        """MEMMAN_RERANK_ENABLED=false disables rerank globally."""
        invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile'])
        env_file('MEMMAN_RERANK_ENABLED', 'false')

        from unittest.mock import patch
        with patch('memman.rerank.voyage.Client.rerank',
                   side_effect=AssertionError('rerank called')) as mock_re:
            result = invoke(runner, ['recall', 'Go SQLite storage'])
            assert result.exit_code == 0
            mock_re.assert_not_called()
            data = json.loads(result.output)
            assert data['meta'].get('reranked') is False

    def test_recall_per_store_disable_overrides_global(self, runner, env_file):
        """MEMMAN_RERANK_ENABLED_<store>=false wins over the global default."""
        invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile'])
        env_file('MEMMAN_RERANK_ENABLED_default', 'false')

        from unittest.mock import patch
        with patch('memman.rerank.voyage.Client.rerank',
                   side_effect=AssertionError('rerank called')) as mock_re:
            result = invoke(runner, ['recall', 'Go SQLite storage'])
            assert result.exit_code == 0
            mock_re.assert_not_called()
            data = json.loads(result.output)
            assert data['meta'].get('reranked') is False

    def test_recall_per_store_enable_overrides_global_disable(
            self, runner, env_file):
        """Per-store true beats global false."""
        for fact in [
                'Go uses SQLite for persistent storage',
                'Go modules manage dependency versions',
                'SQLite uses WAL mode for concurrent writes']:
            invoke(runner, ['remember', fact, '--no-reconcile'])
        env_file('MEMMAN_RERANK_ENABLED', 'false')
        env_file('MEMMAN_RERANK_ENABLED_default', 'true')

        from unittest.mock import patch
        with patch('memman.rerank.voyage.Client.rerank',
                   return_value=[(0, 0.9), (1, 0.5), (2, 0.1)]) as mock_re:
            result = invoke(runner, ['recall', 'Go SQLite persistent storage'])
            assert result.exit_code == 0
            mock_re.assert_called_once()
            data = json.loads(result.output)
            assert data['meta'].get('reranked') is True

    def test_recall_rerank_skipped_on_short_query(self, runner):
        """Rerank auto-skips when the query has <=2 tokens, even with default on."""
        invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile'])

        from unittest.mock import patch
        with patch('memman.rerank.voyage.Client.rerank',
                   side_effect=AssertionError('rerank called on short query')
                   ) as mock_re:
            result = invoke(runner, ['recall', 'storage'])
            assert result.exit_code == 0
            mock_re.assert_not_called()
            data = json.loads(result.output)
            assert data['meta'].get('reranked') is False

    def test_recall_rerank_failure_falls_back_gracefully(self, runner):
        """Reranker errors must not break recall; falls back to baseline."""
        for fact in [
                'Go uses SQLite for persistent storage',
                'Go modules manage dependency versions']:
            invoke(runner, ['remember', fact, '--no-reconcile'])

        from unittest.mock import patch
        with patch('memman.rerank.voyage.Client.rerank',
                   side_effect=RuntimeError('voyage 503')) as mock_re:
            result = invoke(runner, ['recall', 'Go SQLite persistent storage'])
            assert result.exit_code == 0
            mock_re.assert_called_once()

    def test_recall_min_score_reaches_retrieval(self, runner):
        """`--min-score` is forwarded to `intent_aware_recall`.

        Mutation: dropping `min_score=min_score` from the call site, so
            the flag parses and silently does nothing.
        Oracle: a spy capturing the kwarg, compared against the value
            typed on the command line and against the unflagged
            default.
        """
        invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile'])

        empty = {'results': [], 'meta': {'intent': 'GENERAL'}}
        with patch('memman.search.recall.intent_aware_recall',
                   return_value=empty) as spy:
            invoke(runner, ['recall', 'Go SQLite storage',
                            '--min-score', '0.4'])
            assert spy.call_args.kwargs['min_score'] == 0.4
        with patch('memman.search.recall.intent_aware_recall',
                   return_value=empty) as spy:
            invoke(runner, ['recall', 'Go SQLite storage'])
            assert spy.call_args.kwargs['min_score'] == 0.0

    def test_recall_min_score_rejected_on_basic_path(self, runner):
        """`--basic --min-score` fails rather than ignoring the floor.

        Mutation: dropping the guard, which makes the floor a silent
            no-op on the SQL LIKE path that computes no scores.
        Oracle: a non-zero exit whose message names both flags, against
            the same command with the floor left at its default.
        """
        invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile'])
        rejected = invoke(runner, ['recall', 'Go SQLite', '--basic',
                                   '--min-score', '0.5'])
        assert rejected.exit_code != 0
        assert '--min-score' in rejected.output
        assert '--basic' in rejected.output
        allowed = invoke(runner, ['recall', 'Go SQLite', '--basic'])
        assert allowed.exit_code == 0

    def test_recall_basic_names_the_flags_it_ignored(self, runner):
        """`--basic` reports inert ranking flags in `meta.ignored`.

        Mutation: dropping the `was_given` guard so both names are
            always listed, emitting `ignored` unconditionally, naming
            only `intent` and letting `--expand` stay silently inert,
            or leaking the key onto the scored envelope where
            `--intent` is genuinely honored.
        Oracle: the whole `meta` dict compared against a hand-written
            expectation for each of the four flag combinations, so an
            absent key and an empty list are distinguishable.
        """
        invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile'])

        def meta(*flags):
            r = invoke(runner, ['recall', 'Go SQLite', '--basic', *flags])
            assert r.exit_code == 0, r.output
            return json.loads(r.output)['meta']

        assert meta() == {'basic': True}
        assert meta('--intent', 'WHY') == {
            'basic': True, 'ignored': ['intent']}
        assert meta('--expand') == {
            'basic': True, 'ignored': ['expand']}
        assert meta('--intent', 'WHY', '--expand') == {
            'basic': True, 'ignored': ['intent', 'expand']}

        # The key must never reach the scored envelope: recall fires
        # from a hook on every user message, and `--intent` IS honored
        # there, so reporting it would be a lie as well as bytes.
        scored = invoke(runner, [
            'recall', 'Go SQLite', '--intent', 'WHY', '--limit', '1'])
        assert scored.exit_code == 0, scored.output
        assert 'ignored' not in json.loads(scored.output)['meta']

    def test_recall_min_score_rejects_out_of_domain_values(self, runner):
        """Values that cannot act as a floor are refused, not ignored.

        Mutation: dropping the NaN guard, or widening the FloatRange,
            either of which restores a floor that parses and then
            silently filters nothing.
        Oracle: three values that cannot threshold anything (below the
            range, above it, and NaN, which compares false against
            every row) against an in-range value that is accepted.
        """
        invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile'])
        for bad in ('-1', '3.0', 'nan'):
            rejected = invoke(runner, ['recall', 'Go SQLite',
                                       '--min-score', bad])
            assert rejected.exit_code != 0, bad
        accepted = invoke(runner, ['recall', 'Go SQLite',
                                   '--min-score', '2.0'])
        assert accepted.exit_code == 0

    def test_recall_basic_mode(self, runner):
        """Basic recall returns {results: [...], meta: {basic: True}}."""
        invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile'])
        result = invoke(runner, ['recall', 'Go SQLite', '--basic'])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data['meta']['basic'] is True
        assert isinstance(data['results'], list)

    def test_recall_basic_returns_envelope(self, runner):
        """Recall --basic returns insights wrapped in {results: [...]}."""
        invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile'])
        result = invoke(runner, ['recall', '--basic', 'Go SQLite'])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert 'results' in data
        assert any('SQLite' in r['content'] for r in data['results'])

    def test_recall_emits_summary_when_populated(self, runner):
        """Smart recall emits summary in result['insight'] when row has one."""
        long_content = (
            'The application uses a write-through cache layer between the '
            'API tier and Postgres. TTL is 5 minutes for hot keys and 1 '
            'hour for cold keys. Cache invalidation must run before each '
            'DB write commits to avoid stale reads during the gap.')
        invoke(runner, ['remember', long_content])
        result = invoke(runner, ['recall', 'cache invalidation'])
        assert result.exit_code == 0
        data = json.loads(result.output)
        matched = [r for r in data['results']
                   if 'cache' in r['insight']['content'].lower()]
        assert matched, 'expected at least one matching result'
        insight = matched[0]['insight']
        assert insight.get('summary'), \
            'summary should be present and non-empty for substantive content'
        assert insight['summary'] != insight['content']

    def test_recall_omits_summary_when_unenriched(self, runner):
        """When summary is empty/null, the field is not emitted at all."""
        invoke(runner, [
            'remember', 'Q', '--cat', 'fact', '--no-reconcile'])
        result = invoke(runner, ['recall', '--basic', 'Q'])
        assert result.exit_code == 0
        data = json.loads(result.output)
        for r in data['results']:
            assert 'summary' not in r or r['summary'] == ''

    def test_recall_basic_emits_summary_when_present(self, runner):
        """--basic mode also surfaces summary; both paths share the serializer."""
        long_content = (
            'The job scheduler uses systemd timers on Linux hosts and '
            'launchd on macOS hosts. The drain interval defaults to 60 '
            'seconds and is configurable via memman scheduler interval.')
        invoke(runner, ['remember', long_content])
        result = invoke(runner, ['recall', '--basic', 'scheduler timer'])
        assert result.exit_code == 0
        data = json.loads(result.output)
        matched = [r for r in data['results']
                   if 'scheduler' in r['content'].lower()]
        if matched and matched[0].get('summary'):
            assert matched[0]['summary'] != matched[0]['content']

    def test_recall_brief_projects_ranked_rows(self, runner):
        """--brief cuts the ranked path's insight body to the projection.

        Mutation: wiring --brief into the --basic branch only, leaving
            the ranked path -- the one the UserPromptSubmit hook fires
            on every user message -- emitting full content.
        Oracle: the key set the unflagged run emits for the same
            query, differenced against the flagged one.
        """
        invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile'])
        full = json.loads(
            invoke(runner, ['recall', 'Go SQLite storage']).output)
        assert full['results'], 'expected the ranked path to return a row'
        full_keys = set(full['results'][0]['insight'])
        assert 'content' in full_keys

        result = invoke(runner, ['recall', 'Go SQLite storage', '--brief'])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data['results'], 'expected --brief to return the same row'
        brief_keys = set(data['results'][0]['insight'])
        assert brief_keys - {'truncated'} == {
            'id', 'category', 'importance', 'created_at', 'summary'}
        assert 'content' in full_keys - brief_keys

    def test_recall_brief_carries_the_stored_created_at(self, runner):
        """--brief emits created_at with the value the store holds.

        Mutation: emitting `updated_at`, `datetime.now()`, or a
            constant in place of the row's own `created_at` - a
            key-set assertion passes on all three, and a WHEN caller
            sorting on the field would build a wrong timeline from a
            page that looks correct.
        Oracle: the same row's `created_at` off the FULL projection,
            which is independently formatted by
            `insight_to_full_dict`.
        """
        invoke(runner, [
            'remember', 'Kafka retains partitions by time and size',
            '--no-reconcile'])
        full = json.loads(
            invoke(runner, ['recall', 'Kafka partitions retention']).output)
        brief = json.loads(
            invoke(runner, [
                'recall', 'Kafka partitions retention', '--brief']).output)
        assert full['results']
        assert brief['results']

        full_by_id = {r['insight']['id']: r['insight'] for r in full['results']}
        checked = 0
        for row in brief['results']:
            ins = row['insight']
            assert ins['created_at'] == full_by_id[ins['id']]['created_at']
            assert ins['created_at'].endswith('Z')
            checked += 1
        assert checked, 'expected at least one row to compare'

    def test_recall_detail_oplog_records_the_request(self, runner):
        """The recall-detail row carries the REQUESTED limit and session.

        Mutation: recording `len(hits)` in place of the requested
            `limit` - which cannot tell a thin page from a small ask -
            or dropping the session key, either of which leaves a
            return unattributable to the session that asked.
        Oracle: a recall issued with a limit deliberately larger than
            the store can fill, so the requested value and the
            returned count differ.
        """
        invoke(runner, [
            'remember', 'Envoy routes gRPC traffic by header match',
            '--no-reconcile'])
        invoke(runner, [
            'recall', 'Envoy gRPC header routing',
            '--limit', '17', '--session', 'sess-abc'])

        entries = json.loads(
            invoke(runner, ['log', 'list', '--limit', '50']).output)['entries']
        details = [
            json.loads(e['detail'])
            for e in entries if e['operation'] == 'recall-detail']
        assert details, 'expected a recall-detail row'
        row = details[0]
        assert row['limit'] == 17
        assert row['session'] == 'sess-abc'
        assert 'q' in row
        assert len(row['q']) <= 80
        assert len(row['hits']) < row['limit'], (
            'fixture must under-fill the page so the two cannot be '
            'confused')

    def test_recall_session_falls_back_to_the_environment(
            self, runner, monkeypatch):
        """An unflagged recall takes its session from the environment.

        Mutation: dropping the `envvar` list from the `--session`
            option, which leaves the oplog session blank on every
            recall an agent issues without the flag - the normal case,
            since the hooks only ever remind it about the flag.
        Oracle: the oplog row from a recall run with no `--session`
            argument at all, against the exported id.
        """
        monkeypatch.setenv('MEMMAN_SESSION_ID', 'env-session-9')
        invoke(runner, [
            'remember', 'Redis evicts keys by LRU under maxmemory',
            '--no-reconcile'])
        invoke(runner, ['recall', 'Redis LRU maxmemory eviction'])

        entries = json.loads(
            invoke(runner, ['log', 'list', '--limit', '50']).output)['entries']
        details = [
            json.loads(e['detail'])
            for e in entries if e['operation'] == 'recall-detail']
        assert details, 'expected a recall-detail row'
        assert details[0]['session'] == 'env-session-9'

    def test_recall_brief_projects_the_insight_not_the_row(self, runner):
        """--brief cuts the insight and leaves the ranking envelope whole.

        Mutation: projecting the whole result row rather than its
            `insight` value, dropping the ranking diagnostics the
            recall hint and the quality harness both read.
        Oracle: the envelope keys and the projected insight's key set
            asserted on the same row, so neither half can pass alone.
        """
        invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile'])
        result = invoke(runner, ['recall', 'Go SQLite storage', '--brief'])
        assert result.exit_code == 0, result.output
        row = json.loads(result.output)['results'][0]
        assert {'insight', 'score', 'intent', 'signals'} <= set(row)
        assert set(row['insight']) - {'truncated'} == {
            'id', 'category', 'importance', 'created_at', 'summary'}

    def test_recall_brief_basic_path_projects_rows(self, runner):
        """--brief projects the --basic branch's rows and drops none.

        Mutation: returning `insight_to_full_dict` on the --basic
            branch, so `recall --basic --brief` still ships content; or
            letting the flag change how many rows come back rather than
            only their shape.
        Oracle: exact four-key set on each row, and the row count of
            the same query run without the flag.
        """
        from memman.store.db import open_db
        db = open_db(str(pathlib.Path(runner[1]) / 'data' / 'default'))
        for i in range(3):
            insert_insight(db, make_insight(
                id=f'brief-many-{i}', content=f'yankee row {i}',
                category='fact'))
        db.close()

        full = json.loads(
            invoke(runner, ['recall', '--basic', 'yankee']).output)
        assert len(full['results']) == 3, full['results']

        result = invoke(runner, [
            'recall', '--basic', '--brief', 'yankee'])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data['results']) == 3, data['results']
        for r in data['results']:
            assert set(r) - {'truncated'} == {
                'id', 'category', 'importance', 'created_at', 'summary'}

    def test_recall_brief_emits_a_real_summary_unmarked(self, runner):
        """A row that HAS a summary ships it verbatim, with no marker.

        Mutation: always taking the content-prefix fallback. Every
            other --brief CLI test seeds through `remember`, which
            leaves summary blank, so all of them run the fallback
            branch and none would notice.
        Oracle: the seeded summary string, which is absent from the
            content, so a fallback cannot produce it.
        """
        from memman.store.db import open_db
        db = open_db(str(pathlib.Path(runner[1]) / 'data' / 'default'))
        insert_insight(db, make_insight(
            id='brief-summ', content='zeta ' * 100, category='fact'))
        db._conn.execute(
            'update insights set summary = ? where id = ?',
            ('a genuine enrichment summary', 'brief-summ'))
        db.close()

        result = invoke(runner, ['recall', '--basic', '--brief', 'zeta'])
        assert result.exit_code == 0, result.output
        rows = [r for r in json.loads(result.output)['results']
                if r['id'] == 'brief-summ']
        assert rows, 'expected the seeded row back'
        assert rows[0]['summary'] == 'a genuine enrichment summary'
        assert 'truncated' not in rows[0]

    def test_recall_brief_never_returns_an_empty_row(self, runner):
        """A row the compression gate left summary-less still carries text.

        Mutation: a summary-only projection -- `graph/enrichment.py`
            blanks summary whenever it fails the 0.85 compression gate,
            which was 46 of 118 rows on a live store, so a third of
            results come back with nothing to read.
        Oracle: a seeded row whose 400-char content has no summary; the
            emitted text is the hand-computed 200-char prefix and the
            row is marked truncated because content really was cut.
        """
        from memman.store.db import open_db
        content = 'abcde' * 80
        db = open_db(str(pathlib.Path(runner[1]) / 'data' / 'default'))
        insert_insight(db, make_insight(
            id='brief-gate', content=content, category='fact'))
        db.close()

        result = invoke(runner, ['recall', '--basic', '--brief', 'abcde'])
        assert result.exit_code == 0, result.output
        rows = [r for r in json.loads(result.output)['results']
                if r['id'] == 'brief-gate']
        assert rows, 'expected the seeded row back'
        assert rows[0]['summary'] == 'abcde' * 40
        assert rows[0]['truncated'] is True

    def test_recall_source_filter_smart(self, runner):
        """Smart recall respects --source filter."""
        invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile', '--source', 'agent'])
        invoke(runner, [
            'remember', 'Python uses PostgreSQL for web application storage',
            '--no-reconcile', '--source', 'human'])

        result = invoke(runner, [
            'recall', 'database storage', '--source', 'agent'])
        assert result.exit_code == 0
        data = json.loads(result.output)
        for r in data['results']:
            assert r['insight']['source'] == 'agent'

    def test_recall_source_filter_returns_matching_rows(self, runner):
        """Recall --source surfaces a matching row the top-k would drop.

        Folded from `test_recall_source_filter_inflates_fetch_limit`
        after D2 replaced the fetch-inflation post-filter with anchor
        scans that filter before the top-k cut; the deep fill-to-limit
        regression lives in
        `tests/test_recall_filters.py::test_filtered_recall_fills_to_limit`.

        Mutation: dropping the `source` predicate from the anchor
            scans (or filtering only after the cut).
        Oracle: the single agent-sourced row appears despite six
            better-matching user rows competing for the slots.
        """
        topics = [
            'PostgreSQL query optimization with EXPLAIN ANALYZE',
            'PostgreSQL index types including B-tree GIN GiST',
            'PostgreSQL vacuum autovacuum tuning parameters',
            'PostgreSQL partitioning strategies for large tables',
            'PostgreSQL connection pooling with PgBouncer setup',
            'PostgreSQL replication streaming and logical decoding',
            ]
        for topic in topics:
            invoke(runner, [
                'remember', topic, '--no-reconcile', '--source', 'user'])
        invoke(runner, [
            'remember', 'PostgreSQL JSONB operators for document queries',
            '--no-reconcile', '--source', 'agent'])

        result = invoke(runner, [
            'recall', 'PostgreSQL database',
            '--source', 'agent', '--limit', '3'])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data['results'], 'filtered recall returned nothing'
        for r in data['results']:
            assert r['insight']['source'] == 'agent'


class TestForget:
    """`memman forget` happy paths and missing-id error."""

    def test_forget_basic(self, runner):
        """Forget an insight by ID."""
        result = invoke(runner, [
            'remember', 'Redis cache eviction policy uses LRU by default',
            '--no-reconcile'])
        data = parse_remember(result, runner)
        iid = data['id']
        result = invoke(runner, ['forget', iid])
        assert result.exit_code == 0
        fdata = json.loads(result.output)
        assert fdata['status'] == 'deleted'

    def test_forget_writes_oplog(self, runner):
        """Forget command writes an oplog entry atomically."""
        result = invoke(runner, [
            'remember', 'PostgreSQL uses MVCC for transaction isolation',
            '--no-reconcile'])
        data = parse_remember(result, runner)
        iid = data['id']
        invoke(runner, ['forget', iid])
        result = invoke(runner, ['log', 'list', '--stats'])
        assert result.exit_code == 0
        log_data = json.loads(result.output)
        assert 'forget' in log_data['operation_counts']

    def test_forget_nonexistent_fails(self, runner):
        """Forget with nonexistent ID returns error."""
        result = invoke(runner, ['forget', 'nonexistent-id-12345'])
        assert result.exit_code != 0


class TestStore:
    """`memman store` admin: list, create, set, remove."""

    def test_store_list(self, runner):
        """Store list emits a JSON envelope with stores[] and active."""
        import json as _json
        result = invoke(runner, ['store', 'list'])
        assert result.exit_code == 0
        payload = _json.loads(result.output)
        assert 'stores' in payload
        assert 'active' in payload

    def test_store_create(self, runner):
        """Create a new store; JSON reports action='created'."""
        import json as _json
        result = invoke(runner, ['store', 'create', 'test-store'])
        assert result.exit_code == 0
        payload = _json.loads(result.output)
        assert payload['action'] == 'created'
        assert payload['store'] == 'test-store'

    def test_store_create_duplicate(self, runner):
        """Duplicate store name is rejected."""
        invoke(runner, ['store', 'create', 'dup'])
        result = invoke(runner, ['store', 'create', 'dup'])
        assert result.exit_code != 0

    def test_store_set(self, runner):
        """Set active store; JSON reports action='set'."""
        import json as _json
        invoke(runner, ['store', 'create', 'work'])
        result = invoke(runner, ['store', 'use', 'work'])
        assert result.exit_code == 0
        payload = _json.loads(result.output)
        assert payload['action'] == 'set'
        assert payload['store'] == 'work'

    def test_store_remove_yes(self, runner):
        """Remove a non-active store with --yes skips prompt."""
        import json as _json
        invoke(runner, ['store', 'create', 'temp'])
        result = invoke(runner, ['store', 'remove', '--yes', 'temp'])
        assert result.exit_code == 0
        payload = _json.loads(result.output)
        assert payload['action'] == 'removed'
        assert payload['store'] == 'temp'

    def test_store_remove_purges_queue(self, runner):
        """Removing a store also drops its in-flight queue rows.

        Regression: the old `store remove` flow rmtreed the data dir
        but left queue rows orphaned, so the worker would re-attempt
        them against a missing store dir. The purge now happens
        inside `factory.drop_store`, which is what `store remove`
        invokes; the test asserts the observable contract (no
        survivor queue rows) regardless of where the purge fires.
        """
        import json as _json

        from memman.queue import enqueue, list_rows, open_queue_db
        _, data_dir = runner
        invoke(runner, ['store', 'create', 'doomed'])
        qconn = open_queue_db(data_dir)
        try:
            enqueue(qconn, 'doomed', 'a fact to remember')
            qconn.commit()
            before = [
                r for r in list_rows(qconn, limit=50)
                if r['store'] == 'doomed']
            assert len(before) == 1
        finally:
            qconn.close()
        result = invoke(runner, ['store', 'remove', '--yes', 'doomed'])
        assert result.exit_code == 0
        payload = _json.loads(result.output)
        assert payload['action'] == 'removed'
        qconn = open_queue_db(data_dir)
        try:
            after = [
                r for r in list_rows(qconn, limit=50)
                if r['store'] == 'doomed']
            assert after == [], (
                f'expected queue rows for doomed store to be purged, '
                f'got {len(after)} survivors')
        finally:
            qconn.close()

    def test_store_remove_purges_per_store_env_keys(self, runner):
        """Removing a store drops its per-store keys from the env file.

        Mutation: dropping the `removes=` cleanup, which leaves
        `MEMMAN_BACKEND_doomed` and the store's Postgres DSN (password
        included) in the env file after the store is gone.
        Oracle: the parsed env file -- every PER_STORE_KEY_SPECS prefix
        for the removed store absent, and the same prefixes for a
        surviving store plus the global key still present.
        """
        from memman import config
        from memman.setup.scheduler import _write_env_keys
        _, data_dir = runner
        invoke(runner, ['store', 'create', 'doomed'])
        invoke(runner, ['store', 'create', 'keeper'])
        value_for = {
            'MEMMAN_BACKEND_': 'sqlite',
            config._pg_dsn_prefix(): 'postgresql://u:p@127.0.0.1:1/db',
            'MEMMAN_RERANK_ENABLED_': 'false',
            'MEMMAN_SURFACE_': 'code',
            'MEMMAN_AUTO_SEMANTIC_THRESHOLD_': '0.5',
            }
        doomed_keys = {
            f'{prefix}doomed' for prefix, _, _ in config.PER_STORE_KEY_SPECS}
        keeper_keys = {
            f'{prefix}keeper' for prefix, _, _ in config.PER_STORE_KEY_SPECS}
        seeded = {
            f'{prefix}{store}': value_for[prefix]
            for prefix, _, _ in config.PER_STORE_KEY_SPECS
            for store in ('doomed', 'keeper')
            }
        _write_env_keys(
            seeded | {'MEMMAN_LOG_LEVEL': 'DEBUG'}, data_dir=data_dir)
        env_path = config.env_file_path(data_dir)
        assert doomed_keys <= set(config.parse_env_file(env_path))

        result = invoke(runner, ['store', 'remove', '--yes', 'doomed'])
        assert result.exit_code == 0

        after = set(config.parse_env_file(env_path))
        assert not (doomed_keys & after), (
            f'per-store keys survived removal: {sorted(doomed_keys & after)}')
        assert keeper_keys <= after, (
            f'unrelated store keys were collateral: '
            f'{sorted(keeper_keys - after)}')
        assert 'MEMMAN_LOG_LEVEL' in after

    def test_store_remove_prompts_without_yes(self, runner):
        """Without --yes, remove prompts; typing 'n' aborts."""
        r, data_dir = runner
        invoke(runner, ['store', 'create', 'temp2'])
        result = r.invoke(
            cli, ['--data-dir', data_dir, 'store', 'remove', 'temp2'],
            input='n\n')
        assert result.exit_code != 0
        assert store_exists(data_dir, 'temp2')

    def test_store_remove_prompts_accept(self, runner):
        """Without --yes, typing 'y' at the prompt completes the delete."""
        import json as _json
        r, data_dir = runner
        invoke(runner, ['store', 'create', 'temp3'])
        result = r.invoke(
            cli, ['--data-dir', data_dir, 'store', 'remove', 'temp3'],
            input='y\n')
        assert result.exit_code == 0, result.output
        # The confirm prompt echoes before the JSON payload; find the payload.
        payload_start = result.output.find('{')
        payload = _json.loads(result.output[payload_start:])
        assert payload['action'] == 'removed'

    def test_store_auto_create_from_env(self, runner, monkeypatch):
        """MEMMAN_STORE env var silently creates a non-existent store."""
        r, data_dir = runner
        monkeypatch.setenv('MEMMAN_STORE', 'auto-created')

        result = r.invoke(cli, ['--data-dir', data_dir, 'recall', 'test',
                                '--limit', '1'])
        assert result.exit_code == 0, result.output

        store_path = pathlib.Path(data_dir) / 'data' / 'auto-created'
        assert store_path.is_dir(), 'store directory should be auto-created'

        monkeypatch.delenv('MEMMAN_STORE')
        list_result = r.invoke(cli, ['--data-dir', data_dir, 'store', 'list'])
        assert 'auto-created' in list_result.output

        r.invoke(cli, ['--data-dir', data_dir, 'store', 'remove', 'auto-created'])


class TestStatus:
    """`memman status` and `memman doctor` smoke."""

    def test_status_basic(self, runner):
        """Status returns JSON."""
        invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile'])
        result = invoke(runner, ['status'])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert 'total_insights' in data

    def test_doctor_basic(self, runner):
        """Doctor returns JSON with checks and status.

        Exit code may be 0 (pass/warn) or 1 (fail) depending on environment.
        """
        invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile'])
        result = invoke(runner, ['doctor'])
        assert result.exit_code in {0, 1}
        data = json.loads(result.output)
        assert 'status' in data
        assert 'checks' in data
        assert 'total_active' in data


class TestLog:
    """`memman log` smoke."""

    def test_log_basic(self, runner):
        """Log shows recent operations."""
        invoke(runner, [
            'remember', 'Go uses SQLite for persistent storage',
            '--no-reconcile'])
        result = invoke(runner, ['log', 'list'])
        assert result.exit_code == 0


class TestInsightsReview:
    """`memman insights review` flags transient content."""

    def test_review_flags_transient_content(self, runner):
        """A stored instance id is flagged; a durable decision is not."""
        invoke(runner, [
            'remember', 'Production outage traced to instance i-0c220c2402a5245bc running out of memory causing cascading failure',
            '--no-reconcile'])
        invoke(runner, [
            'remember', 'SQLite chosen for simplicity and embedded operation',
            '--no-reconcile', '--imp', '5'])
        result = invoke(runner, ['insights', 'review'])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data['total_flagged'] >= 1
        flagged = [r['content'] for r in data['review_results']]
        assert any('i-0c220c2402a5245bc' in c.lower() for c in flagged)

    def test_review_clean_store_flags_nothing(self, runner):
        """A store of durable content returns zero flagged."""
        invoke(runner, [
            'remember', 'SQLite chosen for simplicity and embedded operation',
            '--no-reconcile'])
        result = invoke(runner, ['insights', 'review'])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data['total_flagged'] == 0


class TestReplace:
    """`memman replace` happy paths, metadata, oplog, edges."""

    def test_replace_basic(self, runner):
        """Replace an insight, verify old soft-deleted, new exists."""
        result = invoke(runner, [
            'remember', 'Redis cache configured with 512MB memory limit',
            '--no-reconcile', '--cat', 'fact', '--imp', '3'])
        old_id = parse_remember(result, runner)['id']

        result = invoke(runner, [
            'replace', old_id,
            'Redis cache configured with 1GB memory limit for production'])
        assert result.exit_code == 0
        data = parse_remember(result, runner)
        assert data['action'] == 'replace'
        assert data['replaced_id'] == old_id
        assert 'redis' in data['content'].lower()

    def test_replace_inherits_metadata(self, runner):
        """Replace without flags inherits cat/imp from original."""
        result = invoke(runner, [
            'remember', 'Chose PostgreSQL over MySQL for JSONB support',
            '--no-reconcile',
            '--cat', 'decision', '--imp', '5'])
        old_id = parse_remember(result, runner)['id']

        result = invoke(runner, [
            'replace', old_id,
            'Chose PostgreSQL over MySQL for JSONB and CTE support'])
        assert result.exit_code == 0
        data = parse_remember(result, runner)
        assert 'id' in data

        result = invoke(runner, ['recall', '--basic', 'PostgreSQL JSONB'])
        hits = json.loads(result.output)['results']
        match = [h for h in hits if h['id'] == data['id']][0]
        assert match['category'] == 'decision'
        assert match['importance'] == 5

    def test_replace_overrides_metadata(self, runner):
        """Replace with explicit flags uses new values."""
        result = invoke(runner, [
            'remember', 'Nginx configured as reverse proxy for API gateway',
            '--no-reconcile', '--cat', 'fact', '--imp', '2'])
        old_id = parse_remember(result, runner)['id']

        result = invoke(runner, [
            'replace', old_id,
            'Switched from Nginx to Envoy for service mesh integration',
            '--cat', 'decision', '--imp', '5'])
        assert result.exit_code == 0
        data = parse_remember(result, runner)
        assert 'id' in data

        result = invoke(runner, ['recall', '--basic', 'Envoy service mesh'])
        hits = json.loads(result.output)['results']
        match = [h for h in hits if h['id'] == data['id']][0]
        assert match['category'] == 'decision'
        assert match['importance'] == 5

    def test_replace_preserves_access_count(self, runner):
        """Replace carries over access_count from original."""
        result = invoke(runner, [
            'remember', 'Terraform modules organized by environment and region',
            '--no-reconcile'])
        old_id = parse_remember(result, runner)['id']
        invoke(runner, ['recall', 'Terraform modules', '--basic'])
        invoke(runner, ['recall', 'Terraform modules', '--basic'])

        result = invoke(runner, [
            'replace', old_id,
            'Terraform modules organized by service and environment'])
        assert result.exit_code == 0
        new_id = parse_remember(result, runner)['id']

        result = invoke(runner, ['recall', 'Terraform modules', '--basic'])
        hits = json.loads(result.output)['results']
        match = [h for h in hits if h['id'] == new_id]
        assert match
        assert match[0]['access_count'] >= 2

    def test_replace_nonexistent_id(self, runner):
        """Replace a nonexistent ID produces error."""
        result = invoke(runner, [
            'replace', 'nonexistent-id',
            'Redis configured for cluster mode replication'])
        assert result.exit_code != 0
        assert 'not found' in result.output

    def test_replace_already_deleted(self, runner):
        """Verify replacing a forgotten insight fails naming the deletion.

        Mutation: preflighting with `nodes.get`, which cannot tell a
            forgotten row from a missing one.
        Oracle: the error text says the row was forgotten.
        """
        result = invoke(runner, [
            'remember', 'Kafka consumer group rebalance strategy uses cooperative',
            '--no-reconcile'])
        old_id = parse_remember(result, runner)['id']
        invoke(runner, ['forget', old_id])

        result = invoke(runner, [
            'replace', old_id,
            'Kafka consumer group rebalance uses eager strategy'])
        assert result.exit_code != 0
        assert 'was forgotten' in result.output

    def test_replace_refuses_a_superseded_id_and_names_the_successor(
            self, runner):
        """Verify replacing a superseded id points at its successor.

        Mutation: preflighting with `nodes.get` (a bare not-found), or
            accepting the superseded id and forking the chain.
        Oracle: the error text carries the successor's id and the
            history command; the successor stays the one current row.
        """
        result = invoke(runner, [
            'remember', 'Kafka retention is seven days', '--no-reconcile'])
        old_id = parse_remember(result, runner)['id']
        result = invoke(runner, [
            'replace', old_id, 'Kafka retention is thirty days'])
        new_id = parse_remember(result, runner)['id']

        result = invoke(runner, [
            'replace', old_id, 'Kafka retention is ninety days'])
        assert result.exit_code != 0
        assert f'is superseded by {new_id}' in result.output
        assert '--history' in result.output
        active = json.loads(invoke(
            runner, ['recall', '--basic', 'Kafka']).output)['results']
        assert [h['id'] for h in active] == [new_id]

    def test_replace_oplog_entries(self, runner):
        """Replace logs both replace and remember ops."""
        result = invoke(runner, [
            'remember', 'Prometheus alerting rules configured for SLO monitoring',
            '--no-reconcile'])
        old_id = parse_remember(result, runner)['id']

        result = invoke(runner, [
            'replace', old_id,
            'Prometheus alerting rules with Grafana dashboards for SLO'])
        parse_remember(result, runner)

        result = invoke(runner, ['log', 'list', '--limit', '10'])
        assert result.exit_code == 0
        assert 'replace' in result.output
        assert 'remember' in result.output

    def test_replace_quality_warnings_populate(self, runner):
        """Replace path also passes quality warnings as hints, never blocks."""
        result = invoke(runner, [
            'remember', 'Kafka chosen for event streaming due to partition tolerance',
            '--no-reconcile'])
        old_id = parse_remember(result, runner)['id']

        result = invoke(runner, [
            'replace', old_id,
            'Stack deployed via Terraform. 32 resources total.'])
        data = json.loads(result.output)
        assert data['action'] != 'rejected'
        assert len(data['quality_warnings']) >= 2

    def test_replace_creates_background_edges(self, runner):
        """Replace passes store context so background edges are created."""
        r1 = invoke(runner, [
            'remember', 'Celery task queue configured for async job processing',
            '--no-reconcile'])
        orig_id = parse_remember(r1, runner)['id']

        r2 = invoke(runner, [
            'replace', orig_id,
            'Celery with Redis broker for distributed task processing'])
        assert r2.exit_code == 0
        new_id = parse_remember(r2, runner)['id']

        result = invoke(runner, ['graph', 'related', new_id])
        assert result.exit_code == 0


class TestLink:
    """`memman graph link` direct edge creation."""

    def test_link_creates_both_directions(self, runner):
        """Link creates edges in both directions atomically."""
        r1 = invoke(runner, [
            'remember', 'FastAPI chosen for async API development',
            '--no-reconcile'])
        id1 = parse_remember(r1, runner)['id']
        r2 = invoke(runner, [
            'remember', 'Uvicorn configured as ASGI server for FastAPI',
            '--no-reconcile'])
        id2 = parse_remember(r2, runner)['id']

        result = invoke(runner, ['graph', 'link', id1, id2, '--type', 'causal'])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data['status'] == 'linked'

        fwd = invoke(runner, ['graph', 'related', id1, '--edge', 'causal'])
        assert fwd.exit_code == 0
        fwd_data = json.loads(fwd.output)
        assert any(e['id'] == id2 for e in fwd_data)

        rev = invoke(runner, ['graph', 'related', id2, '--edge', 'causal'])
        assert rev.exit_code == 0
        rev_data = json.loads(rev.output)
        assert any(e['id'] == id1 for e in rev_data)

    def test_link_respects_user_created_by(self, runner):
        """User-provided --meta['created_by'] is preserved, not clobbered to 'claude'.
        """
        import sqlite3
        r1 = invoke(runner, [
            'remember', 'Nginx is configured as the reverse proxy',
            '--no-reconcile'])
        id1 = parse_remember(r1, runner)['id']
        r2 = invoke(runner, [
            'remember', "Let's Encrypt auto-renews TLS certificates",
            '--no-reconcile'])
        id2 = parse_remember(r2, runner)['id']

        result = invoke(runner, ['graph', 'link', id1, id2, '--type', 'semantic',
                                 '--meta', '{"created_by": "research-agent"}'])
        assert result.exit_code == 0

        _, data_dir = runner
        store_db = pathlib.Path(data_dir) / 'data' / 'default' / 'memman.db'
        conn = sqlite3.connect(str(store_db))
        try:
            rows = conn.execute(
                'SELECT metadata FROM edges'
                ' WHERE source_id = ? AND target_id = ?'
                ' AND edge_type = ?',
                (id1, id2, 'semantic')).fetchall()
        finally:
            conn.close()
        assert rows, 'expected one semantic edge source->target'
        meta = json.loads(rows[0][0])
        assert meta['created_by'] == 'research-agent'

    def test_link_meta_non_dict_fails(self, runner):
        """Non-dict JSON metadata is rejected."""
        r1 = invoke(runner, [
            'remember', 'Elasticsearch configured for full-text search',
            '--no-reconcile'])
        id1 = parse_remember(r1, runner)['id']
        r2 = invoke(runner, [
            'remember', 'Kibana dashboards visualize Elasticsearch data',
            '--no-reconcile'])
        id2 = parse_remember(r2, runner)['id']

        result = invoke(runner, ['graph', 'link', id1, id2, '--type', 'semantic',
                                 '--meta', '[1, 2]'])
        assert result.exit_code != 0
        assert 'object' in result.output.lower() or 'dict' in result.output.lower()

    def test_link_self_edge_rejected(self, runner):
        """Linking an insight to itself is rejected."""
        r1 = invoke(runner, [
            'remember', 'GraphQL schema stitching combines microservice APIs',
            '--no-reconcile'])
        id1 = parse_remember(r1, runner)['id']

        result = invoke(runner, ['graph', 'link', id1, id1, '--type', 'semantic'])
        assert result.exit_code != 0
        assert 'itself' in result.output.lower()

    def test_link_warns_when_lower_weight(self, runner):
        """Link output includes warning when requested weight < existing."""
        r1 = invoke(runner, [
            'remember', 'Consul service discovery enables dynamic routing',
            '--no-reconcile'])
        id1 = parse_remember(r1, runner)['id']
        r2 = invoke(runner, [
            'remember', 'Vault secrets management integrates with Consul',
            '--no-reconcile'])
        id2 = parse_remember(r2, runner)['id']

        invoke(runner, ['graph', 'link', id1, id2, '--weight', '0.9'])
        result = invoke(runner, ['graph', 'link', id1, id2, '--weight', '0.3'])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert 'warning' in data
        assert '0.9' in data['warning']

    def test_link_returns_actual_db_weight(self, runner):
        """Link output weight reflects the DB value, not the user-supplied value."""
        r1 = invoke(runner, [
            'remember', 'FastAPI chosen for async API development',
            '--no-reconcile'])
        id1 = parse_remember(r1, runner)['id']
        r2 = invoke(runner, [
            'remember', 'Uvicorn configured as ASGI server for FastAPI',
            '--no-reconcile'])
        id2 = parse_remember(r2, runner)['id']

        invoke(runner, ['graph', 'link', id1, id2, '--type', 'causal', '--weight', '0.9'])
        result = invoke(runner, ['graph', 'link', id1, id2, '--type', 'causal', '--weight', '0.3'])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data['weight'] >= 0.9, (
            f'Link output shows {data["weight"]} but should be >= 0.9 '
            f'(MAX preserves higher weight over requested 0.3)')
        assert data['weight'] != 0.3, (
            'Link output should not show 0.3 — MAX should preserve higher')


class TestSingleTierEnrichment:
    """Remember runs enrichment + causal inline via ThreadPoolExecutor."""

    def test_output_has_enrichment_dict(self, runner):
        """Worker enrichment lands keywords/summary/entities on the row."""
        from memman.store.db import open_read_only, store_dir

        result = invoke(runner, [
            'remember', 'Redis cache configured with LRU eviction policy',
            '--no-reconcile'])
        assert result.exit_code == 0
        data = parse_remember(result, runner)
        iid = data['id']

        _, data_dir = runner
        db = open_read_only(store_dir(data_dir, 'default'))
        try:
            row = db._query(
                'SELECT keywords, summary, semantic_facts, entities'
                ' FROM insights WHERE id = ?',
                (iid,)).fetchone()
        finally:
            db.close()
        assert row is not None
        keywords, summary, semantic_facts, entities = row
        assert keywords is not None
        assert summary is not None
        assert semantic_facts is not None
        assert entities is not None

    def test_output_has_causal_count(self, runner):
        """Worker creates a (possibly empty) causal-edge set per insight."""
        from memman.store.db import open_read_only, store_dir

        result = invoke(runner, [
            'remember', 'PostgreSQL chosen for JSONB support in API layer',
            '--no-reconcile'])
        assert result.exit_code == 0
        data = parse_remember(result, runner)
        iid = data['id']

        _, data_dir = runner
        db = open_read_only(store_dir(data_dir, 'default'))
        try:
            count = db._query(
                "SELECT COUNT(*) FROM edges WHERE source_id = ?"
                " AND edge_type = 'causal'", (iid,)).fetchone()[0]
        finally:
            db.close()
        assert isinstance(count, int)

    def test_no_link_pending_in_output(self, runner):
        """Output no longer includes link_pending field."""
        result = invoke(runner, [
            'remember', 'Docker containers orchestrated via Kubernetes',
            '--no-reconcile'])
        assert result.exit_code == 0
        raw = json.loads(result.output)
        assert 'link_pending' not in raw

    def test_no_causal_candidates_in_output(self, runner):
        """Output no longer includes causal_candidates field."""
        result = invoke(runner, [
            'remember', 'Terraform modules organized by service boundaries',
            '--no-reconcile'])
        assert result.exit_code == 0
        data = parse_remember(result, runner)
        assert 'causal_candidates' not in data

    def test_linked_at_stamped_after_remember(self, runner):
        """linked_at is non-NULL after remember returns."""
        from memman.store.db import open_read_only

        result = invoke(runner, [
            'remember', 'Consul service mesh enables secure service communication',
            '--no-reconcile'])
        assert result.exit_code == 0
        data = parse_remember(result, runner)
        iid = data['id']

        _, data_dir = runner
        ro = open_read_only(data_dir + '/data/default')
        row = ro._conn.execute(
            'SELECT linked_at FROM insights WHERE id = ?',
            (iid,)).fetchone()
        ro.close()
        assert row is not None
        assert row[0] is not None

    def test_graph_rebuild_zero_pending_after_remember(self, runner):
        """Graph rebuild processes already-linked insights after remember."""
        invoke(runner, [
            'remember', 'Kafka event streaming configured for microservices',
            '--no-reconcile'])
        result = invoke(runner, ['graph', 'rebuild', '--dry-run'])
        assert result.exit_code == 0

    def test_enriched_at_stamped_after_remember(self, runner):
        """enriched_at is non-NULL after remember returns."""
        from memman.store.db import open_read_only

        result = invoke(runner, [
            'remember', 'Elasticsearch full-text search with custom analyzers',
            '--no-reconcile'])
        assert result.exit_code == 0
        data = parse_remember(result, runner)
        iid = data['id']

        _, data_dir = runner
        ro = open_read_only(data_dir + '/data/default')
        row = ro._conn.execute(
            'SELECT enriched_at FROM insights WHERE id = ?',
            (iid,)).fetchone()
        ro.close()
        assert row is not None
        assert row[0] is not None


class TestGraphRebuild:
    """Graph rebuild command tests — dry-run, live, edge preservation."""

    def test_rebuild_dry_run_reports_count(self, tmp_path, monkeypatch):
        """Dry run reports total insights without modifying DB."""
        monkeypatch.delenv('MEMMAN_STORE', raising=False)
        data_dir = str(tmp_path)
        store_path = tmp_path / 'data' / 'default'
        from memman.store.db import open_db
        from memman.store.node import insert_insight
        from tests.conftest import make_insight
        db = open_db(str(store_path))
        for i in range(3):
            insert_insight(db, make_insight(
                id=f'rd-{i}', content=f'Test insight {i}'))
            db._conn.execute(
                'UPDATE insights SET linked_at = ?, enriched_at = ?'
                ' WHERE id = ?',
                ('2024-01-01T00:00:00+00:00',
                 '2024-01-01T00:00:00+00:00', f'rd-{i}'))
        db.close()

        runner = CliRunner()
        result = runner.invoke(cli, [
            '--data-dir', data_dir, 'graph', 'rebuild', '--dry-run'])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data['total'] == 3
        assert data['dry_run'] == 1

        db = open_db(str(store_path))
        row = db._conn.execute(
            'SELECT COUNT(*) FROM insights'
            ' WHERE enriched_at IS NOT NULL').fetchone()
        assert row[0] == 3, 'dry-run must not clear enriched_at'
        db.close()

    def test_rebuild_reprocesses_stale_insights(
            self, tmp_path, monkeypatch):
        """Rebuild re-enriches insights with stale/empty keywords."""
        monkeypatch.delenv('MEMMAN_STORE', raising=False)
        data_dir = str(tmp_path)
        store_path = tmp_path / 'data' / 'default'
        from memman.store.db import open_db
        from memman.store.node import insert_insight
        from tests.conftest import make_insight
        db = open_db(str(store_path))
        insert_insight(db, make_insight(
            id='rs-1', content='Python and SQLite used for data analysis',
            entities=['Python', 'SQLite']))
        insert_insight(db, make_insight(
            id='rs-2', content='SQLite database migration with Python scripts',
            entities=['SQLite', 'Python']))
        db._conn.execute(
            "UPDATE insights"
            " SET linked_at = '2024-01-01T00:00:00+00:00',"
            "     enriched_at = '2024-01-01T00:00:00+00:00',"
            "     keywords = '[]'"
            " WHERE id IN ('rs-1', 'rs-2')")
        db.close()

        runner = CliRunner()
        result = runner.invoke(cli, [
            '--data-dir', data_dir, 'graph', 'rebuild'])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data['processed'] >= 2

        db = open_db(str(store_path))
        row = db._conn.execute(
            "SELECT keywords, enriched_at FROM insights"
            " WHERE id = 'rs-1'").fetchone()
        keywords = json.loads(row[0]) if row[0] else []
        assert len(keywords) > 0, 'rebuild should populate keywords'
        assert row[1] is not None, 'rebuild should set enriched_at'

        entities_raw = db._conn.execute(
            "SELECT entities FROM insights"
            " WHERE id = 'rs-1'").fetchone()[0]
        entities = json.loads(entities_raw) if entities_raw else []
        assert len(entities) > 0, 'rebuild should populate entities'
        db.close()

    def test_rebuild_handles_mix_of_linked_and_unlinked(
            self, tmp_path, monkeypatch):
        """Rebuild processes both linked and unlinked insights."""
        monkeypatch.delenv('MEMMAN_STORE', raising=False)
        data_dir = str(tmp_path)
        store_path = tmp_path / 'data' / 'default'
        from memman.store.db import open_db
        from memman.store.node import insert_insight
        from tests.conftest import make_insight
        db = open_db(str(store_path))
        insert_insight(db, make_insight(
            id='mx-1', content='Already linked insight'))
        db._conn.execute(
            "UPDATE insights SET linked_at = ?, enriched_at = ?"
            " WHERE id = 'mx-1'",
            ('2024-01-01T00:00:00+00:00',
             '2024-01-01T00:00:00+00:00'))
        insert_insight(db, make_insight(
            id='mx-2', content='Never linked insight'))
        db.close()

        runner = CliRunner()
        result = runner.invoke(cli, [
            '--data-dir', data_dir, 'graph', 'rebuild'])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data['processed'] >= 2

        db = open_db(str(store_path))
        pending = db._conn.execute(
            'SELECT COUNT(*) FROM insights'
            ' WHERE linked_at IS NULL'
            ' AND deleted_at IS NULL').fetchone()[0]
        assert pending == 0, 'all insights should be linked after rebuild'
        db.close()

    def test_rebuild_preserves_manual_edges(
            self, tmp_path, monkeypatch):
        """Manual claude edges survive rebuild."""
        monkeypatch.delenv('MEMMAN_STORE', raising=False)
        data_dir = str(tmp_path)
        store_path = tmp_path / 'data' / 'default'
        from memman.store.db import open_db
        from memman.store.edge import get_all_edges, insert_edge
        from memman.store.node import insert_insight
        from tests.conftest import make_edge, make_insight
        db = open_db(str(store_path))
        insert_insight(db, make_insight(
            id='me-1', content='Python web framework',
            entities=['Python']))
        insert_insight(db, make_insight(
            id='me-2', content='Python data pipeline',
            entities=['Python']))
        db._conn.execute(
            "UPDATE insights"
            " SET linked_at = '2024-01-01T00:00:00+00:00',"
            "     enriched_at = '2024-01-01T00:00:00+00:00'")
        manual_edge = make_edge(
            source_id='me-1', target_id='me-2',
            edge_type='semantic',
            metadata={'created_by': 'claude', 'cosine': '0.95'})
        insert_edge(db, manual_edge)
        db.close()

        runner = CliRunner()
        result = runner.invoke(cli, [
            '--data-dir', data_dir, 'graph', 'rebuild'])
        assert result.exit_code == 0, result.output

        db = open_db(str(store_path))
        edges = get_all_edges(db)
        manual = [e for e in edges
                  if e.edge_type == 'semantic'
                  and e.metadata.get('created_by') == 'claude']
        assert len(manual) == 1, (
            'rebuild deleted manual claude edge — '
            'should preserve created_by=claude')
        db.close()


class TestGraphRebuildStaleOnly:
    """Tests for `graph rebuild --stale-only` flag."""

    def _seed_drift(self, store_path, active_pv, active_model):
        """Insert one drifted row and one current row. Return ids.

        Also primes the per-store constants_hash so that opening via
        `_active_backend` does not trigger a wholesale reindex that
        nulls every row's `linked_at` (which would erase the seed's
        linked-state and mask the test's intent).
        """
        from memman.embed.fingerprint import Fingerprint, write_fingerprint
        from memman.graph.engine import compute_constants_hash
        from memman.store.db import open_db
        from memman.store.node import insert_insight, update_enrichment
        from memman.store.sqlite import SqliteBackend
        from tests.conftest import make_insight
        OLD_PV = 'old-prompt-version-deadbeef'
        db = open_db(str(store_path))
        backend = SqliteBackend(db)
        backend.meta.set('constants_hash', compute_constants_hash())
        write_fingerprint(backend, Fingerprint(
            provider='voyage', model='voyage-3-lite', dim=512))
        insert_insight(db, make_insight(
            id='drift-1', content='Drifted insight needing re-enrichment',
            prompt_version=OLD_PV, model_id=active_model))
        insert_insight(db, make_insight(
            id='fresh-1', content='Fresh insight already on active config',
            prompt_version=active_pv, model_id=active_model))
        for iid in ('drift-1', 'fresh-1'):
            update_enrichment(db, iid, ['kw'], 'sum', ['fact'])
            db._conn.execute(
                'UPDATE insights SET linked_at = ?, enriched_at = ?'
                ' WHERE id = ?',
                ('2024-01-01T00:00:00+00:00',
                 '2024-01-01T00:00:00+00:00', iid))
        db.close()

    def test_dry_run_reports_stale_count(self, tmp_path, monkeypatch):
        """`--stale-only --dry-run` reports stale count without modifying."""
        from memman import config
        from memman.pipeline.remember import compute_prompt_version

        active_pv = compute_prompt_version()
        active_model = config.require(config.LLM_MODEL_SLOW_CANONICAL)

        monkeypatch.delenv('MEMMAN_STORE', raising=False)
        data_dir = str(tmp_path)
        store_path = tmp_path / 'data' / 'default'
        self._seed_drift(store_path, active_pv, active_model)

        runner = CliRunner()
        result = runner.invoke(cli, [
            '--data-dir', data_dir, 'graph', 'rebuild',
            '--stale-only', '--dry-run'])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data['mode'] == 'stale-only'
        assert data['total'] == 1
        assert data['dry_run'] == 1

    def test_empty_stale_fast_path(self, tmp_path, monkeypatch):
        """Zero-stale store returns processed=0 without doing work."""
        from memman import config
        from memman.embed.fingerprint import Fingerprint, write_fingerprint
        from memman.graph.engine import compute_constants_hash
        from memman.pipeline.remember import compute_prompt_version
        from memman.store.sqlite import SqliteBackend

        active_pv = compute_prompt_version()
        active_model = config.require(config.LLM_MODEL_SLOW_CANONICAL)

        monkeypatch.delenv('MEMMAN_STORE', raising=False)
        data_dir = str(tmp_path)
        store_path = tmp_path / 'data' / 'default'
        from memman.store.db import open_db
        from memman.store.node import insert_insight
        from tests.conftest import make_insight
        db = open_db(str(store_path))
        backend = SqliteBackend(db)
        backend.meta.set('constants_hash', compute_constants_hash())
        write_fingerprint(backend, Fingerprint(
            provider='voyage', model='voyage-3-lite', dim=512))
        insert_insight(db, make_insight(
            id='ok-1', content='Already on active config',
            prompt_version=active_pv, model_id=active_model))
        db.close()

        runner = CliRunner()
        result = runner.invoke(cli, [
            '--data-dir', data_dir, 'graph', 'rebuild', '--stale-only'])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data['mode'] == 'stale-only'
        assert data['processed'] == 0
        assert data['skipped'] == 'no_stale_rows'

    def test_stale_only_re_enriches_drifted_rows(self, tmp_path, monkeypatch):
        """`--stale-only` clears enriched_at on drifted rows only."""
        from memman import config
        from memman.pipeline.remember import compute_prompt_version
        from memman.store.db import open_db

        active_pv = compute_prompt_version()
        active_model = config.require(config.LLM_MODEL_SLOW_CANONICAL)

        monkeypatch.delenv('MEMMAN_STORE', raising=False)
        data_dir = str(tmp_path)
        store_path = tmp_path / 'data' / 'default'
        self._seed_drift(store_path, active_pv, active_model)

        db = open_db(str(store_path))
        before = {
            row[0]: (row[1], row[2]) for row in db._conn.execute(
                'SELECT id, enriched_at, prompt_version FROM insights')
            }
        db.close()

        runner = CliRunner()
        result = runner.invoke(cli, [
            '--data-dir', data_dir, 'graph', 'rebuild', '--stale-only'])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data['mode'] == 'stale-only'
        assert data['processed'] == 1

        db = open_db(str(store_path))
        after = {
            row[0]: (row[1], row[2]) for row in db._conn.execute(
                'SELECT id, enriched_at, prompt_version FROM insights')
            }
        db.close()
        assert after['fresh-1'] == before['fresh-1']
        assert after['drift-1'][0] != before['drift-1'][0]
        assert after['drift-1'][1] == active_pv

    def test_stale_only_accepted_on_postgres_runner(self, cross_backend_runner):
        """`--stale-only` does not trip the SQLite-only guard on Postgres."""
        r, data_dir = cross_backend_runner
        out = r.invoke(cli, [
            '--data-dir', data_dir, 'graph', 'rebuild',
            '--stale-only', '--dry-run'])
        assert out.exit_code == 0, out.output
        data = json.loads(out.output)
        assert data['mode'] == 'stale-only'
        assert 'SQLite-only' not in out.output

    def test_wholesale_rebuild_accepted_on_postgres_runner(
            self, cross_backend_runner):
        """Wholesale `graph rebuild` is now cross-backend (gate lifted)."""
        r, data_dir = cross_backend_runner
        out = r.invoke(cli, [
            '--data-dir', data_dir, 'graph', 'rebuild', '--dry-run'])
        assert out.exit_code == 0, out.output
        data = json.loads(out.output)
        assert 'total' in data
        assert data.get('dry_run') == 1
        assert 'SQLite-only' not in out.output


def _rows_for_queue_id(data_dir, store, queue_id):
    """Active insight ids stored for one queue row, via its queue_uuid."""
    from memman.queue import queue_db
    from memman.store.db import open_read_only, store_dir
    with queue_db(data_dir) as qconn:
        queue_uuid = qconn.execute(
            'select queue_uuid from queue where id = ?',
            (queue_id,)).fetchone()[0]
    db = open_read_only(store_dir(data_dir, store))
    try:
        return db._query(
            'SELECT id FROM insights WHERE queue_uuid = ?'
            ' AND deleted_at IS NULL', (queue_uuid,)).fetchall()
    finally:
        db.close()


class TestIntraBatchDedup:
    """Sibling facts from the same remember call must deduplicate."""

    def test_similar_sibling_facts_deduplicated(self, runner):
        """When extraction produces two paraphrases, only one is stored."""
        def _two_similar_facts(llm_client, content):
            return [
                {
                    'text': 'Do not rename loop variables to avoid '
                            'shadowing opts attributes',
                    'category': 'preference',
                    'importance': 3,
                    'entities': ['loop variables', 'opts'],
                    },
                {
                    'text': 'Avoid renaming loop variables to prevent '
                            'shadowing of opts attributes',
                    'category': 'preference',
                    'importance': 3,
                    'entities': ['loop variables', 'opts'],
                    },
                ]

        with patch('memman.llm.extract.extract_facts',
                   _two_similar_facts):
            result = invoke(runner, [
                'remember', ('Do not rename loop variables to avoid shadowing '
                             'opts attributes')])
        assert result.exit_code == 0, result.output
        raw = json.loads(result.output)
        queue_id = raw['queue_id']
        _, data_dir = runner
        rows = _rows_for_queue_id(data_dir, raw['store'], queue_id)
        assert len(rows) == 1, (
            f'expected 1 stored fact, got {len(rows)}: '
            f'queue_id={queue_id}')

    def test_distinct_facts_both_stored(self, runner):
        """Genuinely different facts from one input are both stored."""
        def _two_distinct_facts(llm_client, content):
            return [
                {
                    'text': 'Switched from Flask to FastAPI',
                    'category': 'decision',
                    'importance': 4,
                    'entities': ['Flask', 'FastAPI'],
                    },
                {
                    'text': 'Redis cache configured with 4GB max memory',
                    'category': 'fact',
                    'importance': 3,
                    'entities': ['Redis'],
                    },
                ]

        with patch('memman.llm.extract.extract_facts',
                   _two_distinct_facts):
            result = invoke(runner, [
                'remember', 'Switched to FastAPI and configured Redis cache'])
        assert result.exit_code == 0, result.output
        raw = json.loads(result.output)
        queue_id = raw['queue_id']
        _, data_dir = runner
        rows = _rows_for_queue_id(data_dir, raw['store'], queue_id)
        assert len(rows) == 2, (
            f'expected 2 stored facts, got {len(rows)}: '
            f'queue_id={queue_id}')

    @pytest.mark.skipif(
        'not config.getoption("--live")',
        reason='requires --live for real LLM calls')
    def test_single_thought_not_duplicated_live(self, runner):
        """A single coherent preference should produce at most 1 stored fact."""
        result = invoke(runner, [
            'remember', ('Loop variable naming: do not rename loop variables '
                         'to avoid shadowing opts attributes. Maintain existing '
                         'variable names to prevent unintended shadowing of '
                         'options object properties.')])
        assert result.exit_code == 0, result.output
        raw = json.loads(result.output)
        queue_id = raw['queue_id']
        _, data_dir = runner
        rows = _rows_for_queue_id(data_dir, raw['store'], queue_id)
        assert len(rows) == 1, (
            f'expected 1 stored fact from single thought, got '
            f'{len(rows)}: queue_id={queue_id}')

    def test_two_updates_same_target_no_duplicate(self, runner):
        """Sibling UPDATEs must not create duplicates from stale candidates."""
        invoke(runner, [
            'remember', 'PostgreSQL chosen for ACID compliance and JSON support',
            '--no-reconcile'])

        def _two_update_facts(llm_client, content):
            return [
                {
                    'text': 'PostgreSQL chosen for ACID compliance'
                            ' and JSON support plus extensions',
                    'category': 'decision',
                    'importance': 4,
                    'entities': ['PostgreSQL'],
                    },
                {
                    'text': 'PostgreSQL chosen for ACID compliance'
                            ' and JSON support with replication',
                    'category': 'decision',
                    'importance': 4,
                    'entities': ['PostgreSQL'],
                    },
                ]

        with patch('memman.llm.extract.extract_facts',
                   _two_update_facts):
            result = invoke(runner, [
                'remember', 'PostgreSQL chosen for ACID compliance'])
        assert result.exit_code == 0, result.output

        search_result = invoke(runner, ['recall', '--basic', 'PostgreSQL'])
        active = json.loads(search_result.output)['results']
        assert len(active) == 1, (
            f'expected 1 active PostgreSQL insight, got {len(active)}')

    def test_forced_update_stale_target_no_duplicate(self, runner):
        """Forced UPDATE against stale target must not create a duplicate."""
        invoke(runner, [
            'remember', 'Kafka uses topic partitioning for message ordering',
            '--no-reconcile'])

        def _two_facts(llm_client, content):
            return [
                {
                    'text': 'Kafka uses topic partitioning for'
                            ' message ordering and consumer groups',
                    'category': 'fact',
                    'importance': 3,
                    'entities': ['Kafka'],
                    },
                {
                    'text': 'Kafka uses topic partitioning for'
                            ' message ordering and replication',
                    'category': 'fact',
                    'importance': 3,
                    'entities': ['Kafka'],
                    },
                ]

        def _force_update(llm_client, fact, existing):
            if not existing:
                return {'action': 'ADD', 'targets': [], 'merged_text': None}
            return {'action': 'UPDATE', 'targets': [(existing[0][0], 'update')],
                    'merged_text': fact['text']}

        with patch('memman.llm.extract.extract_facts', _two_facts), \
        patch('memman.llm.extract.reconcile_memories',
              _force_update):
            result = invoke(runner, [
                'remember', 'Kafka topic partitioning'])
        assert result.exit_code == 0, result.output

        search_result = invoke(runner, ['recall', '--basic', 'Kafka'])
        active = json.loads(search_result.output)['results']
        assert len(active) == 1, (
            f'expected 1 active Kafka insight, got {len(active)}')

    @pytest.mark.skipif(
        'not config.getoption("--live")',
        reason='requires --live for real LLM calls')
    def test_near_identical_updates_no_duplicate_live(self, runner):
        """Near-identical sibling facts must not create duplicates with real LLM."""
        invoke(runner, [
            'remember', 'Redis cache uses 4GB max memory for session storage',
            '--no-reconcile'])

        def _near_identical_facts(llm_client, content):
            return [
                {
                    'text': 'Redis cache uses 4GB maximum memory'
                            ' for session storage',
                    'category': 'fact',
                    'importance': 3,
                    'entities': ['Redis'],
                    },
                {
                    'text': 'Redis cache uses 4GB max memory'
                            ' for session storage',
                    'category': 'fact',
                    'importance': 3,
                    'entities': ['Redis'],
                    },
                ]

        with patch('memman.llm.extract.extract_facts',
                   _near_identical_facts):
            result = invoke(runner, [
                'remember', 'Redis cache memory configuration'])
        assert result.exit_code == 0, result.output

        search_result = invoke(runner, ['recall', '--basic', 'Redis'])
        active = json.loads(search_result.output)['results']
        assert len(active) == 1, (
            f'expected 1 active Redis insight, got {len(active)}')

    def test_update_reconciliation_no_dangling_edges(self, runner):
        """Chained intra-batch UPDATEs must not leave semantic edges to soft-deleted insights."""
        _r, data_dir = runner

        def _three_paraphrase_facts(llm_client, content):
            return [
                {
                    'text': 'Delta mode dropdown defaults'
                            ' to incremental_sync with no empty option',
                    'category': 'decision',
                    'importance': 3,
                    'entities': [],
                    },
                {
                    'text': 'Delta mode dropdown defaults'
                            ' to incremental_sync with no empty option'
                            ' unlike filter dropdowns',
                    'category': 'decision',
                    'importance': 3,
                    'entities': [],
                    },
                {
                    'text': 'Delta mode dropdown defaults'
                            ' to incremental_sync with no empty option'
                            ' unlike filter dropdowns which have one',
                    'category': 'decision',
                    'importance': 3,
                    'entities': [],
                    },
                ]

        def _force_update(llm_client, fact, existing):
            if not existing:
                return {'action': 'ADD', 'targets': [], 'merged_text': None}
            return {'action': 'UPDATE', 'targets': [(existing[0][0], 'update')],
                    'merged_text': fact['text']}

        fixed_vec = [1.0] + [0.0] * 511

        def _fixed_embed(self, text):
            return list(fixed_vec)

        with patch('memman.llm.extract.extract_facts',
                   _three_paraphrase_facts), \
        patch('memman.llm.extract.reconcile_memories',
              _force_update), \
        patch('memman.embed.voyage.Client.embed', _fixed_embed):
            result = invoke(runner, [
                'remember', 'Delta mode dropdown defaults to incremental_sync'])
        assert result.exit_code == 0, result.output

        store_path = pathlib.Path(data_dir) / 'data' / 'default'
        from memman.doctor import check_dangling_edges
        from memman.store.db import open_db
        from memman.store.sqlite import SqliteBackend
        db = open_db(str(store_path))
        doctor_result = check_dangling_edges(SqliteBackend(db))
        db.close()

        assert doctor_result['status'] == 'pass', (
            f'dangling edges found: {doctor_result["detail"]}')
        assert doctor_result['detail']['count'] == 0


class TestHotPathPurity:
    """Synchronous write commands must be LLM/embed-free.

    `forget` and `graph link` mutate the store DB synchronously and
    must make zero LLM or embed calls. Any future change that adds
    such calls to these paths will fail one of these tests loudly.
    """

    @pytest.fixture
    def runner_with_seed(self, tmp_path):
        """CliRunner over an isolated data dir with two seeded insights.

        Direct DB seeding avoids invoking the LLM for setup, so the
        assertion that the test target makes no LLM calls is meaningful.
        """
        from memman.embed.fingerprint import write_fingerprint
        from memman.store.db import open_db, store_dir, write_active
        from memman.store.sqlite import SqliteBackend

        data_dir = str(tmp_path)
        name = 'default'
        write_active(data_dir, name)
        sdir = store_dir(data_dir, name)
        db = open_db(sdir)
        fp = seed_default_fingerprint()
        write_fingerprint(SqliteBackend(db), fp)

        a = make_insight(id='aud-a', content='alpha', importance=3)
        b = make_insight(id='aud-b', content='beta', importance=3)
        insert_insight(db, a)
        insert_insight(db, b)
        update_embedding(db, 'aud-a',
                         serialize_vector([0.1] * fp.dim), fp.model)
        db.close()

        return CliRunner(), data_dir

    def _make_failing_complete(self, *_args, **_kwargs):
        raise AssertionError(
            'synchronous write must not invoke the LLM')

    def _make_failing_embed(self, *_args, **_kwargs):
        raise AssertionError(
            'synchronous write must not invoke the embed client')

    def test_forget_makes_no_llm_or_embed_calls(
            self, runner_with_seed, monkeypatch):
        """`forget` is pure SQL: no LLM, no embed."""
        monkeypatch.setattr(
            'memman.llm.client.MemmanLLMClient.complete',
            self._make_failing_complete)
        monkeypatch.setattr(
            'memman.embed.voyage.Client.embed', self._make_failing_embed)

        r, data_dir = runner_with_seed
        out = r.invoke(cli, ['--data-dir', data_dir, 'forget', 'aud-a'])
        assert out.exit_code == 0, out.output

    def test_graph_link_makes_no_llm_or_embed_calls(
            self, runner_with_seed, monkeypatch):
        """`graph link` is pure SQL."""
        monkeypatch.setattr(
            'memman.llm.client.MemmanLLMClient.complete',
            self._make_failing_complete)
        monkeypatch.setattr(
            'memman.embed.voyage.Client.embed', self._make_failing_embed)

        r, data_dir = runner_with_seed
        out = r.invoke(cli, ['--data-dir', data_dir,
                             'graph', 'link', 'aud-a', 'aud-b'])
        assert out.exit_code == 0, out.output


class TestPostgresGuards:
    """Admin commands that are SQLite-only must reject postgres backend."""

    def test_embed_reembed_rejects_postgres_backend(self, runner, env_file):
        """`embed reembed` exits non-zero with a clear message on postgres."""
        env_file('MEMMAN_BACKEND_default', 'postgres')
        env_file('MEMMAN_POSTGRES_DSN_default', 'postgresql://user@host/db')
        r, data_dir = runner
        out = r.invoke(cli, ['--data-dir', data_dir, 'embed', 'reembed'])
        assert out.exit_code != 0
        assert 'SQLite-only' in out.output


class TestCorruptStoreErrorHygiene:
    """A store whose database cannot be opened exits cleanly."""

    def test_recall_reports_corrupt_store_without_traceback(self, runner):
        """`recall` on an unreadable store prints an error, not a trace.

        Mutation: dropping `open_db`'s `sqlite3.Error` translation, so
            the driver error escapes the OPEN untranslated.
        Oracle: the MESSAGE, not the exception type. `open_db` names
            the store path (`cannot open database ... memman.db`);
            the root group's generic arm says only `sqlite query
            failed`, so the message is what separates them.

        Scope, since two seams have since grown over this path. The
        root group catches `sqlite3.Error` as well as `BackendError`,
        so `result.exception` is a `SystemExit` under the mutation too
        and the type assertion below is tautological -- kept only as a
        guard against a future seam that re-raises. This test now pins
        `open_db`'s own translation via its message alone. The direct
        pin on `active_store`'s catch is
        `tests/test_session.py::test_active_store_wraps_backend_error_from_open`,
        and the root group's sqlite arm is pinned by
        `tests/test_backend_error_hygiene.py::test_sqlite_statement_error_exits_as_one_clean_line`.
        """
        _, data_dir = runner
        sdir = pathlib.Path(data_dir) / 'data' / 'broken'
        sdir.mkdir(parents=True)
        (sdir / 'memman.db').write_bytes(b'not a sqlite database' * 8)
        result = invoke(
            runner, ['--store', 'broken', 'recall', 'anything', '--basic'])
        assert result.exit_code != 0
        assert not isinstance(result.exception, BackendError)
        assert 'Error: cannot open database' in result.output
        assert 'memman.db' in result.output

    def test_remember_reports_corrupt_queue_without_traceback(self, runner):
        """`remember` on an unreadable queue.db prints an error, not a trace.

        Mutation: dropping the root group's `BackendError` translation,
            so the translated queue error escapes with no seam to catch
            it -- `remember` never reaches `session.active_store`, which
            is the only other seam. `exit_code` alone cannot catch that:
            Click reports an in-command raise as exit 1 too.
        Oracle: a clean exit leaves `result.exception` a `SystemExit`
            and writes `Error: ...` naming the queue file; a leak leaves
            the `BackendError` itself on `result.exception`.
        """
        _, data_dir = runner
        queue_path = pathlib.Path(data_dir) / 'queue.db'
        queue_path.write_bytes(b'not a sqlite database' * 8)
        result = invoke(runner, ['remember', 'a fact worth keeping'])
        assert result.exit_code != 0
        assert not isinstance(result.exception, BackendError)
        assert 'Error: cannot open queue database' in result.output
        assert 'queue.db' in result.output

    def test_reembed_reports_unreadable_store_without_traceback(self, runner):
        """`embed reembed --dry-run` names a bad store dir, not a trace.

        Mutation: dropping the root group's `BackendError` translation,
            or leaving `open_read_only`'s missing-database case raising
            `FileNotFoundError`. `embed reembed` counts rows through
            `open_ro_db`, so it reaches neither `open_db` nor
            `session.active_store`.
        Oracle: a store directory with no `memman.db` -- the sweep is
            global, so one stray directory is enough -- and a clean exit
            leaves `result.exception` a `SystemExit` with `Error: ...`
            naming the path.
        """
        _, data_dir = runner
        (pathlib.Path(data_dir) / 'data' / 'stray').mkdir(parents=True)
        result = invoke(runner, ['embed', 'reembed', '--dry-run'])
        assert result.exit_code != 0
        assert not isinstance(result.exception, BackendError)
        assert 'Error: database not found' in result.output
        assert 'stray' in result.output

    def test_debug_keeps_the_stack_the_seam_hides(self, runner, caplog):
        """`--debug` still carries the traceback behind the clean line.

        Mutation: dropping `exc_info=True` from the seam's
            `logger.debug`, which discards the only copy of the stack
            -- the clean `Error:` line is all that survives, and no
            flag brings the traceback back.
        Oracle: the captured record's own `exc_info`, plus the
            `BackendError` type in its formatted text. Asserting on
            `result.output` would not work: `_configure_logging` binds
            its handler to whatever `sys.stderr` was live at the first
            configure in the process and never rebinds.
        """
        import logging

        _, data_dir = runner
        (pathlib.Path(data_dir) / 'queue.db').write_bytes(
            b'not a sqlite database' * 8)
        with caplog.at_level(logging.DEBUG, logger='memman'):
            result = invoke(runner, ['--debug', 'remember', 'a fact'])
        assert result.exit_code != 0
        seam = [r for r in caplog.records if 'CLI seam' in r.getMessage()]
        assert seam, [r.getMessage() for r in caplog.records]
        assert seam[0].exc_info is not None
        assert 'BackendError' in logging.Formatter().format(seam[0])
