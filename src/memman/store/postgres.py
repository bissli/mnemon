"""Postgres + pgvector implementation of the Backend Protocol surface.

Single-file parallel to `store/sqlite.py`. Schema-per-store layout:
each memman store maps to a Postgres schema named `store_<name>`,
holding the per-store tables (insights, edges, oplog, meta,
worker_runs).

Vector storage:
- `embedding vector(512)` (pgvector); pgvector adapter binds
  `list[float]` directly with no per-call serialization.
- HNSW index built `create index concurrently ... vector_cosine_ops
  where deleted_at is null and superseded_by is null`. Built outside
  any transaction; reindex drops invalid remnants
  (`pg_index.indisvalid`) before retrying.
- Similarity returned as `1 - (embedding <=> :q)` (cosine in
  [-1, 1]; higher better).

Recall issues one round-trip per anchor subset.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from types import TracebackType
from typing import TYPE_CHECKING, Any, ClassVar, Self

from memman import config
from memman.embed.fingerprint import Fingerprint
from memman.embed.vector import pgvector_to_list
from memman.migrate import PAYLOAD_VERSION, Artifact, BackendFeatures
from memman.migrate import MigrateEdge, MigrateError, MigrateInsight
from memman.migrate import MigrateOpLog, MigrationPayload, Migrator
from memman.migrate import PendingReembed, SwapState, sanitize_identifier
from memman.search.keyword import insight_tokens
from memman.store.backend import Backend, EdgeStore, MetaStore, NodeStore
from memman.store.backend import Oplog, RecallSession, _check_identifier
from memman.store.base import BaseNodeStore
from memman.store.errors import BackendError, ConfigError
from memman.store.model import Edge, EnrichmentCoverage, Id, Insight
from memman.store.model import NodeStats, OpLogEntry, OpLogStats
from memman.store.model import ProvenanceCount, ReembedRow, WorkerRun
from memman.store.model import parse_timestamp
from memman.store.node import unterminated_chains

if TYPE_CHECKING:
    import psycopg

logger = logging.getLogger('memman')

EMBEDDING_DIM = 512

# Postgres NAMEDATALEN is 64; an identifier is truncated to 63
# bytes, silently.
PG_NAME_MAX_CHARS = 63


def _store_schema(name: str) -> str:
    """Return the Postgres schema name for a memman store.

    Parameters
    ----------
    name : str
        Store name, unprefixed.

    Returns
    -------
    str
        `store_<name>`.

    Raises
    ------
    ConfigError
        When `name` is not a SQL identifier, or when the prefixed
        schema would exceed `PG_NAME_MAX_CHARS`.

    Notes
    -----
    - The length check covers the prefixed schema, not the bare name.
      Postgres truncates an over-long identifier to NAMEDATALEN-1
      silently, so two store names differing only past that point map
      to one schema and share its rows.
    """
    _check_identifier(name)
    schema = f'store_{name}'
    if len(schema) > PG_NAME_MAX_CHARS:
        raise ConfigError(
            f'store name {name!r} is too long: schema {schema!r} is'
            f' {len(schema)} characters and postgres truncates past'
            f' {PG_NAME_MAX_CHARS}, which would silently merge it'
            f' with another store')
    return schema


def _lock_id(name: str) -> int:
    """Deterministic int8 lock id for `pg_advisory_*lock` calls.

    Postgres advisory locks take a signed int8. blake2b digest_size=8
    yields exactly that. Python's built-in `hash()` is randomized per
    process via PYTHONHASHSEED, so two memman processes computing a
    lock id from the same input would not serialize against each other.
    """
    digest = hashlib.blake2b(name.encode('utf-8'), digest_size=8).digest()
    return int.from_bytes(digest, 'big', signed=True)


def _advisory_lock_key(schema: str, name: str) -> int:
    """Per-store, per-name int8 key for `pg_advisory_*lock` calls."""
    return _lock_id(f'{schema}:{name}')


PG_BASELINE_SCHEMA = """
create table if not exists {schema}.insights (
    id          text primary key,
    content     text not null,
    category    text default 'fact',
    importance  integer default 3,
    entities    jsonb default '[]'::jsonb,
    source      text default 'user',
    access_count integer default 0,
    keywords    jsonb,
    summary     text,
    semantic_facts jsonb,
    last_accessed_at timestamptz,
    embedding   vector({dim}),
    linked_at   timestamptz,
    enriched_at timestamptz,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    deleted_at  timestamptz,
    prompt_version text,
    model_id    text,
    embedding_model text,
    session_id  text,
    queue_uuid  text,
    corroboration_count integer not null default 0,
    kw_tokens   text[] not null,
    superseded_by text
);

create table if not exists {schema}.edges (
    source_id   text not null,
    target_id   text not null,
    edge_type   text not null,
    weight      double precision default 1.0,
    metadata    jsonb default '{{}}'::jsonb,
    created_at  timestamptz not null default now(),
    primary key (source_id, target_id, edge_type),
    foreign key (source_id) references {schema}.insights(id) on delete cascade,
    foreign key (target_id) references {schema}.insights(id) on delete cascade,
    constraint edges_edge_type_check_{schema}
        check (edge_type in ('temporal','semantic','causal','entity'))
);

create table if not exists {schema}.oplog (
    id          bigserial primary key,
    operation   text not null,
    insight_id  text,
    detail      text default '',
    created_at  timestamptz not null default now(),
    before      jsonb,
    after       jsonb,
    legacy_id   bigint,
    constraint oplog_legacy_id_key_{schema} unique (legacy_id)
);

create table if not exists {schema}.meta (
    key   text primary key,
    value text not null
);

create table if not exists {schema}.worker_runs (
    id            bigserial primary key,
    started_at    timestamptz not null default now(),
    ended_at      timestamptz,
    rows_processed integer not null default 0,
    error         text not null default '',
    last_heartbeat_at timestamptz
);

create index if not exists idx_insights_category_{schema}
    on {schema}.insights(category);
create index if not exists idx_insights_importance_{schema}
    on {schema}.insights(importance);
create index if not exists idx_insights_created_{schema}
    on {schema}.insights(created_at);
create index if not exists idx_insights_deleted_{schema}
    on {schema}.insights(deleted_at);
create index if not exists idx_insights_source_{schema}
    on {schema}.insights(source);
create index if not exists idx_insights_session_{schema}
    on {schema}.insights(session_id);
create index if not exists idx_insights_queue_uuid_{schema}
    on {schema}.insights(queue_uuid);
create index if not exists idx_insights_corroboration_{schema}
    on {schema}.insights(corroboration_count);
create index if not exists idx_insights_pending_link_{schema}
    on {schema}.insights(linked_at, created_at)
    where linked_at is null and deleted_at is null and superseded_by is null;
create index if not exists idx_insights_kw_tokens_{schema}
    on {schema}.insights using gin (kw_tokens)
    where deleted_at is null and superseded_by is null;
create index if not exists idx_insights_current_listing_{schema}
    on {schema}.insights(deleted_at, superseded_by, importance, created_at);

create index if not exists idx_edges_source_{schema}
    on {schema}.edges(source_id);
create index if not exists idx_edges_target_{schema}
    on {schema}.edges(target_id);
create index if not exists idx_edges_type_{schema}
    on {schema}.edges(edge_type);
create index if not exists idx_edges_source_type_{schema}
    on {schema}.edges(source_id, edge_type);
create index if not exists idx_edges_target_type_{schema}
    on {schema}.edges(target_id, edge_type);
create index if not exists idx_oplog_created_{schema}
    on {schema}.oplog(created_at);
