"""Ollama HTTP client for embedding generation."""

import logging
import os

import httpx

logger = logging.getLogger('mnemon')

DEFAULT_MODEL = 'nomic-embed-text'
DEFAULT_ENDPOINT = 'http://localhost:11434'


class Client:
    """HTTP client for Ollama embedding API."""

    def __init__(self) -> None:
        self.endpoint = os.environ.get(
            'MNEMON_EMBED_ENDPOINT', DEFAULT_ENDPOINT)
        self.model = os.environ.get(
            'MNEMON_EMBED_MODEL', DEFAULT_MODEL)

    def available(self) -> bool:
        """Check if Ollama server is reachable and model is pulled."""
        try:
            resp = httpx.get(
                f'{self.endpoint}/api/tags', timeout=2.0)
            if resp.status_code != 200:
                return False
            models = resp.json().get('models', [])
            base = self.model.split(':')[0]
            return any(
                m.get('name', '').split(':')[0] == base
                for m in models)
        except Exception:
            return False

    def embed(self, text: str) -> list[float]:
        """Generate embedding for text via Ollama API."""
        resp = httpx.post(
            f'{self.endpoint}/api/embed',
            json={'model': self.model, 'input': text},
            timeout=30.0)
        if resp.status_code != 200:
            raise RuntimeError(
                f'ollama returned status {resp.status_code}')
        data = resp.json()
        embeddings = data.get('embeddings', [])
        if not embeddings or not embeddings[0]:
            raise RuntimeError('empty embedding returned')
        return embeddings[0]

    def unavailable_message(self) -> str:
        """Return error message when Ollama is not available."""
        return (
            f'Ollama not available at {self.endpoint}'
            f' — install with: brew install ollama'
            f' && ollama pull {self.model}')
