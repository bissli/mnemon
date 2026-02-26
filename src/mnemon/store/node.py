"""Insight CRUD, lifecycle, statistics, and embedding operations."""

import logging
import math
import sys
from datetime import datetime, timezone

from mnemon.model import Insight, base_weight, format_timestamp, is_immune
from mnemon.model import parse_timestamp

logger = logging.getLogger('mnemon')

HALF_LIFE_DAYS = 30.0
MAX_INSIGHTS = 1000
PRUNE_BATCH_SIZE = 10


def insert_insight(db: 'DB', i: Insight) -> None:
    """Insert a new insight into the database."""
    db._exec(
        'INSERT INTO insights'
        ' (id, content, category, importance, tags, entities,'
        '  source, access_count, created_at, updated_at)'
        ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (i.id, i.content, i.category, i.importance,
         i.tags_json(), i.entities_json(), i.source, i.access_count,
         format_timestamp(i.created_at), format_timestamp(i.updated_at)))


def get_insight_by_id(db: 'DB', id: str) -> Insight | None:
    """Return a single insight by ID (excludes soft-deleted)."""
    row = db._query(
        'SELECT id, content, category, importance, tags, entities,'
        ' source, access_count, created_at, updated_at, deleted_at'
        ' FROM insights WHERE id = ? AND deleted_at IS NULL',
        (id,)).fetchone()
    if row is None:
        return None
    return _scan_insight(row)


def get_insight_by_id_include_deleted(db: 'DB', id: str) -> Insight | None:
    """Return a single insight by ID, including soft-deleted."""
    row = db._query(
        'SELECT id, content, category, importance, tags, entities,'
        ' source, access_count, created_at, updated_at, deleted_at'
        ' FROM insights WHERE id = ?',
        (id,)).fetchone()
    if row is None:
        return None
    return _scan_insight(row)


def query_insights(db: 'DB', keyword: str = '', category: str = '',
                   min_importance: int = 0, source: str = '',
                   limit: int = 20) -> list[Insight]:
    """Return insights matching filters, ordered by importance DESC, created_at DESC."""
    conditions = ['deleted_at IS NULL']
    args: list = []

    if keyword:
        conditions.append('content LIKE ?')
        args.append(f'%{keyword}%')
    if category:
        conditions.append('category = ?')
        args.append(category)
    if min_importance > 0:
        conditions.append('importance >= ?')
        args.append(min_importance)
    if source:
        conditions.append('source = ?')
        args.append(source)

    if limit <= 0:
        limit = 20
    args.append(limit)

    sql = (
        'SELECT id, content, category, importance, tags, entities,'
        ' source, access_count, created_at, updated_at, deleted_at'
        ' FROM insights WHERE ' + ' AND '.join(conditions)
        + ' ORDER BY importance DESC, created_at DESC LIMIT ?')

    rows = db._query(sql, tuple(args)).fetchall()
    return [_scan_insight(r) for r in rows]


def soft_delete_insight(db: 'DB', id: str) -> None:
    """Set deleted_at on an insight and remove all associated edges."""
    now = format_timestamp(datetime.now(timezone.utc))
    cursor = db._exec(
        'UPDATE insights SET deleted_at = ?, updated_at = ?'
        ' WHERE id = ? AND deleted_at IS NULL',
        (now, now, id))
    if cursor.rowcount == 0:
        raise ValueError(f'insight {id} not found or already deleted')
    from mnemon.store.edge import delete_edges_by_node
    delete_edges_by_node(db, id)


def update_entities(db: 'DB', id: str, entities: list[str]) -> None:
    """Update the entities field for an insight."""
    import json
    now = format_timestamp(datetime.now(timezone.utc))
    db._exec(
        'UPDATE insights SET entities = ?, updated_at = ? WHERE id = ?',
        (json.dumps(entities, sort_keys=True), now, id))


def increment_access_count(db: 'DB', id: str) -> None:
    """Bump the access count and refresh last_accessed_at."""
    now = format_timestamp(datetime.now(timezone.utc))
    db._exec(
        'UPDATE insights SET access_count = access_count + 1,'
        ' last_accessed_at = ? WHERE id = ?',
        (now, id))


def compute_effective_importance(
        importance: int, access_count: int,
        days_since_access: float, edge_count: int) -> float:
    """Calculate the current effective importance."""
    base = base_weight(importance)
    access_factor = math.log(1.0 + access_count)
    access_factor = max(access_factor, 1.0)
    decay_factor = math.pow(0.5, days_since_access / HALF_LIFE_DAYS)
    edges = min(edge_count, 5)
    edge_factor = 1.0 + 0.1 * edges
    return base * access_factor * decay_factor * edge_factor