"""

_MAX_OPLOG_ENTRIES = 5000


_REINDEX_CREATED_BY_FILTER = {
    'semantic': "metadata->>'created_by' = 'auto'",
    'entity': ("(metadata->>'created_by' is null"
               " or metadata->>'created_by'"
               " not in ('claude', 'manual'))"),
    'causal': ("(metadata->>'created_by' is null"
               " or metadata->>'created_by'"
               " not in ('llm', 'claude', 'manual'))"),
    }

_PER_NODE_CREATED_BY_FILTER = {
    'entity': ("(metadata->>'created_by' is null"
               " or metadata->>'created_by'"
               " not in ('claude', 'manual'))"),
    'semantic': ("(metadata->>'created_by' is null"
                 " or metadata->>'created_by' = 'auto')"),
    'causal': "metadata->>'created_by' = 'llm'",
    }


def _open_connection(
        dsn: str, *, autocommit: bool = False,
        keepalives: bool = False,
        connect_timeout: int | None = None,
        register_vector: bool = True) -> psycopg.Connection:
    """Open a fresh psycopg connection with pgvector adapters.

    `keepalives=True` adds `keepalives_idle=30` for the drain-lock
    connection so a hung worker is detected by the kernel rather
    than holding the lock indefinitely.

    Set `register_vector=False` for probes that must run against a
    database where the `vector` extension may legitimately be absent
    (e.g., the install wizard's pgvector-presence check). The
    pgvector adapter raises `ProgrammingError` on register when the
    extension is missing; skipping registration lets callers detect
    absence with their own SQL probe.

    Returns a bare connection; lock-holding paths (`drain_lock`,
    `reembed_lock`) and long-lived backend connections own the
    lifecycle directly. One-shot helpers should use `_connection()`
    below for guaranteed close-on-exit semantics.
    """
    import psycopg
    from pgvector.psycopg import register_vector as _register_vector
    kwargs: dict[str, Any] = {'autocommit': autocommit}
    if keepalives:
        kwargs['keepalives'] = 1
        kwargs['keepalives_idle'] = 30
    if connect_timeout is not None:
        kwargs['connect_timeout'] = connect_timeout
    # Notes:
    # - An unreachable or rejecting server is an ordinary operator
    #   condition, so it leaves here as BackendError. The lock paths
    #   call this directly, bypassing `_connection` below.
    try:
        conn = psycopg.connect(dsn, **kwargs)
        if register_vector:
            _register_vector(conn)
    except psycopg.Error as exc:
        raise BackendError(
            f'postgres connection failed: {exc}') from exc
    return conn


@contextmanager
def _connection(
        dsn: str, *, autocommit: bool = False,
        keepalives: bool = False,
        connect_timeout: int | None = None,
        register_vector: bool = True
        ) -> Iterator[psycopg.Connection]:
    """Context-manager wrapper around `_open_connection`.

    Closes on exit (psycopg3's `with conn:` is transaction-scoped, not
    close-scoped, so wrapping `_open_connection` is the way to get
    deterministic close-on-exit semantics for one-shot helpers
    without losing the `register_vector` adapter setup).

    Notes
    -----
    - Translating here, rather than at each statement, covers every
      query in the scope: memman's contract is that a backend raises
      `BackendError`, and a driver exception does not satisfy it.
    - A caller that branches on a driver type nests its own handler
      around the statement instead. Catching there runs first, so the
      branch is taken before this wrapper sees the error.
    """
    import psycopg as _psycopg

    conn = _open_connection(
        dsn, autocommit=autocommit, keepalives=keepalives,
        connect_timeout=connect_timeout,
        register_vector=register_vector)
    try:
        yield conn
    except _psycopg.Error as exc:
        raise BackendError(f'postgres query failed: {exc}') from exc
    finally:
        try:
            conn.close()
        except _psycopg.Error as exc:
            logger.debug(f'pg connection close failed: {exc}')


def _datetime_or_none(v: Any) -> datetime | None:
    """Coerce a psycopg timestamp value to a UTC-aware datetime.

    psycopg returns TIMESTAMPTZ as `datetime`; this helper normalizes
    naive datetimes (defensive: pgvector / older drivers may strip
    tzinfo) to UTC and passes through None.
    """
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v
    if isinstance(v, str):
        try:
            return parse_timestamp(v)
        except ValueError:
            return None
    return None


def _row_to_insight(row: tuple[Any, ...]) -> Insight:
    """Map a select row into an Insight dataclass."""
    i = Insight()
    i.id = row[0]
    i.content = row[1]
    i.category = row[2] or 'fact'
    i.importance = row[3] if row[3] is not None else 3
    ents = row[4]
    if isinstance(ents, list):
        i.entities = ents
    elif isinstance(ents, str):
        i.parse_entities(ents)
    else:
        i.entities = []
    i.source = row[5] or 'user'
    i.access_count = row[6] or 0
    i.created_at = _datetime_or_none(row[7])
    i.updated_at = _datetime_or_none(row[8])
    i.deleted_at = _datetime_or_none(row[9])
    if len(row) > 10 and row[10]:
        i.summary = row[10]
    if len(row) > 11:
        i.linked_at = _datetime_or_none(row[11])
    if len(row) > 12:
        i.enriched_at = _datetime_or_none(row[12])
    if len(row) > 13:
        i.last_accessed_at = _datetime_or_none(row[13])
    if len(row) > 14 and row[14]:
        i.session_id = row[14]
    if len(row) > 15 and row[15]:
        i.queue_uuid = row[15]
    if len(row) > 16 and row[16] is not None:
        i.corroboration_count = int(row[16])
    if len(row) > 17 and row[17]:
        i.superseded_by = row[17]
    return i


def _row_to_edge(row: tuple[Any, ...]) -> Edge:
    """Map a select row into an Edge dataclass."""
    e = Edge()
    e.source_id = row[0]
    e.target_id = row[1]
    e.edge_type = row[2]
    e.weight = row[3] if row[3] is not None else 1.0
    md = row[4]
    if isinstance(md, dict):
        e.metadata = md
    elif isinstance(md, str):
        e.parse_metadata(md)
    else:
        e.metadata = {}
    e.created_at = _datetime_or_none(row[5])
    return e


# `session_id`, `queue_uuid`, `corroboration_count`, then
# `superseded_by`, appended last -- must stay byte-identical to
# node.py's _INSIGHT_COLUMNS (see
# test_insight_column_lists_are_identical_across_backends).
_INSIGHT_COLS = (
    'id, content, category, importance, entities,'
    ' source, access_count, created_at, updated_at, deleted_at,'
    ' summary, linked_at, enriched_at, last_accessed_at,'
    ' session_id, queue_uuid, corroboration_count, superseded_by')


class PostgresNodeStore(BaseNodeStore, NodeStore):
    """NodeStore implementation against a per-store Postgres schema."""

    def __init__(
            self, conn: psycopg.Connection, schema: str) -> None:
        self._conn = conn
        self._schema = schema
        self._embedding_dim: int | None = None

    def _q(self, sql: str) -> str:
        """Format SQL with the per-store schema interpolated."""
        return sql.format(s=self._schema)

    def _resolve_embedding_dim(self) -> int:
        """Look up the stored `vector(N)` column width; cached.

        pgvector exposes `N` directly in `pg_attribute.atttypmod`.
        Cached on first call because `iter_for_reembed` is hot and the
        schema dim cannot change without an `embed swap` cutover.
        """
        if self._embedding_dim is not None:
            return self._embedding_dim
        sql = """
select atttypmod from pg_attribute
where attrelid = (%s || '.insights')::regclass
  and attname = 'embedding'
  and not attisdropped
"""
        with self._conn.cursor() as cur:
            cur.execute(sql, (self._schema,))
            row = cur.fetchone()
        if row is None or row[0] is None or int(row[0]) <= 0:
            raise BackendError(
                f'schema {self._schema!r} has no resolved embedding'
                f' dim; was the baseline schema applied?')
        self._embedding_dim = int(row[0])
        return self._embedding_dim

    def insert(self, ins: Insight) -> None:
        sql = self._q("""
insert into {s}.insights
    (id, content, category, importance, entities,
     source, access_count, prompt_version, model_id, embedding_model,
     session_id, queue_uuid, corroboration_count, kw_tokens)
values (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s)
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (
                ins.id, ins.content, ins.category, ins.importance,
                ins.entities_json(), ins.source, ins.access_count,
                ins.prompt_version, ins.model_id, ins.embedding_model,
                ins.session_id, ins.queue_uuid,
                ins.corroboration_count,
                sorted(insight_tokens(ins))))

    def get(self, id: Id) -> Insight | None:
        sql = self._q(f"""
select {_INSIGHT_COLS}
from {{s}}.insights
where id = %s and deleted_at is null and superseded_by is null
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (id,))
            row = cur.fetchone()
            return _row_to_insight(row) if row else None

    def get_include_deleted(self, id: Id) -> Insight | None:
        sql = self._q(f"""
select {_INSIGHT_COLS}
from {{s}}.insights
where id = %s
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (id,))
            row = cur.fetchone()
            return _row_to_insight(row) if row else None

    def get_many(self, ids: Sequence[Id]) -> list[Insight]:
        if not ids:
            return []
        sql = self._q(f"""
select {_INSIGHT_COLS}
from {{s}}.insights
where id = any(%s) and deleted_at is null and superseded_by is null
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (list(ids),))
            rows = cur.fetchall()
        by_id = {r[0]: _row_to_insight(r) for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    def query(
            self, *, keyword: str = '', category: str = '',
            source: str = '', limit: int = 20) -> list[Insight]:
        conditions = ['deleted_at is null and superseded_by is null']
        args: list[Any] = []
        if keyword:
            for word in keyword.split():
                conditions.append(
                    '(content ilike %s or entities::text ilike %s'
                    ' or keywords::text ilike %s)')
                pat = f'%{word}%'
                args.extend([pat, pat, pat])
        if category:
            conditions.append('category = %s')
            args.append(category)
        if source:
            conditions.append('source = %s')
            args.append(source)
        args.append(limit)
        where_clause = ' and '.join(conditions)
        sql = self._q(f"""
select {_INSIGHT_COLS}
from {{s}}.insights
where {where_clause}
order by importance desc, created_at desc
limit %s
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, tuple(args))
            return [_row_to_insight(r) for r in cur.fetchall()]

    def soft_delete(self, id: Id) -> bool:
        # Emptying `kw_tokens` sheds a token set the keyword scan can
        # no longer reach: 69% of rows on the biggest store are
        # soft-deleted, and populating them too measured at +56% of
        # table size against +17% for active rows alone.
        update_sql = self._q("""
update {s}.insights
set deleted_at = now(), updated_at = now(), kw_tokens = '{{}}'
where id = %s and deleted_at is null
""")
        delete_sql = self._q("""
delete from {s}.edges
where source_id = %s or target_id = %s
""")
        with self._conn.cursor() as cur:
            cur.execute(update_sql, (id,))
            if cur.rowcount == 0:
                return False
            cur.execute(delete_sql, (id, id))
        return True

    def supersede(self, predecessor_id: Id, successor_id: Id) -> bool:
        # `kw_tokens` stays populated, unlike `soft_delete`: the GIN
        # predicate and `keyword_counts` already exclude superseded
        # rows, and `unsupersede` must not have to recompute it.
        update_sql = self._q("""
update {s}.insights
set superseded_by = %s, updated_at = now()
where id = %s and deleted_at is null and superseded_by is null
""")
        delete_sql = self._q("""
delete from {s}.edges
where source_id = %s or target_id = %s
""")
        with self._conn.cursor() as cur:
            cur.execute(update_sql, (successor_id, predecessor_id))
            if cur.rowcount == 0:
                return False
            cur.execute(delete_sql, (predecessor_id, predecessor_id))
        return True

    def unsupersede(self, id: Id, expected_successor: Id) -> bool:
        sql = self._q("""
update {s}.insights
set superseded_by = null, updated_at = now()
where id = %s and deleted_at is null and superseded_by = %s
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (id, expected_successor))
            return bool(cur.rowcount == 1)

    def predecessors(self, successor_id: Id) -> list[Insight]:
        sql = self._q(f"""
select {_INSIGHT_COLS}
from {{s}}.insights
where superseded_by = %s
order by created_at, id
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (successor_id,))
            return [_row_to_insight(r) for r in cur.fetchall()]

    def supersession_integrity(self) -> dict[str, list[Id]]:
        dangling_sql = self._q("""
select p.id
from {s}.insights p
left join {s}.insights s on s.id = p.superseded_by
where p.superseded_by is not null and s.id is null
order by p.id
""")
        edges_sql = self._q("""
select distinct i.id
from {s}.insights i
join {s}.edges e on e.source_id = i.id or e.target_id = i.id
where i.superseded_by is not null and i.deleted_at is null
order by i.id
""")
        self_sql = self._q("""
select id from {s}.insights where superseded_by = id order by id
""")
        pointers_sql = self._q("""
select id, superseded_by from {s}.insights where superseded_by is not null
""")
        out: dict[str, list[Id]] = {}
        with self._conn.cursor() as cur:
            for key, sql in (('dangling', dangling_sql),
                             ('superseded_with_edges', edges_sql),
                             ('self_pointer', self_sql)):
                cur.execute(sql)
                out[key] = [r[0] for r in cur.fetchall()]
            cur.execute(pointers_sql)
            out['unterminated'] = unterminated_chains(dict(cur.fetchall()))
        return out

    def update_entities(self, id: Id, entities: list[str]) -> None:
        seen: set[str] = set()
        deduped: list[str] = []
        for e in entities:
            key = e.strip().lower()
            if key not in seen:
                seen.add(key)
                deduped.append(e)
        # `kw_tokens` covers content AND entities, so an entity edit
        # that left it alone would keep answering `keyword_counts`
        # from the pre-edit token set. Content is read back rather
        # than taken from the caller because both call sites pass
        # entities only.
        content_sql = self._q(
            'select content from {s}.insights where id = %s')
        sql = self._q("""
update {s}.insights
set entities = %s::jsonb,
    kw_tokens = %s,
    updated_at = now()
where id = %s
""")
        with self._conn.cursor() as cur:
            cur.execute(content_sql, (id,))
            row = cur.fetchone()
            if row is None:
                return
            edited = Insight(content=row[0], entities=deduped)
            cur.execute(sql, (
                edited.entities_json(),
                sorted(insight_tokens(edited)),
                id))

    def update_enrichment(
            self, id: Id, *, keywords: list[str], summary: str,
            semantic_facts: list[str]) -> None:
        import json as _json
        sql = self._q("""
update {s}.insights
set keywords = %s::jsonb,
    summary = %s,
    semantic_facts = %s::jsonb,
    updated_at = now()
where id = %s
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (
                _json.dumps(keywords), summary,
                _json.dumps(semantic_facts), id))

    def increment_access_count(self, id: Id) -> None:
        sql = self._q("""
update {s}.insights
set access_count = access_count + 1, last_accessed_at = now()
where id = %s and deleted_at is null and superseded_by is null
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (id,))

    def increment_corroboration(
            self, id: Id, *, queue_uuid: str | None = None) -> bool:
        sql = self._q("""
update {s}.insights
set corroboration_count = corroboration_count + 1,
    queue_uuid = coalesce(queue_uuid, %s)
where id = %s and deleted_at is null and superseded_by is null
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (queue_uuid, id))
            return bool(cur.rowcount == 1)

    def count_active(self) -> int:
        sql = self._q("""
select count(*) from {s}.insights where deleted_at is null and superseded_by is null
""")
        with self._conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def count_total(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute(self._q('select count(*) from {s}.insights'))
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def has_active_with_queue_uuid(self, queue_uuid: str) -> bool:
        sql = self._q("""
select 1 from {s}.insights
where queue_uuid = %s and deleted_at is null
limit 1
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (queue_uuid,))
            return cur.fetchone() is not None

    def get_by_queue_uuid(self, queue_uuid: str) -> list[Insight]:
        sql = self._q(f"""
select {_INSIGHT_COLS}
from {{s}}.insights
where queue_uuid = %s
  and deleted_at is null
order by created_at, id
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (queue_uuid,))
            return [_row_to_insight(r) for r in cur.fetchall()]

    def iter_for_reembed(
            self, cursor: Id, batch: int) -> list[ReembedRow]:
        sql = self._q("""
select id, content, embedding_model,
       case when embedding is null then null else %s end
from {s}.insights
where deleted_at is null and superseded_by is null and id > %s
order by id
limit %s
""")
        with self._conn.cursor() as cur:
            cur.execute(
                sql, (self._resolve_embedding_dim() * 8, cursor, batch))
            return [
                ReembedRow(
                    id=r[0], content=r[1], embedding_model=r[2],
                    blob_length=r[3])
                for r in cur.fetchall()
                ]

    def count_orphans(self) -> tuple[int, int]:
        total_sql = self._q("""
select count(*) from {s}.insights where deleted_at is null and superseded_by is null
""")
        orphan_sql = self._q("""
select count(*) from {s}.insights i
where i.deleted_at is null and i.superseded_by is null
  and not exists (
      select 1 from {s}.edges e
      where e.source_id = i.id or e.target_id = i.id
  )
""")
        with self._conn.cursor() as cur:
            cur.execute(total_sql)
            total = int(cur.fetchone()[0])
            cur.execute(orphan_sql)
            orphans = int(cur.fetchone()[0])
        return orphans, total

    def provenance_distribution(self) -> list[ProvenanceCount]:
        sql = self._q("""
select prompt_version, model_id, count(*)
from {s}.insights
where deleted_at is null and superseded_by is null
group by prompt_version, model_id
order by count(*) desc
""")
        with self._conn.cursor() as cur:
            cur.execute(sql)
            return [
                ProvenanceCount(
                    prompt_version=r[0], model_id=r[1], count=int(r[2]))
                for r in cur.fetchall()
                ]

    def get_recent_in_window(
            self, *, exclude_id: Id, window_hours: float,
            limit: int) -> list[Insight]:
        sql = self._q(f"""
select {_INSIGHT_COLS}
from {{s}}.insights
where id <> %s
  and deleted_at is null and superseded_by is null
  and created_at >= now() - (%s * interval '1 hour')
order by created_at desc
limit %s
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (exclude_id, window_hours, limit))
            return [_row_to_insight(r) for r in cur.fetchall()]

    def get_latest_by_session(
            self, *, session_id: str | None,
            exclude_id: Id) -> Insight | None:
        # Falsy guard inside the backend: '' = '' matches in SQL and
        # would fuse every unsessioned row into one false chain.
        if not session_id:
            return None
        sql = self._q(f"""
select {_INSIGHT_COLS}
from {{s}}.insights
where session_id = %s and id <> %s and deleted_at is null and superseded_by is null
order by created_at desc, id desc
limit 1
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (session_id, exclude_id))
            row = cur.fetchone()
            return _row_to_insight(row) if row else None

    def get_recent_active(
            self, *, exclude_id: Id, limit: int) -> list[Insight]:
        sql = self._q(f"""
select {_INSIGHT_COLS}
from {{s}}.insights
where id <> %s and deleted_at is null and superseded_by is null
order by created_at desc
limit %s
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (exclude_id, limit))
            return [_row_to_insight(r) for r in cur.fetchall()]

    def get_all_active(self) -> list[Insight]:
        sql = self._q(f"""
select {_INSIGHT_COLS}
from {{s}}.insights
where deleted_at is null and superseded_by is null
order by created_at desc
""")
        with self._conn.cursor() as cur:
            cur.execute(sql)
            return [_row_to_insight(r) for r in cur.fetchall()]

    def stats(self) -> NodeStats:
        active_sql = self._q("""
select count(*) from {s}.insights where deleted_at is null and superseded_by is null
""")
        superseded_sql = self._q("""
select count(*) from {s}.insights
where deleted_at is null and superseded_by is not null
""")
        deleted_sql = self._q("""
select count(*) from {s}.insights where deleted_at is not null
""")
        cat_sql = self._q("""
select category, count(*)
from {s}.insights
where deleted_at is null and superseded_by is null
group by category
""")
        ent_sql = self._q("""
select je, count(distinct i.id) cnt
from {s}.insights i, jsonb_array_elements_text(i.entities) je
where i.deleted_at is null and i.superseded_by is null
group by je
order by cnt desc
limit 20
""")
        with self._conn.cursor() as cur:
            cur.execute(active_sql)
            total = int(cur.fetchone()[0])
            cur.execute(superseded_sql)
            superseded = int(cur.fetchone()[0])
            cur.execute(deleted_sql)
            deleted = int(cur.fetchone()[0])
            cur.execute(cat_sql)
            by_category = {r[0]: int(r[1]) for r in cur.fetchall()}
            cur.execute(self._q('select count(*) from {s}.edges'))
            edges = int(cur.fetchone()[0])
            cur.execute(self._q('select count(*) from {s}.oplog'))
            oplog = int(cur.fetchone()[0])
            top_entities: list[dict[str, Any]] = []
            try:
                cur.execute(ent_sql)
                for entity, cnt in cur.fetchall():
                    top_entities.append(
                        {'entity': entity, 'count': int(cnt)})
            except Exception as exc:
                logger.warning(f'top_entities query failed: {exc}')
        return NodeStats(
            total_insights=total, superseded_insights=superseded,
            deleted_insights=deleted,
            edge_count=edges, oplog_count=oplog,
            by_category=by_category, top_entities=top_entities)

    def update_embedding(
            self, id: Id, vec: list[float], model: str) -> None:
        sql = self._q("""
update {s}.insights
set embedding = %s::vector,
    embedding_model = %s,
    updated_at = now()
where id = %s
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (vec, model, id))

    def get_embedding(self, id: Id) -> bytes | None:
        sql = self._q("""
select embedding from {s}.insights
where id = %s and deleted_at is null and superseded_by is null
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (id,))
            row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        from memman.embed.vector import serialize_vector
        return serialize_vector(pgvector_to_list(row[0]))

    def get_all_embeddings(self) -> list[tuple[Id, str, bytes]]:
        from memman.embed.vector import serialize_vector
        sql = self._q("""
select id, content, embedding
from {s}.insights
where deleted_at is null and superseded_by is null and embedding is not null
""")
        with self._conn.cursor() as cur:
            cur.execute(sql)
            results: list[tuple[Id, str, bytes]] = []
            for rid, content, vec in cur.fetchall():
                if vec is None:
                    continue
                results.append(
                    (rid, content, serialize_vector(pgvector_to_list(vec))))
        return results

    def iter_embeddings_as_vecs(
            self) -> Iterator[tuple[Id, list[float]]]:
        sql = self._q("""
select id, embedding
from {s}.insights
where deleted_at is null and superseded_by is null and embedding is not null
""")
        with self._conn.cursor() as cur:
            cur.execute(sql)
            for rid, vec in cur.fetchall():
                if vec is None:
                    continue
                yield rid, pgvector_to_list(vec)

    def embedding_stats(self) -> tuple[int, int]:
        sql = self._q("""
select count(*),
       count(*) filter (where embedding is not null)
from {s}.insights
where deleted_at is null and superseded_by is null
""")
        with self._conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    def enrichment_coverage(self) -> EnrichmentCoverage:
        sql = self._q("""
select count(*),
       count(*) filter (where embedding is null),
       count(*) filter (
           where keywords is null
              or keywords::text = '[]'
              or jsonb_typeof(keywords) is null
       ),
       count(*) filter (
           where (summary is null or summary = '')
             and enriched_at is null
       ),
       count(*) filter (
           where semantic_facts is null
              or semantic_facts::text = '[]'
              or jsonb_typeof(semantic_facts) is null
       )
from {s}.insights
where deleted_at is null and superseded_by is null
""")
        with self._conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
        if row is None:
            return EnrichmentCoverage()
        return EnrichmentCoverage(
            total_active=int(row[0] or 0),
            missing_embedding=int(row[1] or 0),
            missing_keywords=int(row[2] or 0),
            missing_summary=int(row[3] or 0),
            missing_semantic_facts=int(row[4] or 0))

    def embedding_size_distribution(self) -> dict[int, int]:
        sql = self._q("""
select vector_dims(embedding), count(*)
from {s}.insights
where deleted_at is null and superseded_by is null and embedding is not null
group by vector_dims(embedding)
""")
        with self._conn.cursor() as cur:
            cur.execute(sql)
            return {
                int(size): int(count) for size, count in cur.fetchall()}

    def stamp_linked(self, id: Id) -> None:
        with self._conn.cursor() as cur:
            cur.execute(self._q(
                'update {s}.insights set linked_at = now() where id = %s'),
                (id,))

    def stamp_enriched(
            self, id: Id, *,
            prompt_version: str | None = None) -> None:
        with self._conn.cursor() as cur:
            if prompt_version is None:
                cur.execute(self._q(
                    'update {s}.insights set enriched_at = now()'
                    ' where id = %s'),
                    (id,))
                return
            cur.execute(self._q(
                'update {s}.insights set enriched_at = now(),'
                ' prompt_version = %s where id = %s'),
                (prompt_version, id))

    def get_pending_link_ids(self, *, limit: int) -> list[Id]:
        sql = self._q("""
select id from {s}.insights
where linked_at is null and deleted_at is null and superseded_by is null
order by created_at asc
limit %s
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (limit,))
            return [r[0] for r in cur.fetchall()]

    def get_active_ids(self) -> list[Id]:
        sql = self._q("""
select id from {s}.insights
where deleted_at is null and superseded_by is null
order by created_at asc
""")
        with self._conn.cursor() as cur:
            cur.execute(sql)
            return [r[0] for r in cur.fetchall()]

    def count_pending_links(self) -> int:
        sql = self._q("""
select count(*) from {s}.insights
where linked_at is null and deleted_at is null and superseded_by is null
""")
        with self._conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def get_unenriched_linked_ids(self, *, limit: int) -> list[Id]:
        sql = self._q("""
select id from {s}.insights
where enriched_at is null
  and linked_at is not null
  and deleted_at is null and superseded_by is null
order by created_at asc
limit %s
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (limit,))
            return [r[0] for r in cur.fetchall()]

    def count_unenriched_linked(self) -> int:
        sql = self._q("""
select count(*) from {s}.insights
where enriched_at is null and linked_at is not null
  and deleted_at is null and superseded_by is null
""")
        with self._conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def iter_stale_insight_ids(self, active_pv: str) -> list[Id]:
        sql = self._q("""
select id from {s}.insights
where deleted_at is null and superseded_by is null
  and prompt_version is not null
  and prompt_version != %s
order by created_at asc
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (active_pv,))
            return [r[0] for r in cur.fetchall()]

    def count_stale_insights(self, active_pv: str) -> int:
        sql = self._q("""
select count(*) from {s}.insights
where deleted_at is null and superseded_by is null
  and prompt_version is not null
  and prompt_version != %s
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (active_pv,))
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def reset_for_rebuild(self, ids: list[Id]) -> None:
        if not ids:
            return
        sql = self._q("""
update {s}.insights
set enriched_at = null, linked_at = null
where id = any(%s)
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (ids,))

    def clear_linked_at(self) -> None:
        sql = self._q("""
update {s}.insights
set linked_at = null
where deleted_at is null and superseded_by is null
""")
        with self._conn.cursor() as cur:
            cur.execute(sql)

    def _bulk_update_embedding(
            self, rows: list[tuple[Id, list[float], str]]) -> None:
        """Update embeddings in chunks of <= 1000 rows. Postgres-only.

        Under autocommit=True each `executemany` is its own implicit
        transaction, keeping WAL bloat bounded and preventing a single
        long-running statement from holding row-level locks for
        unrelated readers. Private (underscored) so callers must
        explicitly isinstance-guard the backend; not part of the
        cross-backend `Backend` Protocol surface.
        """
        if not rows:
            return
        sql = self._q("""
update {s}.insights
set embedding = %s::vector,
    embedding_model = %s,
    updated_at = now()
where id = %s
""")
        chunk = 1000
        for start in range(0, len(rows), chunk):
            batch = rows[start:start + chunk]
            with self._conn.cursor() as cur:
                cur.executemany(
                    sql,
                    [(vec, model, eid) for eid, vec, model in batch])


class PostgresEdgeStore(EdgeStore):
    """EdgeStore implementation against a per-store Postgres schema."""

    def __init__(
            self, conn: psycopg.Connection, schema: str) -> None:
        self._conn = conn
        self._schema = schema

    def _q(self, sql: str) -> str:
        return sql.format(s=self._schema)

    def upsert(self, edge: Edge) -> None:
        import json as _json
        sql = self._q("""
insert into {s}.edges
    (source_id, target_id, edge_type, weight, metadata, created_at)
values (%s, %s, %s, %s, %s::jsonb, coalesce(%s, now()))
on conflict (source_id, target_id, edge_type) do update set
    metadata = case
        when {s}.edges.metadata->>'created_by' in ('claude', 'manual')
            then {s}.edges.metadata
        when excluded.weight >= {s}.edges.weight
            then excluded.metadata
        else {s}.edges.metadata
    end,
    weight = greatest({s}.edges.weight, excluded.weight)
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (
                edge.source_id, edge.target_id, edge.edge_type,
                edge.weight, _json.dumps(edge.metadata or {}),
                edge.created_at))

    def by_node(self, node_id: Id) -> list[Edge]:
        sql = self._q("""
select source_id, target_id, edge_type, weight, metadata, created_at
from {s}.edges
where source_id = %s
union all
select source_id, target_id, edge_type, weight, metadata, created_at
from {s}.edges
where target_id = %s and source_id <> %s
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (node_id, node_id, node_id))
            return [_row_to_edge(r) for r in cur.fetchall()]

    def by_node_and_type(
            self, node_id: Id, edge_type: str) -> list[Edge]:
        sql = self._q("""
select source_id, target_id, edge_type, weight, metadata, created_at
from {s}.edges
where source_id = %s and edge_type = %s
union all
select source_id, target_id, edge_type, weight, metadata, created_at
from {s}.edges
where target_id = %s and edge_type = %s and source_id <> %s
""")
        with self._conn.cursor() as cur:
            cur.execute(
                sql, (node_id, edge_type, node_id, edge_type, node_id))
            return [_row_to_edge(r) for r in cur.fetchall()]

    def by_source_and_type(
            self, source_id: Id, edge_type: str) -> list[Edge]:
        sql = self._q("""
select source_id, target_id, edge_type, weight, metadata, created_at
from {s}.edges
where source_id = %s and edge_type = %s
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (source_id, edge_type))
            return [_row_to_edge(r) for r in cur.fetchall()]

    def find_with_entity(
            self, entity: str, *, exclude_id: Id,
            limit: int) -> list[Id]:
        ent = entity.strip().lower()
        sql = self._q("""
select distinct i.id
from {s}.insights i, jsonb_array_elements_text(i.entities) je
where i.deleted_at is null and i.superseded_by is null
  and i.id <> %s
  and lower(trim(je)) = %s
order by i.id
limit %s
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (exclude_id, ent, limit))
            return [r[0] for r in cur.fetchall()]

    def count_with_entity(
            self, entity: str, *, exclude_id: Id) -> int:
        ent = entity.strip().lower()
        sql = self._q("""
select count(distinct i.id)
from {s}.insights i, jsonb_array_elements_text(i.entities) je
where i.deleted_at is null and i.superseded_by is null
  and i.id <> %s
  and lower(trim(je)) = %s
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (exclude_id, ent))
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def all(self) -> list[Edge]:
        sql = self._q("""
select source_id, target_id, edge_type, weight, metadata, created_at
from {s}.edges
""")
        with self._conn.cursor() as cur:
            cur.execute(sql)
            return [_row_to_edge(r) for r in cur.fetchall()]

    def adjacency(self) -> dict[Id, list[tuple[Id, str, float]]]:
        sql = self._q("""
select source_id, target_id, edge_type, weight
from {s}.edges
""")
        adjacency: dict[Id, list[tuple[Id, str, float]]] = {}
        with self._conn.cursor() as cur:
            cur.execute(sql)
            for source_id, target_id, edge_type, weight in cur:
                # `edges.weight` is nullable; the column default only
                # fires when an INSERT omits it. `_row_to_edge` coerced
                # a NULL to 1.0 and traversal relied on that.
                adjacency.setdefault(source_id, []).append(
                    (target_id, edge_type,
                     1.0 if weight is None else float(weight)))
        return adjacency

    def delete_by_node(self, node_id: Id) -> None:
        sql = self._q("""
delete from {s}.edges
where source_id = %s or target_id = %s
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (node_id, node_id))

    def delete_auto_for_node(
            self, node_id: Id, edge_type: str) -> None:
        filt = _PER_NODE_CREATED_BY_FILTER[edge_type]
        sql = self._q(f"""
delete from {{s}}.edges
where (source_id = %s or target_id = %s)
  and edge_type = %s
  and {filt}
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (node_id, node_id, edge_type))

    def delete_auto_by_type(self, edge_type: str) -> None:
        filt = _REINDEX_CREATED_BY_FILTER[edge_type]
        sql = self._q(f"""
delete from {{s}}.edges
where edge_type = %s and {filt}
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (edge_type,))

    def count_auto_by_type(self, edge_type: str) -> int:
        filt = _REINDEX_CREATED_BY_FILTER[edge_type]
        sql = self._q(f"""
select count(*) from {{s}}.edges
where edge_type = %s and {filt}
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (edge_type,))
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def delete_low_weight_temporal_proximity(
            self, *, min_weight: float) -> None:
        sql = self._q("""
delete from {s}.edges
where edge_type = 'temporal'
  and metadata->>'sub_type' = 'proximity'
  and weight < %s
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (min_weight,))

    def count_low_weight_temporal_proximity(
            self, *, min_weight: float) -> int:
        sql = self._q("""
select count(*) from {s}.edges
where edge_type = 'temporal'
  and metadata->>'sub_type' = 'proximity'
  and weight < %s
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (min_weight,))
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def get_weight(
            self, source_id: Id, target_id: Id,
            edge_type: str) -> float | None:
        sql = self._q("""
select weight from {s}.edges
where source_id = %s and target_id = %s and edge_type = %s
""")
        with self._conn.cursor() as cur:
            cur.execute(sql, (source_id, target_id, edge_type))
            row = cur.fetchone()
            return float(row[0]) if row else None

    def count_dangling_by_type(self) -> dict[str, int]:
        sql = self._q("""
select e.edge_type, count(*)
from {s}.edges e
where not exists (
    select 1 from {s}.insights i
    where i.id = e.source_id and i.deleted_at is null and i.superseded_by is null
)
   or not exists (
    select 1 from {s}.insights i
    where i.id = e.target_id and i.deleted_at is null and i.superseded_by is null
)
group by e.edge_type
""")
        with self._conn.cursor() as cur:
            cur.execute(sql)
            return {r[0]: int(r[1]) for r in cur.fetchall()}

    def degree_distribution(self) -> dict[Id, int]:
        ids_sql = self._q("""
select id from {s}.insights
where deleted_at is null and superseded_by is null
""")
        degree_sql = self._q("""
select id, sum(cnt) from (
    select source_id as id, count(*) as cnt
    from {s}.edges
    group by source_id
    union all
    select target_id as id, count(*) as cnt
    from {s}.edges
    group by target_id
) t
group by id
""")
        with self._conn.cursor() as cur:
            cur.execute(ids_sql)
            ids = [r[0] for r in cur.fetchall()]
            if not ids:
                return {}
            cur.execute(degree_sql)
            by_id = {r[0]: int(r[1]) for r in cur.fetchall()}
        return {iid: by_id.get(iid, 0) for iid in ids}

    def get_neighborhood(
            self, seed_id: Id, *, depth: int,
            edge_filter: str = '') -> list[tuple[Id, int, str]]:
        """Bounded BFS via recursive CTE.

        Postgres-native equivalent of the Python deque BFS in
        SqliteEdgeStore. The recursive CTE emits only the bounded
        subgraph (depth <= `depth`); active-node filtering is applied
        in the outer select so deleted nodes do not seed traversal.
        """
        if depth <= 0:
            return []
        edge_filter_join = (
            ' and e.edge_type = %s' if edge_filter else '')
        sql = self._q(f"""
with recursive walk(node_id, hop, via_edge) as (
    select %s::text, 0::int, null::text
    union
    select
        case when e.source_id = w.node_id
             then e.target_id else e.source_id end,
        w.hop + 1,
        e.edge_type
    from walk w
    join {{s}}.edges e
        on (e.source_id = w.node_id or e.target_id = w.node_id)
        {edge_filter_join}
    where w.hop < %s
)
select distinct on (w.node_id) w.node_id, w.hop, w.via_edge
from walk w
join {{s}}.insights i on i.id = w.node_id
where w.hop > 0
  and w.node_id <> %s
  and i.deleted_at is null and i.superseded_by is null
order by w.node_id, w.hop asc
""")
        params: list[Any] = [seed_id]
        if edge_filter:
            params.append(edge_filter)
        params.extend([depth, seed_id])
        with self._conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        triples = [(r[0], int(r[1]), r[2] or '') for r in rows]
        triples.sort(key=lambda t: (t[1], t[0]))
        return triples


