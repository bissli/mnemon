"""Ollama testcontainer fixtures."""

import pytest
from mnemon.embed.ollama import Client


@pytest.fixture(scope='session')
def ollama_endpoint():
    """Start Ollama container and pull all-minilm:22m (once per session)."""
    from testcontainers.ollama import OllamaContainer

    try:
        container = OllamaContainer(image='ollama/ollama:latest')
        container.start()
    except Exception:
        pytest.skip('Docker not available')
    container.pull_model('all-minilm:22m')
    yield container.get_endpoint()
    container.stop()


@pytest.fixture
def ollama_client(ollama_endpoint, monkeypatch):
    """Create Client pointed at the Ollama container."""
    monkeypatch.setenv('MNEMON_EMBED_ENDPOINT', ollama_endpoint)
    monkeypatch.setenv('MNEMON_EMBED_MODEL', 'all-minilm:22m')
    return Client()
