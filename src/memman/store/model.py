"""Shared dataclasses for backend implementations and pipeline code.

Domain types (Insight, Edge) plus DTOs returned by Backend Protocol
verbs (Neighbor, ScoredId, OpLogEntry, OpLogStats, NodeStats,
ProvenanceCount, IntegrityReport, QueueRow, QueueHints, QueueStats,
WorkerRun, ReembedRow). Includes the timestamp helper and importance
helpers used across the package.

Protocol commitment: `Insight.created_at`, `Insight.updated_at`,
and `Edge.created_at` carry no `default_factory` -- backends stamp
these server-side at the verb boundary. In-memory construction without
a value yields `None`; backends fill them in on insert and reads
return them populated.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger('memman')

Id = str
Score = float

VALID_CATEGORIES = {
    'preference', 'decision', 'fact',
    'insight', 'context',
    }

VALID_EDGE_TYPES = {'temporal', 'semantic', 'causal', 'entity'}


@dataclass
class Insight:
    """A memory node in the memman graph."""

    id: str = ''
    content: str = ''
    category: str = 'fact'
    importance: int = 3
    entities: list[str] = field(default_factory=list)
    source: str = 'user'
    access_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deleted_at: datetime | None = None
    last_accessed_at: datetime | None = None
    prompt_version: str | None = None
    model_id: str | None = None
    embedding_model: str | None = None
    summary: str = ''
    linked_at: datetime | None = None
    enriched_at: datetime | None = None
    session_id: str | None = None
    queue_uuid: str | None = None
    corroboration_count: int = 0
    superseded_by: str | None = None

    def entities_json(self) -> str:
        """Return entities as a JSON string for storage."""
        return json.dumps(self.entities, sort_keys=True)

    def parse_entities(self, s: str) -> None:
        """Parse a JSON string into the entities field."""
        try:
            self.entities = json.loads(s)
        except (json.JSONDecodeError, TypeError):
            self.entities = []
        if self.entities is None:
            self.entities = []


@dataclass
class Edge:
    """A directed relationship between two insights."""

    source_id: str = ''
    target_id: str = ''
    edge_type: str = 'semantic'
    weight: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None

    def metadata_json(self) -> str:
        """Return metadata as a JSON string for storage."""
        return json.dumps(self.metadata, sort_keys=True)

    def parse_metadata(self, s: str) -> None:
        """Parse a JSON string into the metadata field."""
        try:
            self.metadata = json.loads(s)
        except (json.JSONDecodeError, TypeError):
            self.metadata = {}
        if self.metadata is None:
            self.metadata = {}


@dataclass
class Neighbor:
    """A graph neighbor: target id + edge type + weight."""

    target_id: Id
    edge_type: str
    weight: float


@dataclass
class ScoredId:
    """An id with an associated score (similarity, anchor weight, etc)."""

    id: Id
    score: Score


@dataclass
class OpLogEntry:
    """One row from the oplog table.

    `before` and `after` capture the insight content before and
    after the logged operation. Populated by reconcile, replace,
    and forget so forensic questions can be answered
    from the oplog alone. Older rows may have both as None.
    """

    id: int
    operation: str
    insight_id: str
    detail: str
    created_at: datetime
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


def insight_to_delta_dict(ins: 'Insight') -> dict[str, Any]:
    """Return the content fields of an insight for oplog deltas.

    Excludes embedding (it is not on the dataclass anyway), the
    surrogate `id`, and timestamps -- the surrounding oplog row
    already carries `insight_id` and `created_at`.
    """
    return {
        'content': ins.content,
        'category': ins.category,
        'importance': ins.importance,
        'entities': list(ins.entities or []),
        'source': ins.source,
        'summary': ins.summary,
        }


BRIEF_CONTENT_CHARS = 200


def insight_to_brief_dict(ins: 'Insight') -> dict[str, Any]:
    """Return the projection `recall --brief` emits in place of the full row.

    Parameters
    ----------
    ins : Insight
        The insight to project.

    Returns
    -------
    dict[str, Any]
        `id`, `category`, `importance`, `created_at`, and `summary`,
        plus `truncated: True` when `summary` holds a content prefix
        rather than a real summary.

    Notes
    -----
    - `summary` is the single text key either way, so a caller reads
      one field and checks `truncated` to learn whether anything was
      withheld.
    - `created_at` is formatted identically to the full projection's,
      because a brief page is the one a WHEN query reads and row
      order carries no timeline: rows come back in relevance order,
      so the field is the only thing a caller can sort on.
    - A row can reach here with no summary several ways: the
      enrichment compression gate blanks one that is too close to the
      content, and an LLM or parse failure leaves the row unenriched
      entirely. A summary-only projection would return an unreadable
      row for a large minority of the store, so the fallback is the
      first `BRIEF_CONTENT_CHARS` characters of `content`.
    - `truncated` marks content the caller has NOT seen, so it fires
      only when the fallback actually cut something. Marking every
      fallback would be false for most of them -- the compression gate
      blanks summaries precisely when content is short, so 230 of the
      253 summary-less rows across ten live stores sit under the
      limit -- and would send the caller to `insights show` for a row
      it already holds in full.
    """
    out: dict[str, Any] = {
        'id': ins.id,
        'category': ins.category,
        'importance': ins.importance,
        'created_at': format_timestamp(ins.created_at),
        }
    if ins.summary.strip():
        out['summary'] = ins.summary
    else:
        out['summary'] = ins.content[:BRIEF_CONTENT_CHARS]
        if len(ins.content) > BRIEF_CONTENT_CHARS:
            out['truncated'] = True
    return out


def insight_to_full_dict(ins: 'Insight') -> dict[str, Any]:
    """Return the user-visible fields of an insight for JSON output.

    Used by CLI commands that emit Insight objects to stdout.
    Timestamps are formatted with
    `format_timestamp`; `updated_at` falls back to `created_at` so
    consumers always see a populated value. Optional fields
    (`deleted_at`, `superseded_by`, `summary`, `linked_at`,
    `enriched_at`) are emitted only when populated; the plumbing keys
    (`session_id`, `queue_uuid`) are deliberately omitted.
    """
    out: dict[str, Any] = {
        'id': ins.id,
        'content': ins.content,
        'category': ins.category,
        'importance': ins.importance,
        'entities': list(ins.entities or []),
        'source': ins.source,
        'access_count': ins.access_count,
        'corroboration_count': ins.corroboration_count,
        'created_at': format_timestamp(ins.created_at),
        'updated_at': format_timestamp(ins.updated_at or ins.created_at),
        }
    if ins.deleted_at:
        out['deleted_at'] = format_timestamp(ins.deleted_at)
    if ins.superseded_by:
        out['superseded_by'] = ins.superseded_by
    if ins.summary:
        out['summary'] = ins.summary
    if ins.linked_at:
        out['linked_at'] = format_timestamp(ins.linked_at)
    if ins.enriched_at:
        out['enriched_at'] = format_timestamp(ins.enriched_at)
    return out


@dataclass
class OpLogStats:
    """Aggregated oplog statistics."""

    operation_counts: dict[str, int] = field(default_factory=dict)
    never_accessed: int = 0
    total_active: int = 0


@dataclass
class NodeStats:
    """Aggregate node statistics returned by `backend.nodes.stats`.

    Attributes
    ----------
    total_insights : int
        Current rows: neither deleted nor superseded.
    superseded_insights : int
        Rows with `superseded_by` set and `deleted_at` null.
    deleted_insights : int
        Rows with `deleted_at` set, superseded or not. The three
        counts partition `count_total`.
    """

    total_insights: int = 0
    superseded_insights: int = 0
    deleted_insights: int = 0
    edge_count: int = 0
    oplog_count: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    top_entities: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ProvenanceCount:
    """One (prompt_version, model_id, count) tuple from provenance distribution.
    """

    prompt_version: str | None
    model_id: str | None
    count: int


@dataclass
class IntegrityReport:
    """Aggregate integrity findings used by `memman doctor`."""

    orphan_count: int = 0
    total_active: int = 0
    dangling_by_type: dict[str, int] = field(default_factory=dict)
    degree_distribution: dict[str, int] = field(default_factory=dict)
    provenance: list[ProvenanceCount] = field(default_factory=list)


@dataclass
class EnrichmentCoverage:
    """Per-field NULL counts for the enrichment columns on `insights`.

    `memman doctor` consumes this to report which enrichment fields
    (embedding, keywords, summary, semantic_facts) have unfilled
    values among active insights.
    """

    total_active: int = 0
    missing_embedding: int = 0
    missing_keywords: int = 0
    missing_summary: int = 0
    missing_semantic_facts: int = 0


@dataclass
class QueueRow:
    """One claimable row from the per-host queue."""

    id: int
    store: str
    op: str
    payload: str
    attempts: int
    created_at: datetime


@dataclass
class QueueHints:
    """Hints attached to a queue row (per-store recency, etc)."""

    store: str
    last_seen: datetime | None = None
    pending_count: int = 0


@dataclass
class QueueStats:
    """Aggregate queue statistics."""

    total: int = 0
    by_store: dict[str, int] = field(default_factory=dict)


@dataclass
class WorkerRun:
    """One worker drain run record."""

    id: int
    started_at: datetime
    ended_at: datetime | None
    rows_processed: int
    error: str = ''
    last_heartbeat_at: datetime | None = None


@dataclass
class ReembedRow:
    """One row returned by `nodes.iter_for_reembed`."""

    id: Id
    content: str
    embedding_model: str | None
    blob_length: int | None


def format_timestamp(dt: datetime) -> str:
    """Format datetime as RFC3339 with Z suffix (Go-compatible)."""
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_timestamp(s: str) -> datetime:
    """Parse RFC3339 timestamp, accepting both Z and +00:00 suffixes."""
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    return datetime.fromisoformat(s)


def format_float(value: float) -> str:
    """Format float to 4 decimal places (Go parity)."""
    return f'{value:.4f}'