class PostgresMetaStore(MetaStore):
    """MetaStore implementation against a per-store Postgres schema."""

    def __init__(
            self, conn: psycopg.Connection, schema: str) -> None:
        self._conn = conn
        self._schema = schema

    def get(self, key: str) -> str | None:
        with self._conn.cursor() as cur:
            cur.execute(
                f'select value from {self._schema}.meta where key = %s',
                (key,))
            row = cur.fetchone()
            return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        sql = f"""
insert into {self._schema}.meta (key, value)
values (%s, %s)
on conflict (key) do update set value = excluded.value
"""
        with self._conn.cursor() as cur:
            cur.execute(sql, (key, value))

    def delete(self, key: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                f'delete from {self._schema}.meta where key = %s',
                (key,))

    def keys(self) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute(f'select key from {self._schema}.meta')
            return [r[0] for r in cur.fetchall()]


class PostgresOplog(Oplog):
    """Oplog implementation: insert-only writes; trim in maintenance."""

    def __init__(
            self, conn: psycopg.Connection, schema: str) -> None:
        self._conn = conn
        self._schema = schema

    def log(
            self, *, operation: str, insight_id: Id,
            detail: str,
            before: dict[str, Any] | None = None,
            after: dict[str, Any] | None = None) -> None:
        before_s = json.dumps(before) if before is not None else None
        after_s = json.dumps(after) if after is not None else None
        sql = f"""
insert into {self._schema}.oplog
       (operation, insight_id, detail, before, after)
values (%s, %s, %s, %s::jsonb, %s::jsonb)
"""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    sql,
                    (operation, insight_id, detail,
                     before_s, after_s))
        except Exception as exc:
            logger.warning(f'oplog insert failed: {exc}')

    def maintenance_step(self) -> None:
        sql = f"""
delete from {self._schema}.oplog
where id <= (select max(id) from {self._schema}.oplog) - %s
"""
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, (_MAX_OPLOG_ENTRIES,))
        except Exception as exc:
            logger.warning(f'oplog cap trim failed: {exc}')

    def trim_by_age(self, *, retention_days: int = 180) -> int:
        sql = f"""
delete from {self._schema}.oplog
where created_at < now() - (%s * interval '1 day')
"""
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, (retention_days,))
                return int(cur.rowcount or 0)
        except Exception as exc:
            logger.warning(f'oplog age trim failed: {exc}')
            return 0

    def recent(
            self, *, limit: int = 20,
            since: str = '') -> list[OpLogEntry]:
        if since:
            since_dt = parse_timestamp(since)
            sql = f"""
select id, operation, insight_id, detail, created_at,
       before, after
from {self._schema}.oplog
where created_at >= %s
order by id desc
limit %s
"""
            with self._conn.cursor() as cur:
                cur.execute(sql, (since_dt, limit))
                rows = cur.fetchall()
        else:
            sql = f"""
select id, operation, insight_id, detail, created_at,
       before, after
from {self._schema}.oplog
order by id desc
limit %s
"""
            with self._conn.cursor() as cur:
                cur.execute(sql, (limit,))
                rows = cur.fetchall()
        return [
            OpLogEntry(
                id=int(r[0]), operation=r[1],
                insight_id=r[2] or '', detail=r[3] or '',
                created_at=_datetime_or_none(r[4])
                or datetime.now(timezone.utc),
                before=r[5], after=r[6])
            for r in rows
            ]

    def stats(self, *, since: str = '') -> OpLogStats:
        op_counts: dict[str, int] = {}
        with self._conn.cursor() as cur:
            if since:
                since_dt = parse_timestamp(since)
                sql = f"""
select operation, count(*)
from {self._schema}.oplog
where created_at >= %s
group by operation
order by count(*) desc
"""
                cur.execute(sql, (since_dt,))
            else:
                sql = f"""
select operation, count(*)
from {self._schema}.oplog
group by operation
order by count(*) desc
"""
                cur.execute(sql)
            for op, cnt in cur.fetchall():
                op_counts[op] = int(cnt)
            never_sql = f"""
select count(*) from {self._schema}.insights
where deleted_at is null and superseded_by is null and access_count = 0
"""
            cur.execute(never_sql)
            never = int(cur.fetchone()[0])
            total_sql = f"""
select count(*) from {self._schema}.insights
where deleted_at is null and superseded_by is null
"""
            cur.execute(total_sql)
            total = int(cur.fetchone()[0])
        return OpLogStats(
            operation_counts=op_counts, never_accessed=never,
            total_active=total)

    def delta_coverage(self) -> tuple[int, int]:
        sql = f"""
select count(*),
       count(*) filter (where before is not null
                          or after is not null)
from {self._schema}.oplog
"""
        with self._conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
        if row is None:
            return (0, 0)
        return (int(row[0] or 0), int(row[1] or 0))


