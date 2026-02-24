# Mnemon — Project Guidelines

## Development

- **Install (dev)**: `make dev` (editable install with dev deps)
- **Install (prod)**: `make install` (isolated venv at ~/.local/share/mnemon/venv)
- **Test**: `make test`
- **E2E**: `make e2e`
- **Dependencies**: click, httpx (runtime); pytest (dev)
- **Optional**: Ollama with `nomic-embed-text` for embedding support

## Structure

- Source: `src/mnemon/`
- Tests: `tests/`
- Entry point: `mnemon.cli:cli`
- Package: Poetry (`pyproject.toml`)