def refresh_effective_importance(db: 'DB', id: str) -> float:
    """Recompute and store effective_importance for one insight."""
    row = db._query(
        'SELECT importance, access_count, created_at, last_accessed_at'
        ' FROM insights WHERE id = ? AND deleted_at IS NULL',
        (id,)).fetchone()
    if row is None:
        raise ValueError(f'insight {id} not found')

    importance, access_count, created_at_str, last_accessed_at_str = row
    last_access = parse_timestamp(created_at_str)
    if last_accessed_at_str:
        try:
            last_access = parse_timestamp(last_accessed_at_str)
        except ValueError:
            pass

    now = datetime.now(timezone.utc)
    days_since = (now - last_access).total_seconds() / 86400.0

    edge_row = db._query(
        'SELECT (SELECT COUNT(*) FROM edges WHERE source_id = ?) +'
        '       (SELECT COUNT(*) FROM edges WHERE target_id = ?)',
        (id, id)).fetchone()
    edge_count = edge_row[0] if edge_row else 0

    ei = compute_effective_importance(
        importance, access_count, days_since, edge_count)

    db._exec(
        'UPDATE insights SET effective_importance = ? WHERE id = ?',
        (ei, id))
    return ei


def get_retention_candidates(
        db: 'DB', threshold: float,
        limit: int) -> tuple[list[dict], int]:
    """Return non-immune insights sorted by effective_importance ascending."""
    rows = db._query(
        'SELECT id, content, category, importance, tags, entities,'
        ' source, access_count, created_at, updated_at, deleted_at,'
        ' last_accessed_at'
        ' FROM insights WHERE deleted_at IS NULL').fetchall()

    insight_rows = []
    for r in rows:
        ins = _scan_insight(r[:11])
        last_accessed_str = r[11]
        last_access = ins.created_at
        if last_accessed_str:
            try:
                last_access = parse_timestamp(last_accessed_str)
            except ValueError:
                pass
        insight_rows.append((ins, last_access))

    ec_rows = db._query(
        'SELECT id, SUM(cnt) FROM ('
        '  SELECT source_id AS id, COUNT(*) AS cnt'
        '   FROM edges GROUP BY source_id'
        '  UNION ALL'
        '  SELECT target_id AS id, COUNT(*) AS cnt'
        '   FROM edges GROUP BY target_id'
        ') GROUP BY id').fetchall()
    edge_counts: dict[str, int] = dict(ec_rows)

    now = datetime.now(timezone.utc)
    updates = []
    candidates = []
    for ins, last_access in insight_rows:
        days_since = (now - last_access).total_seconds() / 86400.0
        ec = edge_counts.get(ins.id, 0)
        ei = compute_effective_importance(
            ins.importance, ins.access_count, days_since, ec)
        immune = is_immune(ins.importance, ins.access_count)
        updates.append((ei, ins.id))

        if ei < threshold and not immune:
            candidates.append({
                'insight': ins,
                'effective_importance': ei,
                'days_since_access': days_since,
                'edge_count': ec,
                'immune': immune,
                })

    if updates:
        try:
            db._conn.execute('BEGIN')
            for ei_val, uid in updates:
                db._conn.execute(
                    'UPDATE insights SET effective_importance = ?'
                    ' WHERE id = ?', (ei_val, uid))
            db._conn.execute('COMMIT')
        except Exception as e:
            db._conn.execute('ROLLBACK')
            print(
                f'warning: batch EI update failed, rolled back: {e}',
                file=sys.stderr)

    candidates.sort(key=lambda c: c['effective_importance'])
    total = len(insight_rows)
    if limit > 0 and len(candidates) > limit:
        candidates = candidates[:limit]
    return candidates, total


def count_active_insights(db: 'DB') -> int:
    """Return the number of non-deleted insights."""
    row = db._query(
        'SELECT COUNT(*) FROM insights WHERE deleted_at IS NULL'
        ).fetchone()
    return row[0]