class PostgresRecallSession(RecallSession):
    """Read-side session bound to a dedicated autocommit connection.

    Owns its own connection (separate from the parent Backend's
    connection) so the session's `search_path` does not leak into
    write traffic, and so the connection can be borrowed from a pool
    without collision.

    On `__exit__` the session resets `search_path` to the default
    (`"$user", public`) before the connection is closed -- so no
    session state leaks when the connection is returned to a pool.

    Vector work stays server-side: `vector_anchors` rides the HNSW
    index, and `similarities` scores with `embedding <=>` so the
    pipeline receives N scalars instead of N x dim floats.
    """

    def __init__(
            self, dsn: str, schema: str,
            *, owns_conn: bool = True) -> None:
        self._dsn = dsn
        self._schema = schema
        self._owns_conn = owns_conn
        self._conn: psycopg.Connection | None = None

    def __enter__(self) -> Self:
        self._conn = _open_connection(self._dsn, autocommit=True)
        with self._conn.cursor() as cur:
            cur.execute(
                f'set search_path = {self._schema}, public')
        return self

    def __exit__(self, *exc: object) -> None:
        if self._conn is not None:
            try:
                with self._conn.cursor() as cur:
                    cur.execute('set search_path = "$user", public')
            except Exception as e:
                logger.warning(f'search_path reset failed: {e}')
            if self._owns_conn:
                import psycopg as _psycopg
                try:
                    self._conn.close()
                except _psycopg.Error as exc:
                    logger.debug(
                        f'pg connection close failed: {exc}')
            self._conn = None

    def close(self) -> None:
        self.__exit__(None, None, None)

    def vector_anchors(
            self, query_vec: list[float], *, k: int = 10,
            category: str = '', source: str = '') -> list[tuple[Id, float]]:
        """Return top-k (id, similarity) matches via HNSW.

        Similarity is `1 - (embedding <=> :q)` (cosine in (0, 1],
        higher is better). `category` / `source` filter in SQL before
        the limit so the top-k cut is taken over eligible rows only.

        Notes
        -----
        - `hnsw.ef_search` is raised to `max(40, 4 * k)` for this
          query. HNSW is approximate, so a search width close to `k`
          returns a top-k that is merely near the true one: measured
          against an exact SQL oracle over 60 real queries at `k=30`,
          pgvector's default width of 40 (1.33x `k`) returned
          `recall@30 = 0.9744`, exact on only 35 of 60, and missed
          rows scoring as high as 0.4972. At 3.3x it was exactly
          1.0000 on 60 of 60, and widening further changed nothing.
        - The width scales with `k` rather than being pinned, so a
          larger anchor budget widens the search with it. The floor
          is pgvector's own default, so this can never search
          narrower than the library would.
        - It must be set on THIS connection. The session opens its
          own (`__enter__`), so a width set on the parent Backend's
          connection never reaches the query that needs it.
        """
        assert self._conn is not None
        sql = f"""
select id, 1 - (embedding <=> %s::vector) as sim
from {self._schema}.insights
where deleted_at is null and superseded_by is null and embedding is not null
  and (%s = '' or category = %s)
  and (%s = '' or source = %s)
order by embedding <=> %s::vector
limit %s
"""
        with self._conn.cursor() as cur:
            cur.execute(f'set hnsw.ef_search = {max(40, 4 * int(k))}')
            cur.execute(sql, (
                query_vec, category, category, source, source,
                query_vec, k))
            return [
                (r[0], float(r[1])) for r in cur.fetchall()
                if r[1] is not None and float(r[1]) > 0.0
                ]

    def similarities(
            self, query_vec: list[float]) -> dict[Id, float]:
        """Cosine per id, positives only, computed in the database.

        Notes
        -----
        - Returns one float per row rather than the embedding itself,
          which is the whole point: the pipeline needs N scalars to
          score with, and shipping N x dim floats to compute them was
          costing a full whole-store pull per recall.
        - Whole-store by design: `beam_search_from_anchor` reads the
          result DURING traversal, so narrowing it to the visited set
          would change traversal scoring rather than just save work.
        """
        assert self._conn is not None
        sql = f"""
select id, 1 - (embedding <=> %s::vector) as sim
from {self._schema}.insights
where deleted_at is null and superseded_by is null and embedding is not null
"""
        with self._conn.cursor() as cur:
            cur.execute(sql, (query_vec,))
            return {
                r[0]: float(r[1]) for r in cur
                if r[1] is not None and float(r[1]) > 0.0
                }

    def keyword_counts(
            self, query_tokens: set[str]) -> dict[Id, int]:
        """Match count per active insight id, computed in the database.

        See the Protocol docstring for the contract.

        Notes
        -----
        - `kw_tokens` holds the row's distinct tokens as
          `keyword.insight_tokens` produced them at write time, so
          the count matches the Python route exactly rather than
          approximately. Nothing here re-derives the tokenizer: the
          earlier SQL form re-expressed `_WORD_RE` as
          `regexp_split_to_array(lower(...), '[^a-z0-9]+')` on every
          row of every recall, which cost 0.54 ms per active row and
          again per matched row, 75% of recall on the largest store.
        - Stopword filtering on the row side cannot change the
          count, because `query_tokens` is stopword-filtered too and
          `intersect` only ever sees tokens present in both. Checked
          against the SQL form over 1,443 rows and all 1,849 distinct
          logged queries of one store: the two counts never differed.
        - `kw_tokens && query` is exactly `matched > 0`, so the GIN
          index answers the filter and the intersect runs only on
          rows that can contribute. Unnesting the STORED array beats
          walking the query against it with `= any` above four query
          tokens, 49 ms against 124 at twelve.
        """
        if not query_tokens:
            return {}
        assert self._conn is not None
        sql = f"""
select i.id, cardinality(array(
        select unnest(%(q)s::text[])
        intersect
        select unnest(i.kw_tokens)
        )) as matched
from {self._schema}.insights i
where i.deleted_at is null and i.superseded_by is null and i.kw_tokens && %(q)s::text[]
"""
        with self._conn.cursor() as cur:
            cur.execute(sql, {'q': sorted(query_tokens)})
            return {r[0]: int(r[1]) for r in cur}

    def vectors_for_ids(
            self, ids: list[Id]) -> dict[Id, list[float]]:
        """Embeddings for a bounded set of ids."""
        if not ids:
            return {}
        assert self._conn is not None
        sql = f"""
select id, embedding
from {self._schema}.insights
where deleted_at is null and superseded_by is null and embedding is not null
  and id = any(%s::text[])
"""
        with self._conn.cursor() as cur:
            cur.execute(sql, (list(ids),))
            return {
                r[0]: pgvector_to_list(r[1]) for r in cur
                if r[1] is not None
                }


