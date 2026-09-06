"""Live LLM tests for updated FACT_EXTRACTION_SYSTEM prompt.

Verifies that the slow_canonical model (currently Sonnet) produces
correct importance and category assignments after prompt quality
fixes. Run in isolation:

    pytest tests/test_prompt_live.py --live

These tests require MEMMAN_OPENROUTER_API_KEY and make real API calls.
"""

import pytest
from memman.llm.client import get_llm_client
from memman.llm.extract import extract_facts

pytestmark = pytest.mark.skipif(
    'not config.getoption("--live")',
    reason='requires --live for real LLM calls')


@pytest.fixture
def llm_client():
    """LLM client for live tests.

    Function-scoped so the autouse `_isolate_env` fixture (which seeds
    the env file with `--live` real_secrets merged on top) has already
    run before this fixture builds the client.
    """
    from memman.llm.client import reset_role_cache
    reset_role_cache()
    return get_llm_client('slow_canonical')


class TestCategoryAccuracy:
    """Prompt produces correct category assignments."""

    def test_formula_is_fact(self, llm_client):
        """Formula input categorized as fact, not decision."""
        facts = extract_facts(
            llm_client,
            'Gamma PnL = 50 * gamma * r^2 using decimal returns')
        assert len(facts) >= 1
        assert facts[0]['category'] == 'fact'

    def test_api_behavior_is_fact(self, llm_client):
        """API behavior description categorized as fact."""
        facts = extract_facts(
            llm_client,
            'The Voyage API returns 512-dimensional embeddings '
            'and accepts batch sizes up to 128 texts per request')
        assert len(facts) >= 1
        assert facts[0]['category'] == 'fact'

    def test_code_pattern_is_fact(self, llm_client):
        """Code pattern description categorized as fact."""
        facts = extract_facts(
            llm_client,
            'SQLite json_each() extracts array elements into rows '
            'for querying JSON arrays stored in text columns')
        assert len(facts) >= 1
        assert facts[0]['category'] == 'fact'

    def test_explicit_decision_is_decision(self, llm_client):
        """Explicit choice of X over Y categorized as decision."""
        facts = extract_facts(
            llm_client,
            'Chose SQLite over PostgreSQL because the embedded '
            'serverless model avoids a separate database process')
        assert len(facts) >= 1
        categories = [f['category'] for f in facts]
        assert 'decision' in categories

    def test_user_preference_is_preference(self, llm_client):
        """User preference categorized as preference."""
        facts = extract_facts(
            llm_client,
            'I prefer single-line docstrings over multi-line '
            'for simple functions')
        assert len(facts) >= 1
        categories = [f['category'] for f in facts]
        assert 'preference' in categories

    def test_data_characteristic_is_fact(self, llm_client):
        """Data characteristic categorized as fact, not decision."""
        facts = extract_facts(
            llm_client,
            'SOFR futures settle at 100 minus the annualized rate '
            'and trade in quarter-point increments')
        assert len(facts) >= 1
        assert facts[0]['category'] == 'fact'


class TestSkipBehavior:
    """Prompt correctly skips trivial content."""

    def test_greeting_skipped(self, llm_client):
        """Greeting produces empty facts with skip_reason."""
        facts = extract_facts(llm_client, 'Hi there')
        assert facts == []

    def test_technical_content_not_skipped(self, llm_client):
        """Technical content is never skipped."""
        facts = extract_facts(
            llm_client,
            'Redis SCAN cursor iterates without blocking the server')
        assert len(facts) >= 1


class TestEntityExtraction:
    """Prompt extracts named entities correctly."""

    def test_entities_from_formula(self, llm_client):
        """Formula input produces relevant entities."""
        facts = extract_facts(
            llm_client,
            'Gamma PnL = 50 * gamma * r^2 using decimal returns')
        assert len(facts) >= 1
        all_entities = []
        for f in facts:
            all_entities.extend(e.lower() for e in f['entities'])
        assert any('gamma' in e or 'pnl' in e for e in all_entities)

    def test_entities_from_tool_comparison(self, llm_client):
        """Tool comparison extracts both tool names."""
        facts = extract_facts(
            llm_client,
            'Chose FastAPI over Flask for async support')
        assert len(facts) >= 1
        all_entities = []
        for f in facts:
            all_entities.extend(e.lower() for e in f['entities'])
        assert any('fastapi' in e for e in all_entities)
        assert any('flask' in e for e in all_entities)
