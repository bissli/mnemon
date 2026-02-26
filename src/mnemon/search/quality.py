"""Content quality signals for the remember pipeline."""

import re

TRANSIENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'i-[0-9a-f]{17}'), 'AWS instance ID'),
    (re.compile(r'\d+ resources? total'), 'resource count'),
    (re.compile(
        r'(?:all|every)\b.{0,30}\bverified', re.IGNORECASE),
        'verification receipt'),
    (re.compile(r'state (?:is )?clean', re.IGNORECASE),
        'state observation'),
    (re.compile(
        r'(?:deployed|completed|applied) via', re.IGNORECASE),
        'deployment receipt'),
    ]


def check_content_quality(content: str) -> list[str]:
    """Scan content for transient patterns and return warnings."""
    warnings = []
    for pattern, label in TRANSIENT_PATTERNS:
        if pattern.search(content):
            warnings.append(label)
    return warnings