class PostgresBackend(Backend):
    """Per-store Postgres backend: schema-bound connection + sub-stores.

    Single primary connection per backend. `transaction()` uses
    psycopg's nested transaction (BEGIN / SAVEPOINT). `recall_session`
    and `readonly_context` open dedicated autocommit connections so
    long reads don't share a connection with active writes.
    """

    nodes: PostgresNodeStore
    edges: PostgresEdgeStore
    meta: PostgresMetaStore
    oplog: PostgresOplog

    def __init__(
            self, dsn: str, store: str,
            *, conn: psycopg.Connection | None = None,
            owns_conn: bool = True) -> None:
        self._dsn = dsn
        self._store = store
        self._schema = _store_schema(store)
        self._owns_conn = owns_conn
        self._conn = conn if conn is not None else _open_connection(
            dsn, autocommit=True)
        with self._conn.cursor() as cur:
            cur.execute(f'set search_path = {self._schema}, public')
        self.nodes = PostgresNodeStore(self._conn, self._schema)
        self.edges = PostgresEdgeStore(self._conn, self._schema)
        self.meta = PostgresMetaStore(self._conn, self._schema)
        self.oplog = PostgresOplog(self._conn, self._schema)

    @property
    def path(self) -> str:
        """DSN+schema identifier (Postgres has no filesystem path)."""
        from memman.trace import redact_dsn
        return f'{redact_dsn(self._dsn)}#{self._schema}'

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run a block in a write transaction.

        Nested calls reuse the outer transaction via SAVEPOINT (psycopg
        emits one when `conn.transaction()` is entered while already in
        a transaction).
        """
        with self._conn.transaction():
            yield

    @contextmanager
    def write_lock(self, name: str) -> Iterator[None]:
        """Acquire a per-store transaction-scoped advisory lock.

        Postgres: `pg_advisory_xact_lock`. Must be called inside an
        active transaction; the lock auto-releases on transaction
        commit/rollback. Reentrant: the same session may acquire the
        same key multiple times safely (used by the nested
        `apply_all` write pattern).
        """
        key = _advisory_lock_key(self._schema, name)
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute('select pg_advisory_xact_lock(%s)', (key,))
            yield

    @contextmanager
    def readonly_context(self) -> Iterator[PostgresBackend]:
        """Yield a Backend bound to a separate autocommit connection.

        Postgres autocommit lets reader threads see commits from the
        main backend connection as they land. The per-call connection
        is closed deterministically on context exit.
        """
        ro_conn = _open_connection(self._dsn, autocommit=True)
        ro = PostgresBackend(
            self._dsn, self._store, conn=ro_conn, owns_conn=False)
        try:
            yield ro
        finally:
            try:
                ro_conn.close()
            except Exception:
                pass

    @contextmanager
    def recall_session(self) -> Iterator[PostgresRecallSession]:
        """Yield a PostgresRecallSession for one recall request."""
        session = PostgresRecallSession(self._dsn, self._schema)
        with session:
            yield session

    @contextmanager
    def reembed_lock(self, name: str) -> Iterator[bool]:
        """Acquire a per-store session-scoped advisory sweep lock.

        Mirrors `drain_lock`: dedicated `psycopg.connect()` outside
        any pool, autocommit, with `keepalives_idle=30`. Uses
        `pg_try_advisory_lock` (non-blocking) so a second sweep
        agent fails fast with `False` instead of waiting hours.
        Released on connection close (intended crash-recovery
        mechanism). Wrong primitive for `reindex_auto_edges`,
        which wants `write_lock`'s transaction-scoped variant.
        """
        key = _advisory_lock_key(self._schema, f'reembed:{name}')
        conn = _open_connection(
            self._dsn, autocommit=True, keepalives=True)
        acquired = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    'select pg_try_advisory_lock(%s)', (key,))
                row = cur.fetchone()
                acquired = bool(row[0]) if row else False
            yield acquired
        finally:
            try:
                if acquired:
                    with conn.cursor() as cur:
                        cur.execute(
                            'select pg_advisory_unlock(%s)', (key,))
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    @contextmanager
    def swap_lock(self) -> Iterator[bool]:
        """Acquire a per-store session-scoped advisory swap lock.

        Mirrors `reembed_lock` but with the dedicated key
        `embed_swap:<schema>` so swaps and reembeds do not contend
        for the same lock. Held continuously across multi-step
        orchestration (swap_prepare -> backfill -> cutover) since
        a swap may span minutes-to-hours and crosses CLI invocations
        on resume. Auto-releases on connection close, surviving
        process crash.
        """
        key = _advisory_lock_key(self._schema, 'embed_swap')
        conn = _open_connection(
            self._dsn, autocommit=True, keepalives=True)
        acquired = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    'select pg_try_advisory_lock(%s)', (key,))
                row = cur.fetchone()
                acquired = bool(row[0]) if row else False
            yield acquired
        finally:
            try:
                if acquired:
                    with conn.cursor() as cur:
                        cur.execute(
                            'select pg_advisory_unlock(%s)', (key,))
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def swap_prepare(self, target_dim: int) -> None:
        """Add the pending vector column and its HNSW index."""
        _check_pg_version(self._dsn)
        _swap_prepare_pg(self._dsn, self._schema, int(target_dim))

    def iter_for_swap(
            self, cursor: str, batch: int) -> list[tuple[str, str]]:
        sql = (
            f'select id, content from {self._schema}.insights'
            f' where deleted_at is null and superseded_by is null'
            f'   and embedding_pending is null'
            f'   and id > %s'
            f' order by id limit %s')
        with self._conn.cursor() as cur:
            cur.execute(sql, (cursor, int(batch)))
            return [(r[0], r[1]) for r in cur.fetchall()]

    def write_swap_batch(
            self, items: list[tuple[str, list[float]]]) -> None:
        if not items:
            return
        sql = (
            f'update {self._schema}.insights'
            f' set embedding_pending = %s::vector'
            f' where id = %s')
        with self._conn.cursor() as cur:
            cur.executemany(
                sql, [(vec, rid) for (rid, vec) in items])

    def swap_cutover(self, target: Fingerprint) -> None:
        _swap_cutover_pg(self._dsn, self._schema)

    def swap_abort(self) -> None:
        _swap_abort_pg(self._dsn, self._schema)

    @contextmanager
    def drain_lock(
            self, store: str | None = None) -> Iterator[bool]:
        """Acquire a per-store advisory drain lock on a dedicated conn.

        Opens a NEW connection outside any pool (psycopg.connect()
        directly) with `keepalives_idle=30` so a hung worker is
        detected by the kernel rather than holding the lock
        indefinitely. The lock auto-releases when the connection
        closes -- the intended crash-recovery mechanism.

        Yields True when the lock was acquired, False otherwise.
        """
        target = store or self._store
        key = _lock_id(f'memman_drain:{target}')
        conn = _open_connection(
            self._dsn, autocommit=True, keepalives=True)
        acquired = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    'select pg_try_advisory_lock(%s)', (key,))
                row = cur.fetchone()
                acquired = bool(row[0]) if row else False
            yield acquired
        finally:
            try:
                if acquired:
                    with conn.cursor() as cur:
                        cur.execute(
                            'select pg_advisory_unlock(%s)', (key,))
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def storage_summary(self) -> dict[str, Any]:
        sizes: dict[str, Any] = {}
        try:
            with self._conn.cursor() as cur:
                for table in ('insights', 'edges', 'oplog', 'meta'):
                    cur.execute(
                        'select pg_relation_size(%s::regclass)',
                        (f'{self._schema}.{table}',))
                    row = cur.fetchone()
                    sizes[f'{table}_bytes'] = (
                        int(row[0]) if row else 0)
        except Exception as exc:
            logger.warning(f'pg_relation_size failed: {exc}')
        sizes['schema'] = self._schema
        return sizes

    def maintenance_step(self) -> None:
        """Run per-store maintenance: trim oplog cap.

        Autovacuum handles vacuuming on Postgres; no pragma needed.
        """
        self.oplog.maintenance_step()

    def integrity_check(self) -> dict[str, Any]:
        with self._conn.cursor() as cur:
            cur.execute(
                f'select 1 from {self._schema}.insights limit 1')
            cur.fetchone()
        return {'ok': True, 'detail': 'schema reachable'}

    def introspect_columns(self, table: str) -> set[str]:
        _check_identifier(table)
        sql = """
