"""Database connection, schema migration, and store management."""

import logging
import os
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote

from memman.store.errors import BackendError

logger = logging.getLogger('memman')

DEFAULT_STORE_NAME = 'default'

_VALID_STORE_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$')


def valid_store_name(name: str) -> bool:
    """Return True if name matches [a-zA-Z0-9][a-zA-Z0-9_-]*."""
    return bool(_VALID_STORE_NAME_RE.match(name))


def portable_store_name(name: str) -> str:
    """Rewrite `name` into a form every backend can host.

    Parameters
    ----------
    name : str
        Any store name, including one no rule has yet checked.
        Callers pass raw `list_local_store_dirs` output, so a
        directory name carrying a dot or a space arrives here.

    Returns
    -------
    str
        `name` with every character outside `[A-Za-z0-9_]` replaced
        by an underscore, prefixed with `s_` unless it then opens on
        a letter.

    Notes
    -----
    - Suggestion text only. The caller renames nothing. There is no
      `memman store rename`, so an operator acts by hand.
    - The output clears `_check_identifier` AND `valid_store_name`,
      because an operator types it into `memman store create`.
      Meeting only the first would suggest a name that command
      rejects, which is why a leading digit or underscore takes the
      `s_` prefix.
    - Rewriting every illegal character, not just the hyphen, is what
      keeps the suggestion creatable: `default.bak` must not come
      back unchanged.
    - The output is not unique: `a-b` and `a_b` both yield `a_b`,
      which may name a different live store.
    """
    portable = re.sub(r'[^A-Za-z0-9_]', '_', name)
    if not portable[:1].isalpha():
        portable = f's_{portable}'
    return portable


def default_data_dir() -> str:
    """Return ~/.memman."""
    home = Path.home()
    return str(home / '.memman')


def store_dir(base_dir: str, name: str) -> str:
    """Return <base_dir>/data/<name>."""
    return os.path.join(base_dir, 'data', name)


def active_file(base_dir: str) -> str:
    """Return path to <base_dir>/active."""
    return os.path.join(base_dir, 'active')


def read_active(base_dir: str) -> str:
    """Read the active store name from <base_dir>/active."""
    try:
        data = Path(active_file(base_dir)).read_text()
    except (OSError, FileNotFoundError):
        return DEFAULT_STORE_NAME
    name = data.strip()
    return name or DEFAULT_STORE_NAME


def write_active(base_dir: str, name: str) -> None:
    """Write the active store name to <base_dir>/active."""
    Path(base_dir).mkdir(mode=0o755, exist_ok=True, parents=True)
    Path(active_file(base_dir)).write_text(name + '\n')


def list_local_store_dirs(base_dir: str) -> list[str]:
    """Return sorted names of every SQLite store dir under
    `<base_dir>/data/`.

    SQLite-only filesystem scanner. Cross-backend enumeration
    (filesystem dirs ∪ Postgres `pg_namespace`) lives in
    `memman.store.factory.list_stores`; that is the helper to use
    from any code path that can encounter postgres-routed stores.
    """
    data_dir = os.path.join(base_dir, 'data')
    if not Path(data_dir).is_dir():
        return []
    names = sorted(
        e.name for e in os.scandir(data_dir) if e.is_dir())
    return names


def store_exists(base_dir: str, name: str) -> bool:
    """Check whether the named store directory exists."""
    path = store_dir(base_dir, name)
    return Path(path).is_dir()


