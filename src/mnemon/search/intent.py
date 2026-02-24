"""Query intent detection and intent-specific edge type weights."""

import re

WHY_PATTERN = re.compile(
    r'(?i)\b(why|reason|because|cause|motivation|rationale)\b')
WHEN_PATTERN = re.compile(
    r'(?i)\b(when|time|date|before|after|during|timeline|history|sequence)\b')
ENTITY_PATTERN = re.compile(
    r'(?i)\b(what is|who is|tell me about|describe|about)\b')

INTENT_WEIGHTS: dict[str, dict[str, float]] = {
    'WHY': {
        'causal': 0.70, 'temporal': 0.20,
        'entity': 0.05, 'semantic': 0.05,
        },
    'WHEN': {
        'temporal': 0.65, 'causal': 0.15,
        'entity': 0.10, 'semantic': 0.10,
        },
    'ENTITY': {
        'entity': 0.55, 'semantic': 0.30,
        'temporal': 0.05, 'causal': 0.10,
        },
    'GENERAL': {
        'temporal': 0.25, 'semantic': 0.25,
        'causal': 0.25, 'entity': 0.25,
        },
    }


def intent_from_string(s: str) -> str:
    """Parse a user-provided intent string into a valid intent value."""
    upper = s.strip().upper()
    if upper in {'WHY', 'WHEN', 'ENTITY', 'GENERAL'}:
        return upper
    raise ValueError(
        f'unknown intent {s!r}; valid: WHY, WHEN, ENTITY, GENERAL')


def detect_intent(query: str) -> str:
    """Analyze a query string and return the detected intent."""
    q = query.lower()
    why_score = len(WHY_PATTERN.findall(q))
    when_score = len(WHEN_PATTERN.findall(q))
    entity_score = len(ENTITY_PATTERN.findall(q))

    if why_score > when_score and why_score > entity_score and why_score > 0:
        return 'WHY'
    if (when_score > why_score and when_score > entity_score
            and when_score > 0):
        return 'WHEN'
    if entity_score > 0:
        return 'ENTITY'
    return 'GENERAL'


def get_weights(intent: str) -> dict[str, float]:
    """Return edge type weights for the given intent."""
    return INTENT_WEIGHTS.get(intent, INTENT_WEIGHTS['GENERAL'])