select column_name from information_schema.columns
where table_schema = %s and table_name = %s
"""
        with self._conn.cursor() as cur:
            cur.execute(sql, (self._schema, table))
            return {row[0] for row in cur.fetchall()}

    def introspect_index_definitions(self, table: str) -> dict[str, str]:
        _check_identifier(table)
        sql = """
select indexname, indexdef from pg_indexes
where schemaname = %s and tablename = %s
"""
        with self._conn.cursor() as cur:
            cur.execute(sql, (self._schema, table))
            return {row[0]: row[1] for row in cur.fetchall()}

    def start_run(self) -> int | None:
        """Insert a per-store `worker_runs` row, return its id."""
        sql = (
            f'insert into {self._schema}.worker_runs'
            f' (last_heartbeat_at) values (now()) returning id')
        with self._conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            self._conn.commit()
        return int(row[0]) if row else None

    def beat_run(self, run_id: int | None) -> None:
        """Advance `last_heartbeat_at = now()` on the per-store run row.
        """
        if run_id is None:
            return
        sql = (
            f'update {self._schema}.worker_runs'
            f' set last_heartbeat_at = now() where id = %s')
        with self._conn.cursor() as cur:
            cur.execute(sql, (run_id,))
            self._conn.commit()

    def finish_run(self, run_id: int | None) -> None:
        """Stamp `ended_at = now()` on the per-store run row."""
        if run_id is None:
            return
        sql = (
            f'update {self._schema}.worker_runs'
            f' set ended_at = now() where id = %s')
        with self._conn.cursor() as cur:
            cur.execute(sql, (run_id,))
            self._conn.commit()

    def recent_runs(self, *, limit: int) -> list[WorkerRun]:
        """Return the per-store recent `worker_runs` rows (newest first)."""
        sql = (
            f'select id, started_at, ended_at, rows_processed, error,'
            f' last_heartbeat_at'
            f' from {self._schema}.worker_runs'
            f' order by id desc limit %s')
        with self._conn.cursor() as cur:
            cur.execute(sql, (limit,))
            rows = cur.fetchall()
        return [
            WorkerRun(
                id=int(r[0]),
                started_at=_datetime_or_none(r[1])
                or datetime.now(timezone.utc),
                ended_at=_datetime_or_none(r[2]),
                rows_processed=int(r[3] or 0),
                error=r[4] or '',
                last_heartbeat_at=_datetime_or_none(r[5]))
            for r in rows
            ]

    def close(self) -> None:
        if self._owns_conn and self._conn is not None:
            import psycopg as _psycopg
            try:
                self._conn.close()
            except _psycopg.Error as exc:
                logger.debug(f'pg connection close failed: {exc}')

    def __enter__(self) -> Self:
        return self

    def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None) -> None:
        self.close()


def _resolve_active_dim(expected_dim: int | None = None) -> int:
    """Return the embedding dim to bake into a fresh schema.

    When `expected_dim` is provided (e.g. read from `meta.embed_fingerprint`
    on a pre-existing schema), it is used directly without consulting
    the env-bound active client. This breaks the chicken-and-egg where
    `open_postgres_backend` needs a dim to ensure the baseline schema
    but the env active client may not be the right one for a store
    whose stored fingerprint differs.

    Falls back to the active fingerprint, then `EMBEDDING_DIM`.
    """
    if expected_dim is not None and expected_dim > 0:
        return int(expected_dim)
    try:
        from memman.embed.fingerprint import seed_default_fingerprint
        from memman.exceptions import ConfigError as RuntimeConfigError
        active = seed_default_fingerprint()
        if active.dim > 0:
            return int(active.dim)
    except (RuntimeConfigError, ImportError) as exc:
        logger.warning(
            f'active fingerprint resolution failed; '
            f'using {EMBEDDING_DIM}-dim default: {exc}')
    return EMBEDDING_DIM


def _read_stored_dim(dsn: str, store: str) -> int | None:
    """Best-effort read of the stored fingerprint dim for `store`.

    Returns None when the schema does not exist yet (UndefinedTable),
    when the meta row is absent, or when the value is unparseable.
    Connection failures (network/auth) propagate so a transient
    outage doesn't masquerade as a fresh schema and silently force a
    fingerprint mismatch on the next assert.
    """
    import psycopg

    schema = _store_schema(store)
    sql = f"select value from {schema}.meta where key = 'embed_fingerprint'"
    # The handler sits at the statement, not around the block: it
    # runs before `_connection` translates, so a missing schema stays
    # a None result while every other driver error still becomes a
    # BackendError.
    with _connection(dsn, autocommit=True) as conn, \
            conn.cursor() as cur:
        try:
            cur.execute(sql)
        except psycopg.errors.UndefinedTable:
            return None
        row = cur.fetchone()
    if row is None or not row[0]:
        return None
    try:
        return int(json.loads(row[0]).get('dim') or 0) or None
    except Exception:
        return None


def open_postgres_backend(
        store: str, dsn: str, *,
        read_only: bool = False) -> PostgresBackend:
    """Open or create the per-store Postgres backend at `dsn`.

    Reads the store's stored fingerprint dim (if any) before
    `_ensure_baseline_schema`, so a freshly discovered Postgres-backed
    store opens at its own dim rather than the env active client's.
    """
    stored = _read_stored_dim(dsn, store)
    target_dim = _resolve_active_dim(expected_dim=stored)
    _ensure_baseline_schema(dsn, store, dim=target_dim)
    _assert_vector_dim_matches(dsn, store, target_dim)
    if read_only:
        ro_conn = _open_connection(dsn, autocommit=True)
        return PostgresBackend(
            dsn, store, conn=ro_conn, owns_conn=True)
    backend = PostgresBackend(dsn, store)
    try:
        _ensure_hnsw_index(dsn, _store_schema(store))
    except Exception as exc:
        logger.warning(f'HNSW index ensure failed: {exc}')
    return backend


def drop_postgres_store(store: str, dsn: str) -> None:
    """Drop the per-store schema at `dsn`.

    Queue rows are not purged here: the queue is SQLite under the
    per-store routing model and `factory.drop_store` calls
    `queue.purge_store` separately.
    """
    schema = _store_schema(store)
    with _connection(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f'drop schema if exists {schema} cascade')


def apply_baseline_schema(
        conn: Any, schema: str, dim: int) -> None:
    """Apply the baseline DDL on an open connection (idempotent).

    Caller controls the transaction. Used both by
    `_ensure_baseline_schema` (autocommit, store-open path) and by
    the migrator (in-transaction so DDL rolls back on import
    failure). Creates the `vector` extension, the schema, and the
    base tables (with all constraints declared inline).
    """
    import psycopg  # optional-extra: lazy like every psycopg use here
    with conn.cursor() as cur:
        cur.execute('create extension if not exists vector')
        cur.execute(f'create schema if not exists {schema}')
        try:
            cur.execute(
                PG_BASELINE_SCHEMA.format(schema=schema, dim=dim))
        except psycopg.errors.UndefinedColumn as exc:
            # Mirrors the SQLite diagnostic in store/db.py::_migrate:
            # `create table if not exists` no-ops on an existing
            # table, so a pre-migration store trips the baseline's
            # index on the newest column.
            raise BackendError(
                f'postgres schema {schema} predates the current'
                f' schema ({exc}); add the missing column to the'
                ' live schema and drop its stale partial indexes,'
                ' then reopen') from exc


def _ensure_baseline_schema(
        dsn: str, store: str, *, dim: int = EMBEDDING_DIM) -> None:
    """Create the schema and apply baseline DDL idempotently.

    `dim` is the embedding dimension to bake into `vector(N)` for
    new schemas. Resolved from `seed_default_fingerprint().dim` by
    `open_postgres_backend` so a non-Voyage operator (e.g. openai
    1536) gets a correctly-sized column on first deploy. For
    existing schemas the call is idempotent: `create table if not
    exists` does not alter the existing column width, and the
    open-time guard at `_assert_vector_dim_matches` refuses the
    open if the stored width differs from `dim`.
    """
    schema = _store_schema(store)
    with _connection(dsn, autocommit=True) as conn:
        apply_baseline_schema(conn, schema, dim)


def _assert_vector_dim_matches(
        dsn: str, store: str, expected_dim: int) -> None:
    """Refuse to open if the stored `vector(N)` column width differs.

    pgvector stores `N` directly in `pg_attribute.atttypmod` (no
    VARHDRSZ offset, unlike standard varlena types). Querying via
    the conventional `information_schema.columns` does not work
    because pgvector extension types do not populate
    `character_maximum_length`.

    Raises `BackendError` with an upgrade hint when the operator's
    active embedding fingerprint dim differs from the stored column
    width. This is the parallel of the schema-version skew refusal
    for embedding-dim skew.

    Swap-aware: if `meta.embed_swap_state` is `backfilling` or
    `cutover`, also accepts the dim of the in-flight
    `embedding_pending` column. This lets a process open the store
    mid-swap without crashing while the backfill completes.
    """
    schema = _store_schema(store)
    dim_sql = """
