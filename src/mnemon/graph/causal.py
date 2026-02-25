"""Causal edge creation and causal candidate discovery."""

import re
from datetime import datetime, timezone

from mnemon.model import Edge, Insight, format_float
from mnemon.search.keyword import tokenize
from mnemon.store.edge import insert_edge
from mnemon.store.node import get_recent_active_insights

MIN_CAUSAL_OVERLAP = 0.15
CAUSAL_LOOKBACK = 10
MAX_CAUSAL_CANDIDATES = 10

CAUSAL_PATTERN = re.compile(
    r'\b(because|therefore|due to|caused by|as a result|decided to|'
    r'chosen because|so that|in order to|leads to|results in|'
    r'enables|prevents|consequently|hence|thus)\b|'
    r'\bthis (ensures|means)\b',
    re.IGNORECASE)

CAUSES_PATTERN = re.compile(r'(?i)\b(because|caused by|due to)\b')
ENABLES_PATTERN = re.compile(
    r'(?i)\b(so that|in order to|enables|leads to)\b')
PREVENTS_PATTERN = re.compile(
    r'(?i)\b(despite|prevented|prevents|blocked)\b')


def has_causal_signal(text: str) -> bool:
    """Return True if the text contains causal keywords."""
    return bool(CAUSAL_PATTERN.search(text))


def suggest_sub_type(text: str) -> str:
    """Guess a causal sub_type from the content text."""
    if PREVENTS_PATTERN.search(text):
        return 'prevents'
    if ENABLES_PATTERN.search(text):
        return 'enables'
    return 'causes'


def find_causal_signal(text: str) -> str:
    """Return the first matching causal keyword in the text."""
    m = CAUSAL_PATTERN.search(text)
    return m.group(0) if m else ''


def token_overlap(a: set[str], b: set[str]) -> float:
    """Compute |intersection| / max(|a|, |b|)."""
    if not a or not b:
        return 0.0
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    intersection = sum(1 for k in small if k in big)
    return intersection / max(len(a), len(b))


def create_causal_edges(db: 'DB', insight: Insight) -> int:
    """Create causal edges when insights share token overlap and causal signals."""
    recent = get_recent_active_insights(
        db, insight.id, CAUSAL_LOOKBACK)
    if not recent:
        return 0

    new_tokens = tokenize(insight.content)
    if not new_tokens:
        return 0

    new_has_signal = has_causal_signal(insight.content)
    now = datetime.now(timezone.utc)
    count = 0

    for prev in recent:
        prev_has_signal = has_causal_signal(prev.content)
        if not new_has_signal and not prev_has_signal:
            continue

        prev_tokens = tokenize(prev.content)
        overlap = token_overlap(new_tokens, prev_tokens)
        if overlap < MIN_CAUSAL_OVERLAP:
            continue

        source_id = prev.id
        target_id = insight.id
        if not new_has_signal and prev_has_signal:
            source_id = insight.id
            target_id = prev.id

        sub_type = suggest_sub_type(insight.content + ' ' + prev.content)

        try:
            insert_edge(db, Edge(
                source_id=source_id, target_id=target_id,
                edge_type='causal', weight=overlap,
                metadata={
                    'overlap': format_float(overlap),
                    'sub_type': sub_type,
                    },
                created_at=now))
            count += 1
        except Exception:
            pass

    return count


def find_causal_candidates(
        db: 'DB', insight: Insight) -> list[dict]:
    """Return insights with potential causal relationships via 2-hop BFS."""
    from mnemon.graph.bfs import BFSOptions, bfs
    nodes = bfs(db, insight.id, BFSOptions(
        max_depth=2, max_nodes=MAX_CAUSAL_CANDIDATES))
    if not nodes:
        return []

    candidates = []
    for n in nodes:
        signal = find_causal_signal(n['insight'].content)
        if not signal:
            signal = find_causal_signal(insight.content)

        combined_text = insight.content + ' ' + n['insight'].content
        sub_type = suggest_sub_type(combined_text)

        candidates.append({
            'id': n['insight'].id,
            'content': n['insight'].content,
            'category': n['insight'].category,
            'hop': n['hop'],
            'via_edge': n['via_edge'],
            'causal_signal': signal,
            'suggested_sub_type': sub_type,
            })

    return candidates
