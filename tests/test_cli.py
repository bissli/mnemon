"""Tests for mnemon.cli — Click CLI commands via CliRunner."""

import json
import pathlib

import pytest
from click.testing import CliRunner
from mnemon.cli import cli


@pytest.fixture
def runner(tmp_path):
    """CliRunner with --data-dir pointing to temp directory."""
    r = CliRunner()
    data_dir = str(tmp_path / 'mnemon_data')
    pathlib.Path(data_dir).mkdir(exist_ok=True, parents=True)
    return r, data_dir


@pytest.fixture(autouse=True)
def _no_ollama(monkeypatch):
    """Prevent CLI tests from making real HTTP requests to Ollama."""
    monkeypatch.setattr(
        'mnemon.embed.ollama.Client.available', lambda self: False)


def invoke(runner_tuple, args):
    """Helper to invoke CLI with data-dir."""
    r, data_dir = runner_tuple
    return r.invoke(cli, ['--data-dir', data_dir] + args)


def test_remember_basic(runner):
    """Store a basic insight."""
    result = invoke(runner, ['remember', 'Go uses SQLite', '--no-diff'])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data['action'] in {'added', 'updated'}
    assert data['content'] == 'Go uses SQLite'


def test_remember_with_flags(runner):
    """Store with category, importance, tags."""
    result = invoke(runner, [
        'remember', 'Use Docker', '--no-diff',
        '--cat', 'decision', '--imp', '4',
        '--tags', 'docker,deployment'])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data['category'] == 'decision'
    assert data['importance'] == 4


def test_remember_invalid_category(runner):
    """Invalid category is rejected."""
    result = invoke(runner, ['remember', 'test', '--cat', 'bogus'])
    assert result.exit_code != 0


def test_remember_invalid_importance(runner):
    """Importance outside 1-5 is rejected."""
    result = invoke(runner, ['remember', 'test', '--imp', '0'])
    assert result.exit_code != 0


def test_recall_basic(runner):
    """Recall after remembering."""
    invoke(runner, ['remember', 'Go uses SQLite for storage', '--no-diff'])
    result = invoke(runner, ['recall', 'Go SQLite'])
    assert result.exit_code == 0


def test_recall_basic_mode(runner):
    """Basic recall returns array."""
    invoke(runner, ['remember', 'Go uses SQLite', '--no-diff'])
    result = invoke(runner, ['recall', 'Go', '--basic'])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert isinstance(data, list)


def test_search_basic(runner):
    """Search returns scored results."""
    invoke(runner, ['remember', 'Go uses SQLite for storage', '--no-diff'])
    result = invoke(runner, ['search', 'Go SQLite'])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert isinstance(data, list)


def test_forget_basic(runner):
    """Forget an insight by ID."""
    result = invoke(runner, ['remember', 'to be forgotten', '--no-diff'])
    data = json.loads(result.output)
    iid = data['id']
    result = invoke(runner, ['forget', iid])
    assert result.exit_code == 0
    fdata = json.loads(result.output)
    assert fdata['status'] == 'deleted'


def test_store_list(runner):
    """Store list shows stores."""
    result = invoke(runner, ['store', 'list'])
    assert result.exit_code == 0


def test_store_create(runner):
    """Create a new store."""
    result = invoke(runner, ['store', 'create', 'test-store'])
    assert result.exit_code == 0
    assert 'Created' in result.output


def test_store_create_duplicate(runner):
    """Duplicate store name is rejected."""
    invoke(runner, ['store', 'create', 'dup'])
    result = invoke(runner, ['store', 'create', 'dup'])
    assert result.exit_code != 0


def test_store_set(runner):
    """Set active store."""
    invoke(runner, ['store', 'create', 'work'])
    result = invoke(runner, ['store', 'set', 'work'])
    assert result.exit_code == 0
    assert 'Active store' in result.output


def test_store_remove(runner):
    """Remove a non-active store."""
    invoke(runner, ['store', 'create', 'temp'])
    result = invoke(runner, ['store', 'remove', 'temp'])
    assert result.exit_code == 0
    assert 'Removed' in result.output


def test_status_basic(runner):
    """Status returns JSON."""
    invoke(runner, ['remember', 'test insight', '--no-diff'])
    result = invoke(runner, ['status'])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert 'total_insights' in data


def test_log_basic(runner):
    """Log shows recent operations."""
    invoke(runner, ['remember', 'test insight', '--no-diff'])
    result = invoke(runner, ['log'])
    assert result.exit_code == 0


def test_gc_suggest(runner):
    """GC suggest mode returns JSON."""
    invoke(runner, ['remember', 'test insight', '--no-diff'])
    result = invoke(runner, ['gc'])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert 'total_insights' in data


def test_viz_dot(runner):
    """Viz dot format renders."""
    invoke(runner, ['remember', 'test insight', '--no-diff'])
    result = invoke(runner, ['viz'])
    assert result.exit_code == 0
    assert 'digraph' in result.output