class DB:
    """Wraps a SQLite database connection."""

    def __init__(self, conn: sqlite3.Connection, path: str) -> None:
        self._conn = conn
        self._in_tx = False
        self.path = path

    @property
    def conn(self) -> sqlite3.Connection:
        """Return the underlying connection."""
        return self._conn

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None) -> None:
        self.close()

    def _exec(
            self, sql: str,
            params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        """Execute a write SQL statement."""
        return self._conn.execute(sql, params)

    def _query(
            self, sql: str,
            params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        """Query SQL using the transaction cursor or connection."""
        return self._conn.execute(sql, params)

    def in_transaction(self, fn: Callable[[], Any]) -> Any:
        """Run fn inside a single SQL transaction, returning its result."""
        if self._in_tx:
            raise RuntimeError('nested transactions not supported')
        self._in_tx = True
        try:
            self._conn.execute('begin immediate')
            result = fn()
            self._conn.execute('commit')
            return result
        except Exception:
            self._conn.execute('rollback')
            raise
        finally:
            self._in_tx = False


def get_meta(db: 'DB', key: str) -> str | None:
    """Read a value from the meta key-value table."""
    row = db._query(
        'select value from meta where key = ?', (key,)).fetchone()
    return row[0] if row else None


def storage_summary(db: 'DB') -> dict[str, Any]:
    """Return backend-specific storage information for the active DB.

    SQLite-specific: {'db_path': <file path>, 'db_size_bytes': <int>}.
    Used by the `memman status` command.
    """
    summary: dict[str, Any] = {'db_path': db.path}
    try:
        summary['db_size_bytes'] = Path(db.path).stat().st_size
    except OSError:
        summary['db_size_bytes'] = 0
    return summary


def set_meta(db: 'DB', key: str, value: str) -> None:
    """Write a value to the meta key-value table."""
    db._exec(
        'insert or replace into meta (key, value) values (?, ?)',
        (key, value))


def open_db(data_dir: str) -> DB:
    """Open (or create) the SQLite database for one store.

    Applies the baseline schema idempotently. Does NOT trigger the
    edge-constants reindex - callers that want that (the CLI's
    `_open_store_db`) invoke `reindex_if_constants_changed(backend,
    store_name=...)` after open. Keeping the graph-reindex out of
    this module avoids a backward import edge from `memman.store` to
    `memman.graph`.

    Parameters
    ----------
    data_dir : str
        The STORE directory, not the base data directory: every
        caller passes `store_dir(base_dir, name)` output, and the
        database is read from `<data_dir>/memman.db`. Created, with
        any missing parent, when absent.

    Returns
    -------
    DB
        An open handle the caller owns and closes, by `DB.close()`
        or the `with` form.

    Raises
    ------
    BackendError
        When the store directory cannot be created, when
        `<data_dir>/memman.db` cannot be opened or read as a
        database, or when the store predates the current schema.

    Notes
    -----
    - A zero-length `memman.db` raises nothing: SQLite reads it as a
      fresh database, so this function recreates the baseline schema
      in it and returns a working, empty store. Only a partially
      truncated file reads as malformed.
    """
    try:
        Path(data_dir).mkdir(mode=0o755, exist_ok=True, parents=True)
    except OSError as exc:
        raise BackendError(
            f'cannot create store directory {data_dir}: {exc}') from exc
    db_path = os.path.join(data_dir, 'memman.db')
    try:
        is_new_db = not Path(db_path).exists()
    except OSError as exc:
        raise BackendError(
            f'cannot open database {db_path}: {exc}') from exc
    try:
        conn = sqlite3.connect(db_path, isolation_level=None)
    except sqlite3.Error as exc:
        raise BackendError(
            f'cannot open database {db_path}: {exc}') from exc
    try:
        if is_new_db:
            conn.execute('pragma auto_vacuum=incremental')
        conn.execute('pragma journal_mode=wal')
        conn.execute('pragma foreign_keys=on')
        conn.execute('pragma busy_timeout=5000')
        db = DB(conn, db_path)
        _migrate(db)
    except sqlite3.Error as exc:
        conn.close()
        raise BackendError(
            f'cannot open database {db_path}: {exc}') from exc
    except Exception:
        conn.close()
        raise
    return db


def open_read_only(data_dir: str) -> DB:
    """Open one store's SQLite database in read-only mode.

    Parameters
    ----------
    data_dir : str
        The STORE directory, exactly as `open_db` takes it. The
        database is read from `<data_dir>/memman.db` and is never
        created.

    Returns
    -------
    DB
        An open read-only handle the caller owns and closes, by
        `DB.close()` or the `with` form.

    Raises
    ------
    BackendError
        When `<data_dir>/memman.db` is absent, or cannot be opened or
        read as a database. Never a bare `OSError` or `sqlite3.Error`.

    Notes
    -----
    - Of the four callers, only `_count_active_rows` (under `memman
      embed reembed`) reports the failure: it reaches the CLI root
      group, which catches `BackendError` alone. `memman prime`,
      `graph.engine.link_pending` and `pipeline.remember` each wrap
      their call in `except Exception` and carry on without the
      read-only handle, so on those three a raised error is a silent
      degrade, not a message.
    """
    db_path = os.path.join(data_dir, 'memman.db')
    try:
        found = Path(db_path).exists()
    except OSError as exc:
        raise BackendError(
            f'cannot open database {db_path}: {exc}') from exc
    if not found:
        raise BackendError(f'database not found: {db_path}')
    # Percent-encode: SQLite cuts a URI at the first `?`, so a raw `#`
    # or `?` in the path both truncates the filename and demotes
    # `mode=ro` to an unrecognized parameter, which opens -- and
    # creates -- a different file read-write.
    uri = f'file:{quote(db_path)}?mode=ro'
    try:
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    except sqlite3.Error as exc:
        raise BackendError(
            f'cannot open database {db_path}: {exc}') from exc
    try:
        conn.execute('pragma foreign_keys=on')
        # Notes:
        # - Forces the header read, so a malformed file fails here
        #   rather than at the caller's first query, outside any
        #   translation. `pragma foreign_keys` alone does not touch
        #   the file and returns cleanly on corrupt bytes.
        # - Must stay a READ. A write pragma (`journal_mode`) fails
        #   on `mode=ro` against any store not already in WAL, which
        #   is every store a `backup restore` just laid down.
        conn.execute('pragma schema_version')
    except sqlite3.Error as exc:
        conn.close()
        raise BackendError(
            f'cannot open database {db_path}: {exc}') from exc
    except Exception:
        conn.close()
        raise
    return DB(conn, db_path)


_BASELINE_SCHEMA = """
create table if not exists insights (
    id          text primary key,
    content     text not null,
    category    text default 'fact',
    importance  integer default 3,
    entities    text default '[]',
    source      text default 'user',
    access_count integer default 0,
    keywords    text,
    summary     text,
    semantic_facts text,
    last_accessed_at text,
    embedding   blob,
    embedding_pending blob,
    linked_at   text,
    enriched_at text,
    created_at  text not null,
    updated_at  text not null,
    deleted_at  text,
    prompt_version text,
    model_id    text,
    embedding_model text,
    session_id  text,
    queue_uuid  text,
    corroboration_count integer not null default 0,
    superseded_by text
);

create table if not exists edges (
    source_id   text not null,
    target_id   text not null,
    edge_type   text not null check(edge_type in ('temporal','semantic','causal','entity')),
    weight      real default 1.0,
    metadata    text default '{}',
    created_at  text not null,
    primary key (source_id, target_id, edge_type),
    foreign key (source_id) references insights(id) on delete cascade,
    foreign key (target_id) references insights(id) on delete cascade
);

create index if not exists idx_insights_category on insights(category);
create index if not exists idx_insights_importance on insights(importance);
create index if not exists idx_insights_created on insights(created_at);
create index if not exists idx_insights_deleted on insights(deleted_at);
create index if not exists idx_insights_source on insights(source);
create index if not exists idx_insights_session on insights(session_id);
create index if not exists idx_insights_queue_uuid on insights(queue_uuid);
-- Load-bearing as the schema canary, not as a query index: this is
-- the statement that makes a 0.18.x store fail at open (see _migrate).
create index if not exists idx_insights_corroboration on insights(corroboration_count);
-- `created_at` rides along so the scheduler's pending-link scan
-- takes its order from the index; without it the planner prefers
-- the listing index below and sorts every current row per tick.
create index if not exists idx_insights_pending_link
    on insights(linked_at, created_at)
    where linked_at is null and deleted_at is null and superseded_by is null;
-- Load-bearing twice: as the schema canary, the statement that
-- makes a 0.32.x store fail at open (see _migrate); and as the
-- carrier of `query_insights`' whole predicate and sort order, so
-- `recall --basic` honors its limit from the index instead of
-- reading every current row into a temp b-tree. Declared as a
-- plain composite, not a partial index: the planner searches the
-- two leading null columns as equalities, while it passes over a
-- partial `(importance, created_at)` for a temp b-tree.
create index if not exists idx_insights_current_listing
    on insights(deleted_at, superseded_by, importance, created_at);

create index if not exists idx_edges_source on edges(source_id);
create index if not exists idx_edges_target on edges(target_id);
create index if not exists idx_edges_type on edges(edge_type);
create index if not exists idx_edges_source_type on edges(source_id, edge_type);
create index if not exists idx_edges_target_type on edges(target_id, edge_type);

create table if not exists oplog (
    id          integer primary key autoincrement,
    operation   text not null,
    insight_id  text,
    detail      text default '',
    created_at  text not null,
    before      text,
    after       text
);
create index if not exists idx_oplog_created on oplog(created_at);

create table if not exists meta (
    key   text primary key,
    value text not null
);
"""


# Keyword channel index, applied by `_migrate` in one transaction
# rather than from `_BASELINE_SCHEMA`. External content: FTS5 holds
# the terms, the text stays in `insights`. Every row is indexed,
# soft-deleted and superseded ones included, and the active
# predicate is applied by joining `insights` at read -- an
# active-only index would need conditional delete triggers, and a
# 'delete' whose old values are not exactly
# what was indexed corrupts the index silently.
_FTS_STATEMENTS = (
    """
create virtual table insights_fts using fts5(
    content,
    entities,
    content='insights',
    content_rowid='rowid',
    tokenize="unicode61 remove_diacritics 0"
)
""",
    # Scoped to the two indexed columns: a bare `after update` would
    # make `increment_access_count` and `update_enrichment` write to
    # the index on every recall.
    """
create trigger insights_fts_insert after insert on insights begin
    insert into insights_fts(rowid, content, entities)
    values (new.rowid, new.content, new.entities);
end
""",
    """
create trigger insights_fts_delete after delete on insights begin
    insert into insights_fts(insights_fts, rowid, content, entities)
    values ('delete', old.rowid, old.content, old.entities);
end
""",
    """
create trigger insights_fts_update
after update of content, entities on insights begin
    insert into insights_fts(insights_fts, rowid, content, entities)
    values ('delete', old.rowid, old.content, old.entities);
    insert into insights_fts(rowid, content, entities)
    values (new.rowid, new.content, new.entities);
end
""",
    "insert into insights_fts(insights_fts) values('rebuild')",
    )


def _migrate(db: DB) -> None:
    """Apply the canonical schema to the database.

    Single-user tool: one authoritative schema (`_BASELINE_SCHEMA`),
    always the latest. `create table if not exists` creates a fresh
    database; pre-existing databases must already match the canonical
    shape -- a schema change is applied to each live store by hand,
    once, rather than carried here as an `alter` migration.

    Notes
    -----
    - A pre-migration store fails here on every open: `create table
      if not exists` no-ops on an existing table, so the tripwire is
      the baseline's `create index` on the NEWEST schema column
      (the first `create index` that names `superseded_by`: here
      `idx_insights_current_listing`, which lists it as a key column;
      on Postgres the pending-link index, whose predicate is resolved
      before the if-not-exists check) raising `no such column`. Every
      schema
      change must index its newest column or the old store opens
      silently and fails later with a raw
      OperationalError. This is the primary schema diagnostic:
      nothing that needs a live Backend can report on such a store.
    - Creating `insights_fts` also populates it, in ONE transaction.
      The triggers only carry rows written after the table exists, so
      a store that predates it -- or one restored from a backup that
      does -- would otherwise open with an empty index and silently
      lose the keyword channel. Atomicity is what makes that safe:
      the connection is autocommit and `executescript` commits before
      it runs, so creating the table there would leave an empty index
      durable if the backfill were interrupted, and the absence check
      would then read as "already migrated" forever.
    """
    try:
        db._conn.executescript(_BASELINE_SCHEMA)
    except sqlite3.OperationalError as exc:
        if 'no such column' in str(exc):
            name = Path(db.path).parent.name
            raise BackendError(
                f'store {name} predates the current schema ({exc});'
                ' add the missing column to the live store and drop'
                ' its stale partial indexes, then reopen') from exc
        raise
    has_fts = db._conn.execute(
        "select 1 from sqlite_master"
        " where type = 'table' and name = 'insights_fts'").fetchone()
    if has_fts:
        return
    db._conn.execute('begin immediate')
    try:
        for statement in _FTS_STATEMENTS:
            db._conn.execute(statement)
    except Exception:
        db._conn.execute('rollback')
        raise
    db._conn.execute('commit')
