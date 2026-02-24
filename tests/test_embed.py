"""Tests for mnemon.embed — vector math and Ollama integration."""

import math

import pytest
from mnemon.embed.ollama import Client
from mnemon.embed.vector import cosine_similarity, deserialize_vector
from mnemon.embed.vector import serialize_vector
from tests.fixtures.ollama import ollama_client, ollama_endpoint  # noqa: F401

# --- Vector unit tests ---


def test_cosine_identical():
    """Identical vectors have similarity 1.0."""
    v = [1.0, 2.0, 3.0]
    assert abs(cosine_similarity(v, v) - 1.0) < 1e-9


def test_cosine_orthogonal():
    """Orthogonal vectors have similarity 0.0."""
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    assert abs(cosine_similarity(a, b)) < 1e-9


def test_cosine_opposite():
    """Opposite vectors have similarity -1.0."""
    a = [1.0, 2.0, 3.0]
    b = [-1.0, -2.0, -3.0]
    assert abs(cosine_similarity(a, b) - (-1.0)) < 1e-9


def test_cosine_different_length():
    """Mismatched dimensions return 0.0."""
    assert cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0


def test_cosine_empty():
    """Empty or None vectors return 0.0."""
    assert cosine_similarity([], []) == 0.0
    assert cosine_similarity(None, None) == 0.0


def test_cosine_zero_vector():
    """Zero vector returns 0.0."""
    assert cosine_similarity([0.0, 0.0, 0.0], [1.0, 2.0, 3.0]) == 0.0


def test_cosine_scaled():
    """Scaled vector has similarity 1.0."""
    a = [1.0, 2.0, 3.0]
    b = [2.0, 4.0, 6.0]
    assert abs(cosine_similarity(a, b) - 1.0) < 1e-9


def test_serialize_deserialize_roundtrip():
    """Verify float64 binary blob roundtrip."""
    original = [1.5, -2.7, 0.0, math.pi, float('inf')]
    blob = serialize_vector(original)
    restored = deserialize_vector(blob)
    assert len(restored) == len(original)
    for o, r in zip(original, restored):
        if math.isinf(o):
            assert math.isinf(r)
        else:
            assert o == r


def test_serialize_empty():
    """Empty/None vector produces empty bytes."""
    assert serialize_vector(None) == b''
    assert serialize_vector([]) == b''


def test_deserialize_empty():
    """Empty/None blob returns None."""
    assert deserialize_vector(None) is None
    assert deserialize_vector(b'') is None


def test_deserialize_invalid_length():
    """Blob with length not multiple of 8 returns None."""
    assert deserialize_vector(bytes(7)) is None


# --- Ollama integration tests ---


def test_available(ollama_client):
    """Ollama container is reachable."""
    assert ollama_client.available() is True


def test_embed_returns_floats(ollama_client):
    """Embedding returns a list of floats."""
    result = ollama_client.embed('hello world')
    assert isinstance(result, list)
    assert all(isinstance(x, float) for x in result)


def test_embed_dimension(ollama_client):
    """all-minilm:22m produces 384-dimensional embeddings."""
    result = ollama_client.embed('test sentence')
    assert len(result) == 384


def test_embed_similarity(ollama_client):
    """Similar texts score higher than dissimilar ones."""
    cat = ollama_client.embed('the cat sat on the mat')
    kitten = ollama_client.embed('the kitten rested on the rug')
    car = ollama_client.embed('the stock market crashed today')
    similar = cosine_similarity(cat, kitten)
    dissimilar = cosine_similarity(cat, car)
    assert similar > dissimilar


def test_embed_empty_raises(ollama_client):
    """Empty string raises RuntimeError."""
    with pytest.raises(RuntimeError):
        ollama_client.embed('')


def test_unavailable_endpoint(monkeypatch):
    """Client with bad endpoint reports unavailable."""
    monkeypatch.setenv('MNEMON_EMBED_ENDPOINT', 'http://localhost:1')
    monkeypatch.setenv('MNEMON_EMBED_MODEL', 'all-minilm:22m')
    bad_client = Client()
    assert bad_client.available() is False
