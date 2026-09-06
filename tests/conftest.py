"""Shared fixtures for memman tests.

Dual-mode API mocking: mocked by default, real APIs with --live flag.

    pytest                    # fast, mocked LLM + embeddings
    pytest --live             # real Haiku + Voyage APIs (slow, needs keys)

Mock mode patches `MemmanLLMClient.complete` and `voyage.Client.embed`
at the HTTP layer, so all extraction/reconciliation/expansion logic
still runs with realistic canned responses. This exercises the real
code paths.
"""

import hashlib
import json
import re
import struct
from datetime import datetime, timezone
from pathlib import Path

import pytest
from memman.store.model import Edge, Insight

try:
    import psycopg  # noqa: F401
    import testcontainers.postgres  # noqa: F401
    pytest_plugins = ['tests.fixtures.postgres']
    _POSTGRES_AVAILABLE = True
except ImportError:
    pytest_plugins = ()
    _POSTGRES_AVAILABLE = False

EMBEDDING_DIM = 512


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-skip @pytest.mark.postgres tests when psycopg is not installed."""
    if _POSTGRES_AVAILABLE:
        return
    skip_pg = pytest.mark.skip(reason='postgres extras not installed')
    for item in items:
        if 'postgres' in item.keywords:
            item.add_marker(skip_pg)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register --live flag for real API calls."""
    parser.addoption(
        '--live', action='store_true', default=False,
        help='Use real Haiku LLM and Voyage embedding APIs')


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch, request):
    """Pin MEMMAN_DATA_DIR to tmp and seed the env file.

    Prevents the user's real `~/.memman/env` from leaking into the
    config resolver during unit tests. By default, seeds a fresh env
    file with `INSTALL_DEFAULTS` so runtime call sites resolve cleanly
    (no code-default fallback exists at runtime). Tests that need to
    assert "absent key" behavior mark themselves
    `@pytest.mark.no_default_env` and the seed step is skipped.

    Skipped entirely for e2e tests, which run real binaries with the
    inherited environment.
    """
    if 'tests/e2e/' in str(request.node.fspath):
        yield
        return
    import os

    from memman import config
    live_mode = request.config.getoption('--live')
    real_secrets = {}
    if live_mode:
        live_keys = (
            'MEMMAN_OPENROUTER_API_KEY', 'MEMMAN_VOYAGE_API_KEY',
            'MEMMAN_OPENAI_EMBED_API_KEY',
            'MEMMAN_LLM_API_KEY', 'MEMMAN_LLM_ENDPOINT',
            )
        for key in live_keys:
            val = os.environ.get(key)
            if val:
                real_secrets[key] = val
        home_env = Path.home() / '.memman' / config.ENV_FILENAME
        if home_env.exists():
            home_values = config.parse_env_file(home_env)
            for key in live_keys:
                if key in real_secrets:
                    continue
                val = home_values.get(key)
                if val:
                    real_secrets[key] = val
    data_dir = tmp_path / 'memman'
    monkeypatch.setenv('MEMMAN_DATA_DIR', str(data_dir))
    monkeypatch.delenv('MEMMAN_STORE', raising=False)
    monkeypatch.delenv('MEMMAN_DEBUG', raising=False)
    monkeypatch.delenv('MEMMAN_WORKER', raising=False)
    monkeypatch.delenv('MEMMAN_SCHEDULER_KIND', raising=False)
    monkeypatch.delenv('MEMMAN_SESSION_ID', raising=False)
    # The harness running the suite may itself be a Claude Code
    # session, and `--session` falls back to this variable. Leaving it
    # set would stamp the real session on every unsessioned test write
    # and make the assertions machine-dependent.
    monkeypatch.delenv('CLAUDE_CODE_SESSION_ID', raising=False)
    monkeypatch.delenv('MEMMAN_OPENROUTER_API_KEY', raising=False)
    monkeypatch.delenv('MEMMAN_VOYAGE_API_KEY', raising=False)
    monkeypatch.delenv('MEMMAN_OPENAI_EMBED_API_KEY', raising=False)
    monkeypatch.delenv('MEMMAN_LLM_API_KEY', raising=False)
    monkeypatch.delenv('MEMMAN_LLM_ENDPOINT', raising=False)
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    monkeypatch.delenv('VOYAGE_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    if live_mode and real_secrets:
        for key, val in real_secrets.items():
            monkeypatch.setenv(key, val)
    if 'no_default_env' not in request.keywords:
        _write_default_env_file(data_dir, real_secrets=real_secrets or None)
    config.reset_file_cache()
    from memman.embed import registry as _embed_registry
    _embed_registry.reset_for_tests()
    yield
    config.reset_file_cache()
    _embed_registry.reset_for_tests()


_TEST_MOCK_SECRETS = {
    'MEMMAN_OPENROUTER_API_KEY': 'mock-key-for-testing',
    'MEMMAN_VOYAGE_API_KEY': 'mock-voyage-key-for-testing',
    'MEMMAN_LLM_API_KEY': 'mock-llm-api-key-for-testing',
    }


def _set_env_file_value(key: str, value: str | None) -> None:
    """Write or remove a key in the active test env file.

    Replacement for `monkeypatch.setenv` for installable keys -- the
    runtime resolver no longer reads `os.environ`, so tests must mutate
    the env file directly. Pass `value=None` to remove the key.
    """
    import os

    from memman import config
    data_dir = os.environ.get(config.DATA_DIR)
    if not data_dir:
        raise RuntimeError(
            '_set_env_file_value requires MEMMAN_DATA_DIR;'
            ' invoke from a test that uses the _isolate_env fixture')
    path = config.env_file_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = config.parse_env_file(path) if path.exists() else {}
    if value is None:
        rows.pop(key, None)
    else:
        rows[key] = value
    contents = '\n'.join(f'{k}={v}' for k, v in rows.items()) + '\n'
    path.write_text(contents)
    config.reset_file_cache()


@pytest.fixture
def env_file():
    """Yield a callable that writes/removes keys in the test env file.

    Usage: `env_file('MEMMAN_LLM_MODEL_FAST', 'foo')` writes the row;
    `env_file('MEMMAN_LLM_MODEL_FAST', None)` removes it. Cache is
    auto-reset; the autouse `_isolate_env` fixture handles cleanup.
    """
    return _set_env_file_value


def _write_default_env_file(data_dir, real_secrets=None):
    """Seed `<data_dir>/env` with `INSTALL_DEFAULTS` for tests.

    Mirrors a post-install state so runtime call sites (which use
    `config.require`) resolve cleanly. By default seeds mock API key
    values since the runtime resolver no longer consults `os.environ`
    -- the keys must live in the env file. Pass `real_secrets={...}`
    (from `--live` mode) to seed real credentials captured from the
    shell instead. Tests that need the broken state opt out via
    `@pytest.mark.no_default_env`.
    """
    from memman import config
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / config.ENV_FILENAME
    secrets = dict(_TEST_MOCK_SECRETS)
    if real_secrets:
        if (config.LLM_API_KEY not in real_secrets
                and 'MEMMAN_OPENROUTER_API_KEY' in real_secrets):
            secrets.pop(config.LLM_API_KEY, None)
        secrets.update(real_secrets)
    if (config.LLM_API_KEY not in secrets
            and 'MEMMAN_OPENROUTER_API_KEY' in secrets):
        secrets[config.LLM_API_KEY] = secrets['MEMMAN_OPENROUTER_API_KEY']
    rows = list(config.INSTALL_DEFAULTS.items()) + list(secrets.items())
    contents = '\n'.join(f'{k}={v}' for k, v in rows) + '\n'
    path.write_text(contents)
    path.chmod(0o600)
    config.reset_file_cache()


@pytest.fixture(autouse=True)
def _reset_heartbeat_state():
    """Clear the module-level heartbeat dict between tests.

    `cli._LAST_HEARTBEAT_AT` is process-global; in-process CliRunner
    tests share it. Reset before AND after each test to prevent
    cross-test contamination if a future fixture reuses a data_dir.
    """
    from memman.cli import _reset_heartbeat_state as _reset
    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _scheduler_started(request, monkeypatch):
    """Force scheduler state to STARTED so writes are accepted in tests.

    cli.py's `_require_started` rejects writes when `read_state()`
    returns STATE_STOPPED. The autouse fixture monkeypatches it to
    STARTED for all non-e2e tests.

    Inline-mode auto-drain is NOT injected here -- write commands
    enqueue and return immediately, exactly as production. Tests that
    need to read what was just written go through the
    `MemmanCliRunner` (default `runner` fixture) which auto-drains
    after `remember`/`replace`. Tests that intentionally inspect a
    pre-drain queue should use the `no_auto_drain` mark.
    """
    if 'tests/e2e/' in str(request.node.fspath):
        return
    if request.node.fspath.basename == 'test_scheduler_setup.py':
        return
    if 'no_scheduler_started_mock' in request.keywords:
        return
    from memman.setup import scheduler as sched_mod
    monkeypatch.setattr(sched_mod, 'read_state',
                        lambda: sched_mod.STATE_STARTED)

    import click.testing
    original_invoke = click.testing.CliRunner.invoke

    def _wrapped_invoke(self, cli_obj, args=None, **kwargs):
        result = original_invoke(self, cli_obj, args, **kwargs)
        if (result.exit_code == 0
                and 'no_auto_drain' not in request.keywords
                and _args_target_write(args)):
            data_dir = _args_data_dir(args)
            if data_dir is not None:
                _force_drain_with(self.__class__, data_dir, original_invoke)
        return result

    monkeypatch.setattr(
        click.testing.CliRunner, 'invoke', _wrapped_invoke)


_AUTO_DRAIN_TRIGGERS = ('remember', 'replace')


def _args_target_write(args) -> bool:
    if not args:
        return False
    for arg in args:
        if isinstance(arg, str) and arg in _AUTO_DRAIN_TRIGGERS:
            return True
    return False


def _args_data_dir(args) -> str | None:
    if not args:
        return None
    seq = list(args)
    for i, arg in enumerate(seq):
        if arg == '--data-dir' and i + 1 < len(seq):
            return seq[i + 1]
    return None


def _force_drain_with(runner_cls, data_dir, original_invoke) -> None:
    """Run `scheduler drain` via the underlying click invoke.

    Bypasses the autouse-wrapped `invoke` to avoid re-triggering the
    auto-drain path on the drain command itself.
    """
    from memman.cli import cli
    instance = runner_cls()
    result = original_invoke(
        instance, cli,
        ['--data-dir', data_dir, 'scheduler', 'drain'])
    assert result.exit_code == 0, (
        f'force_drain failed: exit={result.exit_code} '
        f'output={result.output} exc={result.exception}')


def force_drain(data_dir: str) -> None:
    """Synchronously drain the queue for the given data dir.

    Tests that follow `remember`/`replace` with a read assertion call
    this to flush pending work through the worker before reading. Uses
    the same `scheduler drain` code path the OS timer fires.
    """
    import click.testing
    from memman.cli import cli
    instance = click.testing.CliRunner()
    result = instance.invoke(
        cli, ['--data-dir', data_dir,
              'scheduler', 'drain'])
    assert result.exit_code == 0, (
        f'force_drain failed: exit={result.exit_code} '
        f'output={result.output} exc={result.exception}')


@pytest.fixture(autouse=True)
def _autoseed_fingerprint(request, monkeypatch):
    """Auto-seed `meta.embed_fingerprint` on `bound_embedder`.

    `tmp_db` already writes a fingerprint via `write_fingerprint`; this
    fixture additionally backstops tests that hand-build backends and
    then call `bound_embedder(backend)` -- it seeds the env-active
    fingerprint on first lookup so those tests don't need
    `seed_if_fresh` boilerplate.

    Tests that exercise the strict production behavior (raw missing-
    fingerprint error from `bound_embedder`) should mark themselves
    `@pytest.mark.no_autoseed_fingerprint`.
    """
    if 'tests/e2e/' in str(request.node.fspath):
        return
    if 'no_autoseed_fingerprint' in request.keywords:
        return

    from memman.embed import fingerprint as fp_mod
    real_bound = fp_mod.bound_embedder

    def seed_then_bound(backend):
        if fp_mod.stored_fingerprint(backend) is None:
            from memman.embed import get_client
            fp_mod.write_fingerprint(
                backend, fp_mod.Fingerprint.from_client(get_client()))
        return real_bound(backend)

    monkeypatch.setattr(fp_mod, 'bound_embedder', seed_then_bound)


@pytest.fixture(autouse=True)
def _mock_apis(request, monkeypatch):
    """Mock LLM and embedding HTTP calls unless --live is set.

    Patches at the method layer: MemmanLLMClient.complete returns
    realistic JSON that the real extract/reconcile/expand code parses.
    Voyage embed returns a deterministic content-hash vector.
    `openrouter_models.resolve_latest_for_role` is stubbed to a fixed
    id so install-path tests never hit the network.

    Tests that exercise the real MemmanLLMClient.complete method
    should mark themselves with `@pytest.mark.no_mock_llm` to skip
    the method-level patch while keeping the resolver and embedding
    stubs in place.
    """
    if 'tests/e2e/' in str(request.node.fspath):
        return
    if request.config.getoption('--live'):
        return

    if 'no_mock_llm' not in request.keywords:
        monkeypatch.setattr(
            'memman.llm.client.MemmanLLMClient.complete',
            _mock_llm_complete)

    def _stub_resolve_role(role, endpoint='https://openrouter.ai/api/v1'):
        family = 'haiku' if role == 'fast' else 'sonnet'
        return f'anthropic/claude-{family}-4.5'

    monkeypatch.setattr(
        'memman.llm.openrouter_models.resolve_latest_for_role',
        _stub_resolve_role)
    monkeypatch.setattr(
        'memman.embed.voyage.Client.embed', _mock_embed)
    monkeypatch.setattr(
        'memman.embed.voyage.Client.embed_batch', _mock_embed_batch)
    monkeypatch.setattr(
        'memman.embed.voyage.Client.available', lambda self: True)
    from memman import config
    config.reset_file_cache()
    from memman.llm import client as llm_client_mod
    from memman.llm import extract as llm_extract_mod
    llm_client_mod.reset_role_cache()
    llm_extract_mod.reset_expand_cache()


def _mock_llm_complete(self: object, system: str, user: str,
                       **kwargs: object) -> str:
    """Route to appropriate mock based on system prompt content.

    Accepts and ignores keyword args (e.g. `temperature`) so callers
    that pass them for reproducibility don't break the mock.
    """
    if 'ADD|UPDATE|SUPERSEDE|NONE' in system:
        return _mock_reconciliation(user)
    if 'Expand a search query' in system:
        return _mock_query_expansion(user)
    if 'keyword' in system.lower() and 'enrichment' in system.lower():
        return _mock_enrichment(user)
    if 'causal' in system.lower():
        return _mock_causal(user)
    return json.dumps({'facts': [{'text': user, 'category': 'fact',
                                  'entities': []}]})


def _mock_reconciliation(prompt: str) -> str:
    """Generate a reconciliation response in the one-fact contract.

    Parses the structured prompt for the existing memories and the one
    new fact. Word overlap with the closest memory decides: UPDATE over
    0.6, NONE over 0.4, else ADD. One entry, top-level `merged_text`.
    """
    existing = {}
    fact_lines = []
    in_existing = False
    in_new = False
    for line in prompt.split('\n'):
        if line.startswith('EXISTING MEMORIES:'):
            in_existing = True
            in_new = False
            continue
        if line.startswith('NEW FACT:'):
            in_new = True
            in_existing = False
            continue
        if in_existing:
            m = re.match(r'\[(\d+)\]\s+(.*)', line)
            if m:
                existing[m.group(1)] = m.group(2)
        if in_new and line.strip():
            fact_lines.append(line.strip())

    fact = ' '.join(fact_lines)
    fact_lower = fact.lower()
    best_id = None
    best_overlap = 0
    for eid, econtent in existing.items():
        words_f = set(fact_lower.split())
        words_e = set(econtent.lower().split())
        overlap = len(words_f & words_e) / max(len(words_f | words_e), 1)
        if overlap > best_overlap:
            best_overlap = overlap
            best_id = eid

    if best_overlap > 0.6:
        return json.dumps({
            'merged_text': fact,
            'actions': [{'action': 'UPDATE', 'target_id': best_id,
                         'reason': 'similar content, updating'}],
            })
    if best_overlap > 0.4:
        return json.dumps({
            'merged_text': None,
            'actions': [{'action': 'NONE', 'target_id': best_id,
                         'reason': 'already captured'}],
            })
    return json.dumps({
        'merged_text': None,
        'actions': [{'action': 'ADD', 'target_id': None,
                     'reason': 'new information'}],
        })


def _mock_query_expansion(query: str) -> str:
    """Generate realistic query expansion response."""
    words = query.split()
    entities = [w for w in words if w[0:1].isupper()]
    return json.dumps({
        'expanded_query': query,
        'keywords': words,
        'entities': entities,
        'intent': 'GENERAL',
        })


def _mock_enrichment(content: str) -> str:
    """Generate realistic enrichment response."""
    return json.dumps({
        'keywords': content.lower().split()[:5],
        'summary': content[:100],
        'entities': _extract_mock_entities(content),
        })


def _mock_causal(content: str) -> str:
    """Generate realistic causal analysis response."""
    return json.dumps({'causal_links': []})


def _extract_mock_entities(text: str) -> list[str]:
    """Extract entities from text using capitalized words heuristic."""
    stopwords = {'The', 'A', 'An', 'In', 'On', 'For', 'With', 'And',
                 'Or', 'Is', 'Are', 'Was', 'Were', 'To', 'From', 'By',
                 'At', 'Of', 'But', 'Not', 'It', 'This', 'That', 'If',
                 'As', 'So', 'We', 'I', 'My', 'No', 'Yes', 'Chose',
                 'Switched', 'Store', 'Production', 'Configured',
                 'Infrastructure', 'Deployed', 'Critical', 'Uses',
                 'Using', 'Based', 'After', 'Before'}
    entities = []
    for word in re.findall(r'\b[A-Z][a-zA-Z0-9]+\b', text):
        if word not in stopwords and word not in entities:
            entities.append(word)
    return entities[:5]


def _mock_embed_batch(
        self: object, texts: list[str]) -> list[list[float]]:
    """Batch variant of `_mock_embed`. One vector per input."""
    return [_mock_embed(self, t) for t in texts]


def _mock_embed(self: object, text: str) -> list[float]:
    """Deterministic embedding from content hash.

    Reads target dimension from `self.dim` when available, falling
    back to `EMBEDDING_DIM`. Produces a unit vector seeded by content,
    so identical text gives identical vectors. Different text gives
    different vectors with low cosine similarity. Values are derived
    as int32-mapped uniforms so the float32 cast (used by pgvector)
    never produces NaN or Inf.
    """
    dim = getattr(self, 'dim', 0) or EMBEDDING_DIM
    digest = hashlib.sha256(text.encode()).digest()
    ints = list(struct.unpack(
        f'<{len(digest) // 4}i', digest))
    while len(ints) < dim:
        extra = hashlib.sha256(
            digest + len(ints).to_bytes(4, 'little')).digest()
        ints.extend(struct.unpack(f'<{len(extra) // 4}i', extra))
    ints = ints[:dim]
    floats = [x / (1 << 31) for x in ints]
    norm = sum(x * x for x in floats) ** 0.5
    if norm > 0:
        floats = [x / norm for x in floats]
    return floats


def _vec(*prefix: float, dim: int = EMBEDDING_DIM) -> list[float]:
    """Build a fixed-dim vector for cross-backend embedding tests.

    SQLite stores embeddings as BLOB and accepts any dimension;
    Postgres uses `vector(dim)` and rejects shorter vectors. Padding
    with zeros preserves cosine similarity for the math the call
    sites assert on.
    """
    return list(prefix) + [0.0] * (dim - len(prefix))


@pytest.fixture
def tmp_db(request, tmp_path):
    """Fresh SQLite database in temp directory.

    Seeds `meta.embed_fingerprint` to match the active client by
    default, mirroring `setup.claude._init_default_store`. Tests
    exercising unseeded behavior should use the
    `no_autoseed_fingerprint` mark.
    """
    from memman.store.db import open_db
    from memman.store.sqlite import SqliteBackend
    db = open_db(str(tmp_path))
    if 'no_autoseed_fingerprint' not in request.keywords:
        from memman.embed.fingerprint import seed_default_fingerprint
        from memman.embed.fingerprint import write_fingerprint
        write_fingerprint(
            SqliteBackend(db), seed_default_fingerprint())
    yield db
    db.close()


@pytest.fixture
def tmp_backend(tmp_db):
    """Wrap `tmp_db` in a SqliteBackend.

    Pipeline / search / graph entry points take `Backend`. Tests that
    drive those entry points against a fresh store use this fixture;
    the underlying DB and SqliteBackend share the same connection so
    free-function and verb-surface calls see one transaction.
    """
    from memman.store.sqlite import SqliteBackend
    return SqliteBackend(tmp_db)


def _backend_params() -> list:
    """Parametrize slots for the cross-backend `backend` fixture.

    SQLite is always present. Postgres only emits when `psycopg` and
    `testcontainers.postgres` are importable, and its slot carries
    `pytest.mark.postgres` so `pytest -m "not postgres"` skips it.
    """
    params = [pytest.param('sqlite', id='sqlite')]
    try:
        import psycopg  # noqa: F401
        import testcontainers.postgres  # noqa: F401
        params.append(pytest.param(
            'postgres', id='postgres',
            marks=pytest.mark.postgres))
    except ImportError:
        pass
    return params


@pytest.fixture(params=_backend_params())
def backend_kind(request) -> str:
    """The backend identifier for this parametrization slot."""
    return request.param


@pytest.fixture(params=_backend_params())
def runner_kind(request) -> str:
    """Backend identifier for CliRunner-driven cross-backend tests.

    Pairs with the `cross_backend_runner` fixture to flip the
    per-store `MEMMAN_BACKEND_<store>` between sqlite and postgres
    for each test invocation.
    """
    return request.param


@pytest.fixture
def cross_backend_runner(request, runner_kind, tmp_path, env_file, monkeypatch):
    """CliRunner whose env writes per-store keys for `<runner_kind>`.

    For postgres mode writes `MEMMAN_BACKEND_<store>` and
    `MEMMAN_POSTGRES_DSN_<store>` from the session container DSN and
    registers a teardown that drops the per-test schema. For sqlite
    mode writes `MEMMAN_DEFAULT_BACKEND=sqlite`. Returns the same
    `(runner, data_dir)` tuple shape as the legacy `runner` fixture
    in `test_memory_system.py` so a test can swap one for the other
    transparently.
    """
    import os

    from click.testing import CliRunner
    r = CliRunner()
    env_data_dir = os.environ.get('MEMMAN_DATA_DIR')
    data_dir = env_data_dir or str(tmp_path / 'memman_data')
    Path(data_dir).mkdir(parents=True, exist_ok=True)

    env_file('MEMMAN_DEFAULT_BACKEND', runner_kind)
    if runner_kind == 'postgres':
        pg_dsn = request.getfixturevalue('pg_dsn')
        store_name = _safe_store_name(request.node.name)
        env_file(f'MEMMAN_BACKEND_{store_name}', 'postgres')
        env_file(f'MEMMAN_POSTGRES_DSN_{store_name}', pg_dsn)
        env_file('MEMMAN_DEFAULT_POSTGRES_DSN', pg_dsn)
        monkeypatch.setenv('MEMMAN_STORE', store_name)

        def _drop_postgres_schema() -> None:
            try:
                from memman.store.factory import drop_store as _drop
                _drop(store_name, data_dir)
            except Exception:
                pass
        request.addfinalizer(_drop_postgres_schema)
    return r, data_dir


@pytest.fixture
def backend(request, backend_kind, tmp_path):
    """Cross-backend Backend fixture for pipeline tests.

    Parametrizes over `{sqlite, postgres}` (postgres slot active only
    when extras are importable). Yields a fully-isolated Backend with
    `meta.embed_fingerprint` pre-seeded so pipeline tests that touch
    embeddings do not trip the fingerprint refusal. Postgres tests
    get a unique store name per test so schemas don't collide; the
    schema is dropped on teardown.

    Pipeline / search / graph tests should use this fixture instead
    of `tmp_backend` to gain Postgres parity.
    """
    from memman.embed.fingerprint import META_KEY, seed_default_fingerprint
    pg_dsn = None
    if backend_kind == 'sqlite':
        from memman.store.sqlite import drop_sqlite_store, open_sqlite_backend
        data_dir = str(tmp_path / 'memman')
        store_name = 'test'
        b = open_sqlite_backend(store_name, data_dir)
    else:
        pg_dsn = request.getfixturevalue('pg_dsn')
        from memman.store.postgres import drop_postgres_store
        from memman.store.postgres import open_postgres_backend
        store_name = _safe_store_name(request.node.name)
        try:
            drop_postgres_store(store_name, pg_dsn)
        except Exception:
            pass
        b = open_postgres_backend(store_name, pg_dsn)
    b.meta.set(META_KEY, seed_default_fingerprint().to_json())
    try:
        yield b
    finally:
        try:
            b.close()
        except Exception:
            pass
        if backend_kind == 'postgres':
            try:
                drop_postgres_store(store_name, pg_dsn)
            except Exception:
                pass
        else:
            try:
                drop_sqlite_store(store_name, str(tmp_path / 'memman'))
            except Exception:
                pass


def _safe_store_name(test_id: str) -> str:
    """Derive a postgres-schema-safe store name from a test node id.

    Postgres identifiers must match `[a-z][a-z0-9_]*`; pytest test
    node ids contain `[`, `]`, `-`, `.`, etc. Replace non-alnum with
    underscores, lowercase, truncate to fit `_check_identifier`.
    """
    safe = ''.join(c if c.isalnum() else '_' for c in test_id).lower()
    if safe and not safe[0].isalpha():
        safe = 'p_' + safe
    return safe[:40] or 'p_test'


def set_created_at(backend, insight_id: str, when: datetime) -> None:
    """Test-only: directly UPDATE `created_at` on a stored insight.

    The Backend Protocol's `nodes.insert` ignores caller-passed
    `Insight.created_at` (server-side timestamps). Tests that
    exercise temporal logic against pre-existing rows with
    controlled timestamps call this helper after
    `backend.nodes.insert` to override the server-stamped value.
    Bypasses the Protocol intentionally; do NOT use outside test
    code.
    """
    from memman.store.model import format_timestamp
    from memman.store.sqlite import SqliteBackend
    when_str = format_timestamp(when)
    if isinstance(backend, SqliteBackend):
        backend._db._exec(
            'UPDATE insights SET created_at = ? WHERE id = ?',
            (when_str, insight_id))
    else:
        with backend._conn.cursor() as cur:
            cur.execute(
                f'UPDATE {backend._schema}.insights'
                ' SET created_at = %s WHERE id = %s',
                (when, insight_id))
        backend._conn.commit()


def make_insight(**overrides) -> Insight:
    """Factory for test Insight instances."""
    now = datetime.now(timezone.utc)
    defaults = {
        'id': 'test-id',
        'content': 'test content',
        'category': 'fact',
        'importance': 3,
        'entities': [],
        'source': 'test',
        'access_count': 0,
        'created_at': now,
        'updated_at': now,
        'deleted_at': None,
        'last_accessed_at': None,
        }
    defaults.update(overrides)
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


def insert_pending(db, insight_id: str, content: str = 'test content',
                   **kw) -> None:
    """Insert an insight with linked_at = NULL.

    Helper for graph/link tests that need pending insights as fixtures.
    Forwards extra kwargs to `make_insight` for content/category control.
    """
    from memman.store.node import insert_insight
    insert_insight(db, make_insight(id=insight_id, content=content, **kw))
    db._conn.execute(
        'UPDATE insights SET linked_at = NULL WHERE id = ?',
        (insight_id,))


@pytest.fixture
def queue_conn(tmp_path):
    """Fresh queue.db connection for direct queue helper tests."""
    from memman.queue import open_queue_db
    conn = open_queue_db(str(tmp_path))
    yield conn
    conn.close()


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect HOME and `Path.home` to a tmp_path.

    Used by setup-adjacent tests that touch `~/.memman` directly.
    Does not pin `MEMMAN_DATA_DIR` (the autouse `_isolate_env` already
    handles env scoping for unit tests). Tests that need both the
    redirect and an explicit data-dir under the fake home should
    construct it locally as `fake_home / 'memman'`.
    """
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    return tmp_path


def install_env_factory(data_dir, **keys: str | None) -> None:
    """Seed an env file at `data_dir` with selected keys.

    Replacement for the per-file `_write_keys` and `_install_env`
    helpers. Pass key=value to write a row; pass key=None to omit it.
    Recognized convenience aliases: ``openrouter`` -> OPENROUTER_API_KEY,
    ``voyage`` -> VOYAGE_API_KEY. Other kwargs are written as-is.
    """
    from pathlib import Path as _Path

    from memman import config
    aliases = {
        'openrouter': config.OPENROUTER_API_KEY,
        'voyage': config.VOYAGE_API_KEY,
        }
    p = _Path(data_dir)
    p.mkdir(parents=True, exist_ok=True)
    rows = []
    for k, v in keys.items():
        if v is None:
            continue
        real_key = aliases.get(k, k)
        rows.append(f'{real_key}={v}')
    if rows:
        path = p / config.ENV_FILENAME
        path.write_text('\n'.join(rows) + '\n')
        path.chmod(0o600)
    config.reset_file_cache()


def fake_subprocess(monkeypatch, target_module, active: bool = True) -> None:
    """Stub `subprocess` on `target_module` so tests don't shell out.

    `target_module` is the module under test that imports
    `subprocess` (typically `memman.setup.scheduler`). When `active`
    is True, the fake `run` returns a `_FakeResult` with returncode 0
    and stdout 'active'; when False, returncode 3 and stdout 'inactive'.
    `_record_subprocess` in `test_scheduler_setup.py` is a richer
    variant that captures call arguments; this helper covers the common
    case where the test only needs subprocess to be quiet.
    """
    class _FakeResult:
        returncode = 0 if active else 3
        stdout = 'active' if active else 'inactive'
        stderr = ''

    fake = type('S', (), {
        'run': staticmethod(lambda *a, **k: _FakeResult()),
        'TimeoutExpired': TimeoutError,
        })()
    monkeypatch.setattr(target_module, 'subprocess', fake)


def make_cli_runner(tmp_path, *, subdir: str = 'mm') -> tuple:
    """Build a `(CliRunner, data_dir)` tuple.

    Canonical replacement for the local `runner` fixtures duplicated
    across test_cli.py, test_cli_new_groups.py, test_worker_runs.py,
    test_provenance.py, test_doctor.py. Each call site can keep its
    `runner` fixture as a 2-line wrapper, or take the tuple inline via
    the `mm_runner` fixture below.

    The data_dir matches `MEMMAN_DATA_DIR` set by the autouse
    `_isolate_env` fixture so that env-file reads keyed off the CLI
    `--data-dir` arg find the seeded keys (per-store routing reads
    `<data_dir>/env` directly).
    """
    import os

    from click.testing import CliRunner
    r = CliRunner()
    env_data_dir = os.environ.get('MEMMAN_DATA_DIR')
    data_dir = env_data_dir or str(tmp_path / subdir)
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    return r, data_dir


@pytest.fixture
def mm_runner(tmp_path):
    """Default `(CliRunner, data_dir)` tuple for sqlite-only CLI tests.

    Tests that need cross-backend parity use `cross_backend_runner`
    instead. Local file-level `runner` fixtures should delegate here:
    `def runner(mm_runner): return mm_runner`. Once a callsite no
    longer needs a custom data_dir name, it can take `mm_runner`
    directly and drop the local fixture.
    """
    return make_cli_runner(tmp_path)


def invoke(runner_tuple, args):
    """Invoke memman CLI with `--data-dir` prepended.

    Shared replacement for the per-file `invoke` helpers.
    """
    from memman.cli import cli
    r, data_dir = runner_tuple
    return r.invoke(cli, ['--data-dir', data_dir] + args)


def parse_remember(result, runner_tuple=None):
    """Parse remember/replace output, returning a fact-shaped dict.

    Modern `remember`/`replace` returns just `{action: queued,
    queue_id, store}`. The autouse-drain runs the worker after the
    invocation, so the new insight lives in the store DB carrying the
    queue row's `queue_uuid` (source is provenance and defaults to
    `'user'` since D1, so it no longer identifies the row). This
    helper reads the uuid off the queue row — `purge_done` retains
    done rows for 60 s, ample inside a test — and looks the insight
    up by it. Postgres-aware: switches the lookup query when the
    per-store `MEMMAN_BACKEND_<store>=postgres` resolves.
    """
    raw = json.loads(result.output)
    if 'facts' in raw and raw['facts']:
        fact = dict(raw['facts'][0])
        fact['_raw'] = raw
        return fact
    if runner_tuple is None:
        return raw
    queue_id = raw.get('queue_id')
    if queue_id is None:
        return raw
    _, data_dir = runner_tuple
    from memman.queue import queue_db
    from memman.store.db import read_active
    from memman.store.factory import resolve_store_backend
    from memman.store.factory import resolve_store_pg_dsn
    with queue_db(data_dir) as qconn:
        qrow = qconn.execute(
            'select queue_uuid from queue where id = ?',
            (queue_id,)).fetchone()
    if qrow is None:
        return raw
    queue_uuid = qrow[0]
    name = raw.get('store') or read_active(data_dir) or 'default'
    backend_kind = resolve_store_backend(name, data_dir)
    if backend_kind == 'postgres':
        import psycopg
        from memman.store.postgres import _store_schema
        schema = _store_schema(name)
        sql = f"""
select id, content, category, importance
from {schema}.insights
where queue_uuid = %s
  and deleted_at is null
order by created_at
"""
        dsn = resolve_store_pg_dsn(name, data_dir)
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(sql, (queue_uuid,))
            rows = cur.fetchall()
    else:
        from memman.store.db import open_read_only, store_dir
        sdir = store_dir(data_dir, name)
        db = open_read_only(sdir)
        sql = """
select id, content, category, importance
from insights
where queue_uuid = ?
  and deleted_at is null
order by created_at
"""
        try:
            rows = db._query(sql, (queue_uuid,)).fetchall()
        finally:
            db.close()
    if not rows:
        return raw
    action = 'replace' if raw.get('replaced_id') else 'add'
    fact = {
        'id': rows[0][0],
        'content': rows[0][1],
        'category': rows[0][2],
        'importance': rows[0][3],
        'action': action,
        'replaced_id': raw.get('replaced_id'),
        '_raw': raw,
        }
    return fact
