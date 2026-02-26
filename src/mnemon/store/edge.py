"""Edge CRUD and traversal queries."""

import logging

from mnemon.model import Edge, format_timestamp, parse_timestamp

logger = logging.getLogger('mnemon')


def insert_edge(db: 'DB', e: Edge) -> None:
    """Insert or replace an edge."""
    db._exec(
        'INSERT OR REPLACE INTO edges'
        ' (source_id, target_id, edge_type, weight, metadata, created_at)'
        ' VALUES (?, ?, ?, ?, ?, ?)',
        (e.source_id, e.target_id, e.edge_type, e.weight,
         e.metadata_json(), format_timestamp(e.created_at)))


def get_edges_by_node(db: 'DB', node_id: str) -> list[Edge]:
    """Return all edges where the given node is source or target."""
    rows = db._query(
        'SELECT source_id, target_id, edge_type, weight,'
        ' metadata, created_at'
        ' FROM edges WHERE source_id = ?'
        ' UNION ALL'
        ' SELECT source_id, target_id, edge_type, weight,'
        ' metadata, created_at'
        ' FROM edges WHERE target_id = ? AND source_id != ?',
        (node_id, node_id, node_id)).fetchall()
    return [_scan_edge(r) for r in rows]


def get_edges_by_node_and_type(
        db: 'DB', node_id: str, edge_type: str) -> list[Edge]:
    """Return edges for a node filtered by edge type."""
    rows = db._query(
        'SELECT source_id, target_id, edge_type, weight,'
        ' metadata, created_at'
        ' FROM edges WHERE source_id = ? AND edge_type = ?'
        ' UNION ALL'
        ' SELECT source_id, target_id, edge_type, weight,'
        ' metadata, created_at'
        ' FROM edges WHERE target_id = ? AND edge_type = ?'
        ' AND source_id != ?',
        (node_id, edge_type, node_id, edge_type, node_id)).fetchall()
    return [_scan_edge(r) for r in rows]


def get_edges_by_source_and_type(
        db: 'DB', source_id: str, edge_type: str) -> list[Edge]:
    """Return edges where the given node is source, filtered by type."""
    rows = db._query(
        'SELECT source_id, target_id, edge_type, weight,'
        ' metadata, created_at'
        ' FROM edges WHERE source_id = ? AND edge_type = ?',
        (source_id, edge_type)).fetchall()
    return [_scan_edge(r) for r in rows]


def find_insights_with_entity(
        db: 'DB', entity: str, exclude_id: str,
        limit: int) -> list[str]:
    """Return insight IDs that have the given entity."""
    rows = db._query(
        'SELECT DISTINCT i.id FROM insights i, json_each(i.entities) je'
        ' WHERE i.deleted_at IS NULL AND i.id != ? AND je.value = ?'
        ' ORDER BY i.created_at DESC LIMIT ?',
        (exclude_id, entity, limit)).fetchall()
    return [r[0] for r in rows]


def count_insights_with_entity(
        db: 'DB', entity: str, exclude_id: str) -> int:
    """Count distinct insights that contain the given entity."""
    row = db._query(
        'SELECT COUNT(DISTINCT i.id)'
        ' FROM insights i, json_each(i.entities) je'
        ' WHERE i.deleted_at IS NULL AND i.id != ?'
        ' AND je.value = ?',
        (exclude_id, entity)).fetchone()
    return row[0] if row else 0


def get_all_edges(db: 'DB') -> list[Edge]:
    """Return all edges in the graph."""
    rows = db._query(
        'SELECT source_id, target_id, edge_type, weight,'
        ' metadata, created_at FROM edges').fetchall()
    return [_scan_edge(r) for r in rows]


def delete_edges_by_node(db: 'DB', node_id: str) -> None:
    """Remove all edges referencing a node."""
    db._exec(
        'DELETE FROM edges WHERE source_id = ? OR target_id = ?',
        (node_id, node_id))


def _scan_edge(row: tuple) -> Edge:
    """Parse a database row into an Edge dataclass."""
    e = Edge()
    e.source_id = row[0]
    e.target_id = row[1]
    e.edge_type = row[2]
    e.weight = row[3]
    e.parse_metadata(row[4])
    e.created_at = parse_timestamp(row[5])
    return e
