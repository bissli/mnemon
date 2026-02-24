"""Database connection, schema migration, and store management."""

import logging
import os
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger('mnemon')

DEFAULT_STORE_NAME = 'default'

_VALID_STORE_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$')


def valid_store_name(name: str) -> bool:
    """Return True if name matches [a-zA-Z0-9][a-zA-Z0-9_-]*."""
    return bool(_VALID_STORE_NAME_RE.match(name))


def default_data_dir() -> str:
    """Return ~/.mnemon."""
    home = Path.home()
    return str(home / '.mnemon')


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


def list_stores(base_dir: str) -> list[str]:
    """Return sorted names of all stores under <base_dir>/data/."""
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
        self._tx: sqlite3.Cursor | None = None
        self._in_tx = False
        self.path = path

    @property
    def conn(self) -> sqlite3.Connection:
        """Return the underlying connection."""
        return self._conn

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute SQL using the transaction cursor or connection."""
        if self._in_tx:
            return self._conn.execute(sql, params)
        return self._conn.execute(sql, params)

    def _query(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Query SQL using the transaction cursor or connection."""
        return self._conn.execute(sql, params)

    def in_transaction(self, fn: callable) -> None:
        """Run fn inside a single SQL transaction."""
        if self._in_tx:
            raise RuntimeError('nested transactions not supported')
        self._in_tx = True
        try:
            self._conn.execute('BEGIN IMMEDIATE')
            fn()
            self._conn.execute('COMMIT')
        except Exception:
            self._conn.execute('ROLLBACK')
            raise
        finally:
            self._in_tx = False


def open_db(data_dir: str) -> DB:
    """Open (or create) the SQLite database at the given directory."""
    Path(data_dir).mkdir(mode=0o755, exist_ok=True, parents=True)
    db_path = os.path.join(data_dir, 'mnemon.db')
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    db = DB(conn, db_path)
    _migrate(db)
    return db


def open_read_only(data_dir: str) -> DB:
    """Open the SQLite database in read-only mode."""
    db_path = os.path.join(data_dir, 'mnemon.db')
    if not Path(db_path).exists():
        raise FileNotFoundError(f'database not found: {db_path}')
    uri = f'file:{db_path}?mode=ro'
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.execute('PRAGMA journal_mode=OFF')
    conn.execute('PRAGMA foreign_keys=ON')
    return DB(conn, db_path)


def _migrate(db: DB) -> None:
    """Run schema migrations."""
    schema = """
CREATE TABLE IF NOT EXISTS insights (
    id          TEXT PRIMARY KEY,
    content     TEXT NOT NULL,
    category    TEXT DEFAULT 'general',
    importance  INTEGER DEFAULT 3,
    tags        TEXT DEFAULT '[]',
    entities    TEXT DEFAULT '[]',
    source      TEXT DEFAULT 'user',
    access_count INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    deleted_at  TEXT
);

CREATE TABLE IF NOT EXISTS edges (
    source_id   TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    edge_type   TEXT NOT NULL CHECK(edge_type IN ('temporal','semantic','causal','entity')),
    weight      REAL DEFAULT 1.0,
    metadata    TEXT DEFAULT '{}',
    created_at  TEXT NOT NULL,
    PRIMARY KEY (source_id, target_id, edge_type),
    FOREIGN KEY (source_id) REFERENCES insights(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES insights(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_insights_category ON insights(category);
CREATE INDEX IF NOT EXISTS idx_insights_importance ON insights(importance);
CREATE INDEX IF NOT EXISTS idx_insights_created ON insights(created_at);
CREATE INDEX IF NOT EXISTS idx_insights_deleted ON insights(deleted_at);
CREATE INDEX IF NOT EXISTS idx_insights_source ON insights(source);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type);
CREATE INDEX IF NOT EXISTS idx_edges_source_type ON edges(source_id, edge_type);
CREATE INDEX IF NOT EXISTS idx_edges_target_type ON edges(target_id, edge_type);

CREATE TABLE IF NOT EXISTS oplog (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    operation   TEXT NOT NULL,
    insight_id  TEXT,
    detail      TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oplog_created ON oplog(created_at);
"""
    db._conn.executescript(schema)

    _add_column_if_not_exists(
        db._conn, 'ALTER TABLE insights ADD COLUMN last_accessed_at TEXT')
    _add_column_if_not_exists(
        db._conn, 'ALTER TABLE insights ADD COLUMN embedding BLOB')
    _add_column_if_not_exists(
        db._conn,
        'ALTER TABLE insights ADD COLUMN effective_importance REAL DEFAULT 0.5')

    db._conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_insights_effective_imp'
        ' ON insights(effective_importance)')
    db._conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_prune_candidates'
        ' ON insights(deleted_at, importance, access_count,'
        ' effective_importance)')

    _migrate_remove_narrative_edges(db)

    row = db._conn.execute(
        "SELECT COUNT(*) FROM insights"
        " WHERE category = 'narrative' AND deleted_at IS NULL"
        ).fetchone()
    if row[0] > 0:
        db._conn.execute(
            "UPDATE insights SET deleted_at = datetime('now')"
            " WHERE category = 'narrative' AND deleted_at IS NULL")


def _add_column_if_not_exists(
        conn: sqlite3.Connection, stmt: str) -> None:
    """Run ALTER TABLE ADD COLUMN, ignoring duplicate column errors."""
    try:
        conn.execute(stmt)
    except sqlite3.OperationalError as e:
        if 'duplicate column' not in str(e).lower():
            raise


def _migrate_remove_narrative_edges(db: DB) -> None:
    """Recreate edges table without narrative type if old schema allows it."""
    try:
        db._conn.execute(
            "INSERT INTO edges VALUES"
            " ('__test','__test','narrative',0,'{}',datetime('now'))")
    except sqlite3.IntegrityError:
        return

    db._conn.execute("DELETE FROM edges WHERE source_id = '__test'")
    db._conn.execute("DELETE FROM edges WHERE edge_type = 'narrative'")
    db._conn.execute('ALTER TABLE edges RENAME TO edges_old')
    db._conn.execute("""
        CREATE TABLE edges (
            source_id   TEXT NOT NULL,
            target_id   TEXT NOT NULL,
            edge_type   TEXT NOT NULL CHECK(edge_type IN ('temporal','semantic','causal','entity')),
            weight      REAL DEFAULT 1.0,
            metadata    TEXT DEFAULT '{}',
            created_at  TEXT NOT NULL,
            PRIMARY KEY (source_id, target_id, edge_type),
            FOREIGN KEY (source_id) REFERENCES insights(id) ON DELETE CASCADE,
            FOREIGN KEY (target_id) REFERENCES insights(id) ON DELETE CASCADE
        )""")
    db._conn.execute('INSERT INTO edges SELECT * FROM edges_old')
    db._conn.execute('DROP TABLE edges_old')
    db._conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id)')
    db._conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id)')
    db._conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type)')