select attname, atttypmod from pg_attribute
where attrelid = (%s || '.insights')::regclass
  and attname in ('embedding', 'embedding_pending')
  and not attisdropped
"""
    state_sql = (
        f"select value from {schema}.meta"
        f" where key = 'embed_swap_state'")
    with _connection(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(dim_sql, (schema,))
        rows = cur.fetchall()
        try:
            cur.execute(state_sql)
            state_row = cur.fetchone()
        except Exception as exc:
            logger.debug(
                f'meta.embed_swap_state read failed on {schema}:'
                f' {type(exc).__name__}: {exc}')
            state_row = None
    dims = {r[0]: int(r[1]) for r in rows if r[1] is not None
            and int(r[1]) > 0}
    if not dims:
        return
    swap_state = (state_row[0] if state_row else '') or ''
    swap_active = swap_state in {'backfilling', 'cutover'}
    accepted = {dims['embedding']} if 'embedding' in dims else set()
    if swap_active and 'embedding_pending' in dims:
        accepted.add(dims['embedding_pending'])
    if expected_dim in accepted:
        return
    stored_dim = dims.get('embedding') or next(iter(dims.values()))
    raise BackendError(
        f'store {store!r} has vector({stored_dim}) but the active'
        f' embedding client produces dim={expected_dim}.'
        f" Run 'memman embed swap --to <model>' to migrate, or"
        f' switch back to a {stored_dim}-dim provider.')


def _check_pg_version(dsn: str) -> None:
    """Refuse if the server is older than Postgres 12.

    `embed swap` relies on `ADD COLUMN vector(N)` being metadata-only
    (PG 11+) and on `CREATE INDEX CONCURRENTLY` semantics that PG 12
    cleaned up. Operators on older versions are pointed at the offline
    `memman embed reembed` fallback.
    """
    with _connection(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute('show server_version_num')
        row = cur.fetchone()
        if row is None:
            return
        version_num = int(row[0])
    if version_num < 120000:
        raise BackendError(
            f'Postgres {version_num // 10000} is below the swap'
            ' minimum (12). Use `memman embed reembed` for offline'
            ' rebuild instead.')


def _swap_index_timeout_s() -> int:
    """Read `MEMMAN_EMBED_SWAP_INDEX_TIMEOUT` (default 0 = unlimited).
    """
    raw = os.environ.get(config.EMBED_SWAP_INDEX_TIMEOUT)
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _swap_pending_index_name(schema: str) -> str:
    """Per-schema name for the in-flight HNSW index on the pending
    column. Renamed to the canonical `idx_insights_hnsw_<schema>`
    during cutover.
    """
    return f'idx_insights_hnsw_pending_{schema}'


def _swap_prepare_pg(
        dsn: str, schema: str, target_dim: int,
        retries: int = 3, retry_sleep_s: float = 1.0) -> None:
    """Add `embedding_pending vector(N)` and build a new HNSW.

    `ADD COLUMN ... IF NOT EXISTS` and `CREATE INDEX CONCURRENTLY ...
    IF NOT EXISTS` make this idempotent on resume. The ADD COLUMN runs
    under `lock_timeout='5s'` with retry to avoid wedging behind
    long-running queries; the index build uses
    `MEMMAN_EMBED_SWAP_INDEX_TIMEOUT` (default 0 = unlimited).
    """
    _check_identifier(schema)
    add_sql = (
        f'alter table {schema}.insights add column if not exists'
        f' embedding_pending vector({int(target_dim)})')
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            with _connection(dsn, autocommit=True) as conn, \
                    conn.cursor() as cur:
                cur.execute("set lock_timeout = '5s'")
                cur.execute(add_sql)
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            if attempt + 1 == retries:
                break
            time.sleep(retry_sleep_s)
    if last_exc is not None:
        raise BackendError(
            f'failed to add embedding_pending column on'
            f' {schema}: {last_exc}')

    index_name = _swap_pending_index_name(schema)
    timeout_s = _swap_index_timeout_s()
    create_idx_sql = (
        f'create index concurrently if not exists {index_name}'
        f' on {schema}.insights using hnsw'
        f' (embedding_pending vector_cosine_ops)'
        f' where deleted_at is null and superseded_by is null')
    with _connection(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"set statement_timeout = '{timeout_s}s'")
        cur.execute(create_idx_sql)


def _swap_cutover_pg(
        dsn: str, schema: str) -> None:
    """Atomic switch from `embedding` to `embedding_pending`.

    Single transaction with `statement_timeout=0`:
      1. Verify count(embedding_pending) >= count(embedding)
         where deleted_at is null and superseded_by is null.
      2. Drop old HNSW index.
      3. Drop column embedding.
      4. Rename embedding_pending -> embedding.
      5. Rename pending HNSW index -> canonical name.

    The orchestrator records cutover state and writes the fingerprint
    around this call.
    """
    _check_identifier(schema)
    canonical_idx = f'idx_insights_hnsw_{schema}'
    pending_idx = _swap_pending_index_name(schema)
    verify_sql = (
        f'select count(*) filter (where embedding is not null),'
        f' count(*) filter (where embedding_pending is not null)'
        f' from {schema}.insights where deleted_at is null and superseded_by is null')
    with _connection(dsn, autocommit=False) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("set local statement_timeout = '0'")
                cur.execute(verify_sql)
                row = cur.fetchone()
                old_count = int(row[0]) if row else 0
                new_count = int(row[1]) if row else 0
                if new_count < old_count:
                    raise BackendError(
                        f'cutover refused: embedding_pending has'
                        f' {new_count} rows but embedding has'
                        f' {old_count}; backfill is incomplete')
                cur.execute(
                    f'drop index if exists'
                    f' {schema}.{canonical_idx} cascade')
                cur.execute(
                    f'alter table {schema}.insights'
                    f' drop column embedding')
                cur.execute(
                    f'alter table {schema}.insights'
                    f' rename column embedding_pending to embedding')
                cur.execute(
                    f'alter index if exists {schema}.{pending_idx}'
                    f' rename to {canonical_idx}')
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _swap_abort_pg(dsn: str, schema: str) -> None:
    """Drop the pending column and any pending HNSW remnant.
    """
    _check_identifier(schema)
    pending_idx = _swap_pending_index_name(schema)
    with _connection(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            f'drop index if exists {schema}.{pending_idx}')
        cur.execute(
            f'alter table {schema}.insights'
            f' drop column if exists embedding_pending')


def _ensure_hnsw_index(dsn: str, schema: str) -> None:
    """Create or recreate the HNSW index on `insights.embedding`.

    1. Query `pg_index.indisvalid` for any prior HNSW index on this
       column; drop it if invalid (an aborted CONCURRENTLY build
       leaves an invalid remnant).
    2. `create index concurrently if not exists` with
       `vector_cosine_ops where deleted_at is null and superseded_by is null`.

    Runs on a dedicated autocommit connection because
    `create index concurrently` cannot run inside a transaction.
    `statement_timeout` is set from `MEMMAN_REINDEX_TIMEOUT` (default
    180 seconds) so a stuck build aborts and the next call's
    invalid-remnant cleanup can recover.
    """
    _check_identifier(schema)
    index_name = f'idx_insights_hnsw_{schema}'
    timeout_s = int(os.environ.get(config.REINDEX_TIMEOUT, '180'))
    inspect_sql = """
select i.indexrelid::regclass::text, i.indisvalid
from pg_index i
join pg_class c on c.oid = i.indexrelid
where c.relname = %s
"""
    create_sql = f"""