def auto_prune(db: 'DB', max_insights: int,
               exclude_ids: list[str] | None = None) -> int:
    """Soft-delete the lowest EI non-immune insights when over capacity."""
    if exclude_ids is None:
        exclude_ids = []

    total = count_active_insights(db)
    if total <= max_insights:
        return 0

    excess = min(total - max_insights, PRUNE_BATCH_SIZE)

    args: list = list(exclude_ids)
    exclude_clause = ''
    if exclude_ids:
        placeholders = ','.join('?' for _ in exclude_ids)
        exclude_clause = f'AND id NOT IN ({placeholders})'
    args.append(excess)

    rows = db._query(
        f'SELECT id FROM insights'
        f' WHERE deleted_at IS NULL AND importance < 4'
        f' AND access_count < 3 {exclude_clause}'
        f' ORDER BY effective_importance ASC LIMIT ?',
        tuple(args)).fetchall()

    now = format_timestamp(datetime.now(timezone.utc))
    pruned = 0
    for (cid,) in rows:
        cursor = db._exec(
            'UPDATE insights SET deleted_at = ?, updated_at = ?'
            ' WHERE id = ? AND deleted_at IS NULL',
            (now, now, cid))
        if cursor.rowcount > 0:
            from mnemon.store.edge import delete_edges_by_node
            delete_edges_by_node(db, cid)
            pruned += 1
    return pruned


def review_content_quality(
        db: 'DB', limit: int = 50) -> list[dict]:
    """Review active insights for content quality issues."""
    from mnemon.search.quality import check_content_quality

    insights = get_all_active_insights(db)
    flagged = []
    for ins in insights:
        warnings = check_content_quality(ins.content)
        if warnings:
            flagged.append({
                'insight': ins,
                'quality_warnings': warnings,
                })
    flagged.sort(key=lambda x: len(x['quality_warnings']), reverse=True)
    return flagged[:limit]


def boost_retention(db: 'DB', id: str) -> None:
    """Boost an insight's retention: access_count +3, refreshes last_accessed_at."""
    now = format_timestamp(datetime.now(timezone.utc))
    cursor = db._exec(
        'UPDATE insights SET access_count = access_count + 3,'
        ' last_accessed_at = ?, updated_at = ?'
        ' WHERE id = ? AND deleted_at IS NULL',
        (now, now, id))
    if cursor.rowcount == 0:
        raise ValueError(f'insight {id} not found or already deleted')


def get_recent_insights_in_window(
        db: 'DB', exclude_id: str, window_hours: float,
        limit: int) -> list[Insight]:
    """Return non-deleted insights created within the given time window."""
    cutoff = datetime.now(timezone.utc).timestamp() - window_hours * 3600
    cutoff_dt = datetime.fromtimestamp(cutoff, tz=timezone.utc)
    cutoff_str = format_timestamp(cutoff_dt)
    rows = db._query(
        'SELECT id, content, category, importance, tags, entities,'
        ' source, access_count, created_at, updated_at, deleted_at'
        ' FROM insights WHERE id != ? AND deleted_at IS NULL'
        ' AND created_at >= ?'
        ' ORDER BY created_at DESC LIMIT ?',
        (exclude_id, cutoff_str, limit)).fetchall()
    return [_scan_insight(r) for r in rows]


def get_latest_insight_by_source(
        db: 'DB', source: str, exclude_id: str) -> Insight | None:
    """Return the most recent non-deleted insight for a given source."""
    row = db._query(
        'SELECT id, content, category, importance, tags, entities,'
        ' source, access_count, created_at, updated_at, deleted_at'
        ' FROM insights WHERE source = ? AND id != ?'
        ' AND deleted_at IS NULL'
        ' ORDER BY created_at DESC, rowid DESC LIMIT 1',
        (source, exclude_id)).fetchone()
    if row is None:
        return None
    return _scan_insight(row)


def get_recent_active_insights(
        db: 'DB', exclude_id: str,
        limit: int) -> list[Insight]:
    """Return the N most recent non-deleted insights regardless of source."""
    rows = db._query(
        'SELECT id, content, category, importance, tags, entities,'
        ' source, access_count, created_at, updated_at, deleted_at'
        ' FROM insights WHERE id != ? AND deleted_at IS NULL'
        ' ORDER BY created_at DESC LIMIT ?',
        (exclude_id, limit)).fetchall()
    return [_scan_insight(r) for r in rows]


def get_all_active_insights(db: 'DB') -> list[Insight]:
    """Return all non-deleted insights."""
    rows = db._query(
        'SELECT id, content, category, importance, tags, entities,'
        ' source, access_count, created_at, updated_at, deleted_at'
        ' FROM insights WHERE deleted_at IS NULL'
        ' ORDER BY created_at DESC').fetchall()
    return [_scan_insight(r) for r in rows]


