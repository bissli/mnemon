"""Vector serialization, deserialization, and cosine similarity."""

import math
import struct


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0

    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for i in range(len(a)):
        dot += a[i] * b[i]
        norm_a += a[i] * a[i]
        norm_b += b[i] * b[i]

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def serialize_vector(v: list[float]) -> bytes:
    """Encode float64 vector as little-endian binary blob."""
    if not v:
        return b''
    return struct.pack(f'<{len(v)}d', *v)


def deserialize_vector(b: bytes) -> list[float] | None:
    """Decode little-endian binary blob to float64 vector."""
    if not b:
        return None
    if len(b) % 8 != 0:
        return None
    count = len(b) // 8
    return list(struct.unpack(f'<{count}d', b))
