"""Data models for mnemon: Insight and Edge dataclasses, constants, validation."""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger('mnemon')

VALID_CATEGORIES = {
    'preference', 'decision', 'fact', 'insight', 'context', 'general',
    }

VALID_EDGE_TYPES = {'temporal', 'semantic', 'causal', 'entity'}


@dataclass
class Insight:
    """A memory node in the mnemon graph."""

    id: str = ''
    content: str = ''
    category: str = 'general'
    importance: int = 3
    tags: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    source: str = 'user'
    access_count: int = 0
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))
    deleted_at: datetime | None = None
    last_accessed_at: datetime | None = None
    effective_importance: float = 0.0

    def tags_json(self) -> str:
        """Return tags as a JSON string for storage."""
        return json.dumps(self.tags, sort_keys=True)

    def entities_json(self) -> str:
        """Return entities as a JSON string for storage."""
        return json.dumps(self.entities, sort_keys=True)

    def parse_tags(self, s: str) -> None:
        """Parse a JSON string into the tags field."""
        try:
            self.tags = json.loads(s)
        except (json.JSONDecodeError, TypeError):
            self.tags = []
        if self.tags is None:
            self.tags = []

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
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))

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


def base_weight(importance: int) -> float:
    """Map importance (1-5) to a base weight."""
    weights = {5: 1.0, 4: 0.8, 3: 0.5, 2: 0.3}
    return weights.get(importance, 0.15)


def is_immune(importance: int, access_count: int) -> bool:
    """Check if an insight is immune to auto-pruning."""
    return importance >= 4 or access_count >= 3
