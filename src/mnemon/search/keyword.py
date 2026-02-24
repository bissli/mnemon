"""Token-based keyword search and content similarity."""

import heapq
import re

from mnemon.model import Insight

STOPWORDS = {
    'a', 'an', 'the', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'shall', 'can', 'to', 'of', 'in', 'for',
    'on', 'with', 'at', 'by', 'from', 'as', 'into', 'about', 'that',
    'this', 'it', 'its', 'or', 'and', 'but', 'if', 'not', 'no', 'so',
    'up', 'out', 'than', 'then', 'too', 'very', 'just', 'also', 'more',
    'some', 'any', 'all', 'each', 'i', 'me', 'my', 'we', 'you', 'your',
    'he', 'she', 'they', 'them', 'his', 'her', 'our', 'their', 'what',
    'which', 'who', 'how', 'when', 'where',
    }

_WORD_RE = re.compile(r'[a-zA-Z0-9]+')


def tokenize(text: str) -> set[str]:
    """Split text into lowercase tokens with stopword filtering."""
    tokens: set[str] = set()
    for word in _WORD_RE.findall(text.lower()):
        if word not in STOPWORDS:
            tokens.add(word)
    return tokens


def insight_tokens(ins: Insight) -> set[str]:
    """Return combined token set from content, tags, and entities."""
    tokens = tokenize(ins.content)
    for tag in ins.tags:
        tokens |= tokenize(tag)
    for ent in ins.entities:
        tokens |= tokenize(ent)
    return tokens


def keyword_search(
        insights: list[Insight], query: str,
        limit: int,
        token_cache: dict[str, set[str]] | None = None,
        ) -> list[tuple[Insight, float]]:
    """Score insights by token overlap with query.

    Returns list of (insight, score) sorted by score descending.
    """
    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    heap_list: list[tuple[float, int, Insight]] = []
    for ins in insights:
        content_tokens = insight_tokens(ins)
        if token_cache is not None:
            token_cache[ins.id] = content_tokens

        intersection = sum(1 for t in query_tokens if t in content_tokens)
        if intersection == 0:
            continue
        score = intersection / len(query_tokens)

        entry = (score, ins.importance, ins.id, ins)
        if limit <= 0 or len(heap_list) < limit:
            heapq.heappush(heap_list, entry)
        else:
            top = heap_list[0]
            if (score > top[0]
                    or (score == top[0]
                        and ins.importance > top[1])):
                heapq.heapreplace(heap_list, entry)

    result = []
    while heap_list:
        score, _imp, _id, ins = heapq.heappop(heap_list)
        result.append((ins, score))
    result.reverse()
    return result


def content_similarity(a: str, b: str) -> float:
    """Compute bidirectional token overlap between two texts."""
    tok_a = tokenize(a)
    tok_b = tokenize(b)
    if not tok_a or not tok_b:
        return 0.0

    intersection = sum(1 for t in tok_a if t in tok_b)
    score_a = intersection / len(tok_a)
    score_b = intersection / len(tok_b)
    return max(score_a, score_b)