def get_stats(db: 'DB') -> dict:
    """Return aggregate statistics."""
    stats: dict = {'by_category': {}}

    row = db._query(
        'SELECT COUNT(*) FROM insights WHERE deleted_at IS NULL'
        ).fetchone()
    stats['total_insights'] = row[0]

    row = db._query(
        'SELECT COUNT(*) FROM insights WHERE deleted_at IS NOT NULL'
        ).fetchone()
    stats['deleted_insights'] = row[0]

    rows = db._query(
        'SELECT category, COUNT(*) FROM insights'
        ' WHERE deleted_at IS NULL GROUP BY category').fetchall()
    for cat, count in rows:
        stats['by_category'][cat] = count

    row = db._query('SELECT COUNT(*) FROM edges').fetchone()
    stats['edge_count'] = row[0]

    row = db._query('SELECT COUNT(*) FROM oplog').fetchone()
    stats['oplog_count'] = row[0]

    top_entities = []
    try:
        erows = db._query(
            'SELECT je.value, COUNT(DISTINCT i.id) as cnt'
            ' FROM insights i, json_each(i.entities) je'
            ' WHERE i.deleted_at IS NULL'
            ' GROUP BY je.value'
            ' ORDER BY cnt DESC LIMIT 20').fetchall()
        for entity, count in erows:
            top_entities.append({'entity': entity, 'count': count})
    except Exception:
        pass
    stats['top_entities'] = top_entities

    return stats


def update_embedding(db: 'DB', id: str, blob: bytes) -> None:
    """Store an embedding vector for an insight."""
    now = format_timestamp(datetime.now(timezone.utc))
    db._exec(
        'UPDATE insights SET embedding = ?, updated_at = ? WHERE id = ?',
        (blob, now, id))


def get_embedding(db: 'DB', id: str) -> bytes | None:
    """Return the raw embedding blob for an insight."""
    row = db._query(
        'SELECT embedding FROM insights'
        ' WHERE id = ? AND deleted_at IS NULL',
        (id,)).fetchone()
    if row is None or row[0] is None:
        return None
    return row[0]


def get_all_embeddings(db: 'DB') -> list[tuple[str, str, bytes]]:
    """Return all active insights that have embeddings as (id, content, blob)."""
    rows = db._query(
        'SELECT id, content, embedding FROM insights'
        ' WHERE deleted_at IS NULL AND embedding IS NOT NULL'
        ).fetchall()
    results = []
    for id, content, blob in rows:
        if blob and len(blob) > 0:
            results.append((id, content, blob))
    return results


def scan_embeddings(
        db: 'DB',
        fn: callable) -> None:
    """Stream embeddings one at a time via callback."""
    rows = db._query(
        'SELECT id, embedding FROM insights'
        ' WHERE deleted_at IS NULL AND embedding IS NOT NULL'
        ).fetchall()
    for id, blob in rows:
        if blob and len(blob) > 0 and not fn(id, blob):
            break


def embedding_stats(db: 'DB') -> tuple[int, int]:
    """Return (total_active, embedded_count)."""
    total = db._query(
        'SELECT COUNT(*) FROM insights WHERE deleted_at IS NULL'
        ).fetchone()[0]
    embedded = db._query(
        'SELECT COUNT(*) FROM insights'
        ' WHERE deleted_at IS NULL AND embedding IS NOT NULL'
        ).fetchone()[0]
    return total, embedded


def get_insights_without_embedding(
        db: 'DB', limit: int = 100) -> list[Insight]:
    """Return active insights that lack embeddings."""
    if limit <= 0:
        limit = 100
    rows = db._query(
        'SELECT id, content, category, importance, tags, entities,'
        ' source, access_count, created_at, updated_at, deleted_at'
        ' FROM insights WHERE deleted_at IS NULL AND embedding IS NULL'
        ' ORDER BY importance DESC, created_at DESC LIMIT ?',
        (limit,)).fetchall()
    return [_scan_insight(r) for r in rows]


def _scan_insight(row: tuple) -> Insight:
    """Parse a database row into an Insight dataclass."""
    i = Insight()
    i.id = row[0]
    i.content = row[1]
    i.category = row[2]
    i.importance = row[3]
    i.parse_tags(row[4])
    i.parse_entities(row[5])
    i.source = row[6]
    i.access_count = row[7]
    i.created_at = parse_timestamp(row[8])
    i.updated_at = parse_timestamp(row[9])
    if row[10]:
        i.deleted_at = parse_timestamp(row[10])
    return i
