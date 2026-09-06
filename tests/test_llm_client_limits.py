"""Per-role LLM client output-token and timeout budgets.

The recall hot path (`fast`) stays tight; the worker roles
(`slow_canonical`, `slow_metadata`) emit JSON that scales with input
size and must not truncate large insights, so they get a larger token
budget and a longer read timeout.
"""

import pytest
from memman.llm import usage as llm_usage
from memman.llm.client import MemmanLLMClient, get_llm_client, reset_role_cache


def test_fast_role_keeps_tight_budget():
    """Recall hot-path role keeps the small token budget and short timeout.
    """
    reset_role_cache()
    client = get_llm_client('fast')
    assert client.max_tokens == 1024
    assert client.timeout == 10.0


def test_slow_canonical_role_gets_large_budget():
    """Canonical-rewrite role gets headroom so big inputs are not truncated.
    """
    reset_role_cache()
    client = get_llm_client('slow_canonical')
    assert client.max_tokens >= 4096
    assert client.timeout >= 60.0


def test_slow_metadata_role_gets_large_budget():
    """Enrichment role gets headroom so big inputs are not truncated.
    """
    reset_role_cache()
    client = get_llm_client('slow_metadata')
    assert client.max_tokens >= 4096
    assert client.timeout >= 60.0


class _RecordingSession:
    """An HTTP session that records every request body and answers 200."""

    def __init__(self):
        self.bodies = []

    def post(self, url, headers, json, timeout):
        self.bodies.append(json)
        return _OkResponse()


class _OkResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {'choices': [{'message': {'content': '{}'}}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}


@pytest.mark.no_mock_llm
def test_complete_honors_a_per_call_max_tokens(monkeypatch):
    """Verify `max_tokens` on `complete` overrides the role ceiling for that call.

    Mutation: ignoring the keyword and always sending the role ceiling,
        so the reconciler's larger budget never reaches the request.
    Oracle: the recorded request bodies: the role ceiling without the
        keyword, the override with it.
    """
    session = _RecordingSession()
    monkeypatch.setattr('memman.llm.client.get_session', lambda name: session)
    client = MemmanLLMClient('https://llm.example', 'key', 'model', max_tokens=4096)

    client.complete('s', 'u', stage=llm_usage.STAGE_RECONCILIATION)
    client.complete('s', 'u', stage=llm_usage.STAGE_RECONCILIATION, max_tokens=8192)

    assert [b['max_tokens'] for b in session.bodies] == [4096, 8192]
