"""Operation logging with auto-trim."""

import logging
import sys
from datetime import datetime, timezone

from mnemon.model import format_timestamp

logger = logging.getLogger('mnemon')

MAX_OPLOG_ENTRIES = 5000


def log_op(db: 'DB', operation: str, insight_id: str,
           detail: str) -> None:
    """Record an operation to the oplog and trim old entries."""
    now = format_timestamp(datetime.now(timezone.utc))
    try:
        db._exec(
            'INSERT INTO oplog'
            ' (operation, insight_id, detail, created_at)'
            ' VALUES (?, ?, ?, ?)',
            (operation, insight_id, detail, now))
    except Exception as e:
        print(f'warning: oplog insert: {e}', file=sys.stderr)

    try:
        db._exec(
            'DELETE FROM oplog WHERE id <='
            ' (SELECT MAX(id) FROM oplog) - ?',
            (MAX_OPLOG_ENTRIES,))
    except Exception as e:
        print(f'warning: oplog trim: {e}', file=sys.stderr)


def get_oplog(db: 'DB', limit: int = 20) -> list[dict]:
    """Return the most recent N oplog entries."""
    if limit <= 0:
        limit = 20
    rows = db._query(
        'SELECT id, operation, insight_id, detail, created_at'
        ' FROM oplog ORDER BY id DESC LIMIT ?',
        (limit,)).fetchall()
    entries = [{
            'id': row[0],
            'operation': row[1],
            'insight_id': row[2] or '',
            'detail': row[3] or '',
            'created_at': row[4],
            } for row in rows]
    return entries