create index concurrently if not exists {index_name}
on {schema}.insights
using hnsw (embedding vector_cosine_ops)
where deleted_at is null and superseded_by is null
"""
    with _connection(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"set statement_timeout = '{timeout_s}s'")
        cur.execute(inspect_sql, (index_name,))
        row = cur.fetchone()
        if row and not row[1]:
            logger.warning(
                f'dropping invalid HNSW index {row[0]}')
            cur.execute(f'drop index if exists {row[0]} cascade')
        cur.execute(create_sql)


_POSTGRES_MIGRATOR_FEATURES = BackendFeatures(
    supports_edges=True,
    supports_oplog=True,
    supports_reembed=True,
    supports_drain_heartbeat=True,
    supports_filesystem_artifacts=True,
    supports_dry_run=True,
    accepted_embedding_dtypes=frozenset({'float32', 'float64'}))


class PostgresMigrator(Migrator):
    """Postgres + pgvector implementation of the Migrator surface.

    `gather(store)` reads `store_<store>` schema into a portable
    `MigrationPayload`. `apply(store, payload)` creates the schema
    (idempotently) and inserts rows in one transaction with
    `ON CONFLICT DO NOTHING`. `archive` invokes `pg_dump -Fc` for
    a recoverable filesystem artifact. `drop` issues
    `drop schema cascade`.
    """

    backend_name: ClassVar[str] = 'postgres'
    snapshot_features: ClassVar[BackendFeatures] = _POSTGRES_MIGRATOR_FEATURES

    def __init__(self, data_dir: str, *, dsn: str) -> None:
        self.data_dir = data_dir
        self.dsn = dsn

    def preflight_source(self, store: str) -> None:
        _check_identifier(store)
        schema = _store_schema(store)
        with _connection(self.dsn, autocommit=True) as conn, \
                conn.cursor() as cur:
            cur.execute(
                'select 1 from pg_namespace where nspname = %s',
                (schema,))
            if cur.fetchone() is None:
                raise MigrateError(
                    f'source postgres schema {schema!r} does not'
                    f' exist for store {store!r}')
            cur.execute(
                f"select value from {schema}.meta"
                " where key = 'embed_fingerprint'")
            fp_row = cur.fetchone()
            if fp_row is None or not fp_row[0]:
                raise MigrateError(
                    f'source schema {schema!r} has no'
                    f' meta.embed_fingerprint; run `memman doctor`'
                    f' on the source store before migrating')

    def preflight_target(self, store: str) -> None:
        sanitize_identifier(
            store, max_len=63, allowed_chars=r'[A-Za-z0-9_]')
        _check_identifier(store)
        with _connection(self.dsn, autocommit=True) as conn, \
                conn.cursor() as cur:
            cur.execute('select 1')
            cur.execute(
                "select 1 from pg_extension where extname = 'vector'")
            if cur.fetchone() is None:
                raise MigrateError(
                    'pgvector extension not installed in the'
                    ' target database; run `create extension'
                    ' vector;` as a superuser first')
            cur.execute(
                'select has_database_privilege(current_user,'
                " current_database(), 'CREATE')")
            if not bool(cur.fetchone()[0]):
                raise MigrateError(
                    'current postgres role lacks create schema'
                    ' privilege on the target database')

    def gather(self, store: str) -> MigrationPayload:
        _check_identifier(store)
        schema = _store_schema(store)
        with _connection(self.dsn, autocommit=True) as conn, \
                conn.cursor() as cur:
            cur.execute(
                'select 1 from pg_namespace where nspname = %s',
                (schema,))
            if cur.fetchone() is None:
                raise MigrateError(
                    f'source postgres schema {schema!r} does not'
                    f' exist for store {store!r}')

            cur.execute(f'select key, value from {schema}.meta')
            meta_dict = dict(cur.fetchall())

            fp_str = meta_dict.get('embed_fingerprint')
            if not fp_str:
                raise MigrateError(
                    f'source schema {schema!r} has no'
                    f' meta.embed_fingerprint')
            fingerprint = Fingerprint.from_json(fp_str)

            # `embedding_pending` is the one column the schema adds
            # on demand (the swap path), so it alone is probed.
            cur.execute(
                "select 1 from information_schema.columns"
                " where table_schema = %s"
                " and table_name = 'insights'"
                " and column_name = 'embedding_pending'",
                (schema,))
            has_pending = cur.fetchone() is not None
            pending_select = ', embedding_pending' if has_pending else ''
            cur.execute(f"""
select id, content, category, importance, entities,
       source, access_count, keywords, summary, semantic_facts,
       last_accessed_at, embedding,
       linked_at, enriched_at, created_at, updated_at,
       deleted_at, prompt_version, model_id, embedding_model,
       session_id, queue_uuid, corroboration_count, superseded_by
       {pending_select}
from {schema}.insights
order by id
""")
            insight_rows = cur.fetchall()
            insights: list[MigrateInsight] = []
            pending: list[PendingReembed] = []
            for r in insight_rows:
                emb = list(r[11]) if r[11] is not None else None
                insights.append(MigrateInsight(
                    id=r[0], content=r[1], category=r[2],
                    importance=int(r[3]),
                    entities=list(r[4]) if r[4] is not None else [],
                    source=r[5], access_count=int(r[6]),
                    keywords=(
                        list(r[7]) if r[7] is not None else None),
                    summary=r[8],
                    semantic_facts=(
                        list(r[9]) if r[9] is not None else None),
                    last_accessed_at=r[10],
                    embedding=emb,
                    linked_at=r[12],
                    enriched_at=r[13],
                    created_at=r[14],
                    updated_at=r[15],
                    deleted_at=r[16],
                    prompt_version=r[17], model_id=r[18],
                    embedding_model=r[19],
                    session_id=r[20], queue_uuid=r[21],
                    corroboration_count=int(r[22]),
                    superseded_by=r[23]))
                if has_pending and r[24] is not None:
                    pending.append(PendingReembed(
                        insight_id=r[0], vector=list(r[24])))

            cur.execute(f"""
select source_id, target_id, edge_type, weight,
       metadata, created_at
from {schema}.edges
order by source_id, target_id, edge_type
""")
            edges = [
                MigrateEdge(
                    source_id=e[0], target_id=e[1],
                    edge_type=e[2], weight=float(e[3]),
                    metadata=dict(e[4]) if e[4] else {},
                    created_at=e[5])
                for e in cur.fetchall()]

            cur.execute(f"""
select coalesce(legacy_id, id) as sqlite_id,
       operation, insight_id, detail, created_at,
       before, after, id
from {schema}.oplog
order by sqlite_id
""")
            oplog = [
                MigrateOpLog(
                    id=int(o[7]), operation=o[1],
                    insight_id=o[2], detail=o[3] or '',
                    created_at=o[4],
                    before=dict(o[5]) if o[5] else None,
                    after=dict(o[6]) if o[6] else None,
                    legacy_id=int(o[0]) if o[0] is not None else None)
                for o in cur.fetchall()]

        swap_state = None
        if 'embed_swap_state' in meta_dict:
            try:
                dim = int(meta_dict.get('embed_swap_target_dim', '0'))
            except ValueError:
                dim = 0
            swap_state = SwapState(
                target_provider=meta_dict.get(
                    'embed_swap_target_provider', ''),
                target_model=meta_dict.get(
                    'embed_swap_target_model', ''),
                target_dim=dim,
                cursor=meta_dict.get('embed_swap_cursor') or None,
                started_at=None)

        stripped_meta = {
            k: v for k, v in meta_dict.items()
            if not k.startswith('embed_swap_')}

        return MigrationPayload(
            payload_version=PAYLOAD_VERSION,
            fingerprint=fingerprint,
            embedding_dim=fingerprint.dim,
            embedding_dtype='float32',
            insights=insights,
            edges=edges,
            oplog=oplog,
            embedding_pending=pending,
            swap_state=swap_state,
            meta=stripped_meta)

    def apply(
            self, store: str, payload: MigrationPayload) -> None:
        if payload.payload_version != PAYLOAD_VERSION:
            raise MigrateError(
                f'payload version {payload.payload_version} does not'
                f' match this build ({PAYLOAD_VERSION}); re-gather'
                ' with the matching memman')
        if payload.embedding_dtype not in (
                self.snapshot_features.accepted_embedding_dtypes):
            raise MigrateError(
                f'postgres cannot accept embedding_dtype'
                f' {payload.embedding_dtype!r}; accepted:'
                f' {sorted(self.snapshot_features.accepted_embedding_dtypes)}')

        _check_identifier(store)
        schema = _store_schema(store)
        dim = payload.embedding_dim
        with _connection(self.dsn, autocommit=False) as conn:
            try:
                apply_baseline_schema(conn, schema, dim)

                if payload.insights:
                    insight_rows = []
                    for ins in payload.insights:
                        emb = (
                            [float(x) for x in ins.embedding]
                            if ins.embedding is not None else None)
                        insight_rows.append((
                            ins.id, ins.content, ins.category,
                            ins.importance,
                            json.dumps(ins.entities),
                            ins.source, ins.access_count,
                            json.dumps(ins.keywords)
                            if ins.keywords is not None else None,
                            ins.summary,
                            json.dumps(ins.semantic_facts)
                            if ins.semantic_facts is not None
                            else None,
                            ins.last_accessed_at, emb,
                            ins.linked_at, ins.enriched_at,
                            ins.created_at, ins.updated_at,
                            ins.deleted_at, ins.prompt_version,
                            ins.model_id, ins.embedding_model,
                            ins.session_id, ins.queue_uuid,
                            ins.corroboration_count,
                            [] if ins.deleted_at else sorted(
                                insight_tokens(Insight(
                                    content=ins.content,
                                    entities=list(ins.entities)))),
                            ins.superseded_by))
                    with conn.cursor() as cur:
                        cur.executemany(
                            f'insert into {schema}.insights ('
                            ' id, content, category, importance,'
                            ' entities, source, access_count,'
                            ' keywords, summary, semantic_facts,'
                            ' last_accessed_at, embedding,'
                            ' linked_at, enriched_at, created_at,'
                            ' updated_at, deleted_at,'
                            ' prompt_version, model_id,'
                            ' embedding_model, session_id,'
                            ' queue_uuid, corroboration_count,'
                            ' kw_tokens, superseded_by)'
                            ' values (%s, %s, %s, %s, %s::jsonb,'
                            ' %s, %s, %s::jsonb, %s, %s::jsonb,'
                            ' %s, %s, %s, %s, %s, %s, %s, %s,'
                            ' %s, %s, %s, %s, %s, %s, %s)'
                            ' on conflict (id) do nothing',
                            insight_rows)

                if payload.edges:
                    edge_rows = [(
                        e.source_id, e.target_id, e.edge_type,
                        e.weight, json.dumps(e.metadata),
                        e.created_at) for e in payload.edges]
                    with conn.cursor() as cur:
                        cur.executemany(
                            f'insert into {schema}.edges'
                            ' (source_id, target_id, edge_type,'
                            ' weight, metadata, created_at)'
                            ' values (%s, %s, %s, %s, %s::jsonb,'
                            ' %s)'
                            ' on conflict (source_id, target_id,'
                            ' edge_type) do nothing',
                            edge_rows)

                if payload.oplog:
                    op_rows = []
                    for op in payload.oplog:
                        legacy = op.legacy_id or op.id
                        op_rows.append((
                            op.operation, op.insight_id, op.detail,
                            op.created_at,
                            json.dumps(op.before)
                            if op.before is not None else None,
                            json.dumps(op.after)
                            if op.after is not None else None,
                            legacy))
                    with conn.cursor() as cur:
                        cur.executemany(
                            f'insert into {schema}.oplog'
                            ' (operation, insight_id, detail,'
                            '  created_at, before, after, legacy_id)'
                            ' values (%s, %s, %s, %s, %s::jsonb,'
                            ' %s::jsonb, %s)'
                            ' on conflict (legacy_id) do nothing',
                            op_rows)

                if payload.embedding_pending:
                    with conn.cursor() as cur:
                        cur.execute(
                            f'alter table {schema}.insights'
                            ' add column if not exists'
                            f' embedding_pending vector({dim})')
                        for p in payload.embedding_pending:
                            cur.execute(
                                f'update {schema}.insights'
                                ' set embedding_pending = %s'
                                ' where id = %s',
                                ([float(x) for x in p.vector],
                                 p.insight_id))

                meta_rows = list(payload.meta.items())
                if payload.swap_state:
                    s = payload.swap_state
                    meta_rows.extend([
                        ('embed_swap_target_provider',
                         s.target_provider),
                        ('embed_swap_target_model', s.target_model),
                        ('embed_swap_target_dim', str(s.target_dim)),
                        ('embed_swap_cursor', s.cursor or ''),
                        ])
                if meta_rows:
                    with conn.cursor() as cur:
                        cur.executemany(
                            f'insert into {schema}.meta (key, value)'
                            ' values (%s, %s)'
                            ' on conflict (key) do update'
                            ' set value = excluded.value',
                            meta_rows)

                conn.commit()
            except Exception as exc:
                conn.rollback()
                if isinstance(exc, MigrateError):
                    raise
                raise MigrateError(
                    f'postgres apply for store {store!r} failed:'
                    f' {type(exc).__name__}: {exc}') from exc

    def archive(self, store: str, data_dir: str) -> Artifact:
        from memman.setup.archive import archive_postgres_schema
        try:
            path = archive_postgres_schema(data_dir, store, self.dsn)
        except RuntimeError as exc:
            raise MigrateError(
                f'archive failed for store {store!r}: {exc}'
                ) from exc
        return Artifact(
            kind='filesystem',
            location=str(path / 'dump.pgdump'), metadata={})

    def drop(self, store: str) -> None:
        drop_postgres_store(store, self.dsn)
