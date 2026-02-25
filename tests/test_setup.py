"""Tests for mnemon.setup — settings, markdown, detection."""

import json
import os
import pathlib
import subprocess

from mnemon.setup.claude import claude_register_hooks
from mnemon.setup.markdown import eject_memory_block
from mnemon.setup.settings import add_claude_hooks_selective
from mnemon.setup.settings import add_mnemon_permission, read_json_file
from mnemon.setup.settings import remove_claude_hooks
from mnemon.setup.settings import remove_mnemon_permission, strip_json5
from mnemon.setup.settings import write_json_file


def test_strip_json5_line_comments():
    """Remove // line comments."""
    s = '{"key": "value" // comment\n}'
    assert json.loads(strip_json5(s)) == {'key': 'value'}


def test_strip_json5_comment_in_string():
    """// inside quotes is preserved."""
    s = '{"url": "https://example.com"}'
    assert json.loads(strip_json5(s)) == {'url': 'https://example.com'}


def test_strip_json5_trailing_comma():
    """Trailing commas before closing brackets are removed."""
    s = '{"a": 1, "b": 2,}'
    assert json.loads(strip_json5(s)) == {'a': 1, 'b': 2}


def test_strip_json5_trailing_comma_array():
    """Trailing commas in arrays are removed."""
    s = '[1, 2, 3,]'
    assert json.loads(strip_json5(s)) == [1, 2, 3]


def test_read_json_missing_file(tmp_path):
    """Missing file returns empty dict."""
    result = read_json_file(str(tmp_path / 'nope.json'))
    assert result == {}


def test_read_json_with_comments(tmp_path):
    """JSON5 with comments parses correctly."""
    p = tmp_path / 'test.json'
    p.write_text('{\n  "key": "val" // comment\n}')
    result = read_json_file(str(p))
    assert result == {'key': 'val'}


def test_write_json_atomic(tmp_path):
    """Write uses .tmp + rename pattern."""
    p = str(tmp_path / 'out.json')
    write_json_file(p, {'hello': 'world'})
    assert pathlib.Path(p).exists()
    assert not pathlib.Path(p + '.tmp').exists()
    data = json.loads(pathlib.Path(p).open().read())
    assert data == {'hello': 'world'}


def test_remove_claude_hooks():
    """Remove mnemon hooks from settings dict."""
    data = {
        'hooks': {
            'SessionStart': [
                {'hooks': [{'type': 'command', 'command': '/path/to/mnemon/prime.sh'}]},
                {'hooks': [{'type': 'command', 'command': '/other/tool.sh'}]},
            ],
        },
    }
    remove_claude_hooks(data)
    assert len(data['hooks']['SessionStart']) == 1
    assert 'mnemon' not in str(data['hooks']['SessionStart'][0])


def test_add_claude_hooks_selective():
    """Add hooks idempotently with selective options."""
    data = {}
    add_claude_hooks_selective(data, '/hooks/dir', remind=True, nudge=False)
    hooks = data['hooks']
    assert 'SessionStart' in hooks
    assert 'UserPromptSubmit' in hooks
    assert 'Stop' not in hooks


def test_eject_memory_block(tmp_path):
    """Remove markers and content between them."""
    p = tmp_path / 'test.md'
    p.write_text('before\n<!-- mnemon:start -->\nstuff\n<!-- mnemon:end -->\nafter\n')
    assert eject_memory_block(str(p)) is True
    content = p.read_text()
    assert 'mnemon' not in content
    assert 'before' in content
    assert 'after' in content


def test_eject_memory_block_empty_file(tmp_path):
    """File deleted if empty after marker removal."""
    p = tmp_path / 'test.md'
    p.write_text('<!-- mnemon:start -->\nstuff\n<!-- mnemon:end -->\n')
    assert eject_memory_block(str(p)) is True
    assert not p.exists()


def test_eject_memory_block_no_markers(tmp_path):
    """No markers returns False."""
    p = tmp_path / 'test.md'
    p.write_text('no markers here')
    assert eject_memory_block(str(p)) is False


def test_add_claude_hooks_with_task_recall():
    """task_recall=True produces PreToolUse entry with Task matcher."""
    data = {}
    add_claude_hooks_selective(
        data, '/hooks/dir', task_recall=True)
    hooks = data['hooks']
    assert 'PreToolUse' in hooks
    entries = hooks['PreToolUse']
    assert len(entries) == 1
    assert entries[0]['matcher'] == 'Task'
    assert entries[0]['hooks'][0]['command'].endswith(
        'task_recall.sh')


def test_add_claude_hooks_task_recall_default_false():
    """Default (no task_recall) does NOT create PreToolUse."""
    data = {}
    add_claude_hooks_selective(data, '/hooks/dir')
    hooks = data['hooks']
    assert 'PreToolUse' not in hooks


def test_remove_claude_hooks_cleans_pretooluse():
    """Mnemon PreToolUse entries removed, non-mnemon preserved."""
    data = {
        'hooks': {
            'PreToolUse': [
                {
                    'hooks': [{'type': 'command',
                               'command': '/mnemon/task_recall.sh'}],
                    'matcher': 'Task',
                    },
                {
                    'hooks': [{'type': 'command',
                               'command': '/other/enforce.py'}],
                    'matcher': 'Bash',
                    },
                ],
            },
        }
    remove_claude_hooks(data)
    entries = data['hooks']['PreToolUse']
    assert len(entries) == 1
    assert entries[0]['matcher'] == 'Bash'


def test_remove_claude_hooks_preserves_non_mnemon_pretooluse():
    """PreToolUse with only non-mnemon entries is untouched."""
    data = {
        'hooks': {
            'PreToolUse': [
                {
                    'hooks': [{'type': 'command',
                               'command': '/other/lint.sh'}],
                    'matcher': 'Bash',
                    },
                ],
            },
        }
    remove_claude_hooks(data)
    entries = data['hooks']['PreToolUse']
    assert len(entries) == 1
    assert entries[0]['matcher'] == 'Bash'


def test_add_claude_hooks_appends_to_existing_pretooluse():
    """task_recall appends to existing PreToolUse array."""
    data = {
        'hooks': {
            'PreToolUse': [
                {
                    'hooks': [{'type': 'command',
                               'command': '/other/enforce.py'}],
                    'matcher': 'Bash',
                    },
                ],
            },
        }
    add_claude_hooks_selective(
        data, '/hooks/dir', task_recall=True)
    entries = data['hooks']['PreToolUse']
    assert len(entries) == 2
    matchers = {e['matcher'] for e in entries}
    assert matchers == {'Bash', 'Task'}


def test_add_mnemon_permission():
    """Adds Bash(mnemon:*) to allow list. Idempotent."""
    data = {}
    add_mnemon_permission(data)
    assert 'Bash(mnemon:*)' in data['permissions']['allow']
    add_mnemon_permission(data)
    assert data['permissions']['allow'].count(
        'Bash(mnemon:*)') == 1


def test_add_mnemon_permission_existing_allow():
    """Appends without disturbing existing entries."""
    data = {'permissions': {'allow': ['Bash(git:*)']}}
    add_mnemon_permission(data)
    allow = data['permissions']['allow']
    assert allow == ['Bash(git:*)', 'Bash(mnemon:*)']


def test_remove_mnemon_permission():
    """Removes Bash(mnemon:*), preserves others."""
    data = {
        'permissions': {
            'allow': ['Bash(git:*)', 'Bash(mnemon:*)'],
            },
        }
    remove_mnemon_permission(data)
    assert data['permissions']['allow'] == ['Bash(git:*)']


def test_remove_mnemon_permission_missing():
    """No-op when Bash(mnemon:*) not present."""
    data = {'permissions': {'allow': ['Bash(git:*)']}}
    remove_mnemon_permission(data)
    assert data['permissions']['allow'] == ['Bash(git:*)']


def test_register_hooks_no_permission(tmp_path):
    """claude_register_hooks() does not add Bash(mnemon:*) to settings."""
    config_dir = str(tmp_path / '.claude')
    hooks_dir = os.path.join(config_dir, 'hooks', 'mnemon')
    pathlib.Path(hooks_dir).mkdir(parents=True)
    claude_register_hooks(config_dir, remind=True, nudge=True,
                          task_recall=True)
    data = read_json_file(os.path.join(config_dir, 'settings.json'))
    allow = data.get('permissions', {}).get('allow', [])
    assert 'Bash(mnemon:*)' not in allow


def test_add_claude_hooks_with_compact():
    """compact=True produces PreCompact entry."""
    data = {}
    add_claude_hooks_selective(
        data, '/hooks/dir', compact=True)
    hooks = data['hooks']
    assert 'PreCompact' in hooks
    entries = hooks['PreCompact']
    assert len(entries) == 1
    assert entries[0]['hooks'][0]['command'].endswith(
        'compact.sh')


def test_add_claude_hooks_compact_default_false():
    """Default (no compact) does NOT create PreCompact."""
    data = {}
    add_claude_hooks_selective(data, '/hooks/dir')
    hooks = data['hooks']
    assert 'PreCompact' not in hooks


def test_compact_hook_script(tmp_path):
    """Compact hook writes flag file with session info."""
    from importlib.resources import files as pkg_files
    script = str(
        pkg_files('mnemon.setup.assets')
        .joinpath('claude/compact.sh'))

    result = subprocess.run(
        ['bash', script],
        check=False, input='{"session_id": "test-abc-123", "trigger": "manual"}',
        capture_output=True, text=True,
        env={**os.environ, 'HOME': str(tmp_path)})
    assert result.returncode == 0

    flag = tmp_path / '.mnemon' / 'compact' / 'test-abc-123.json'
    assert flag.exists()
    data = json.loads(flag.read_text())
    assert data['trigger'] == 'manual'
    assert 'ts' in data


def test_compact_hook_script_no_session(tmp_path):
    """Compact hook writes no flag when session_id is missing."""
    from importlib.resources import files as pkg_files
    script = str(
        pkg_files('mnemon.setup.assets')
        .joinpath('claude/compact.sh'))

    result = subprocess.run(
        ['bash', script],
        check=False, input='{"trigger": "auto"}',
        capture_output=True, text=True,
        env={**os.environ, 'HOME': str(tmp_path)})
    assert result.returncode == 0

    compact_dir = tmp_path / '.mnemon' / 'compact'
    assert not compact_dir.exists()


def test_prime_hook_compact_source(tmp_path):
    """Prime hook outputs recall instruction on compact source."""
    from importlib.resources import files as pkg_files
    script = str(
        pkg_files('mnemon.setup.assets')
        .joinpath('claude/prime.sh'))

    compact_dir = tmp_path / '.mnemon' / 'compact'
    compact_dir.mkdir(parents=True)
    flag = compact_dir / 'sess-42.json'
    flag.write_text('{"trigger":"manual","ts":"2026-01-01T00:00:00Z"}')

    result = subprocess.run(
        ['bash', script],
        check=False, input='{"source": "compact", "session_id": "sess-42"}',
        capture_output=True, text=True,
        env={**os.environ, 'HOME': str(tmp_path)})
    assert result.returncode == 0
    assert 'compacted' in result.stdout
    assert 'manual' in result.stdout
    assert 'recall' in result.stdout.lower()
    assert flag.exists()


def test_prime_hook_compact_no_flag(tmp_path):
    """Prime hook outputs recall instruction even without flag file."""
    from importlib.resources import files as pkg_files
    script = str(
        pkg_files('mnemon.setup.assets')
        .joinpath('claude/prime.sh'))

    result = subprocess.run(
        ['bash', script],
        check=False, input='{"source": "compact", "session_id": "no-flag"}',
        capture_output=True, text=True,
        env={**os.environ, 'HOME': str(tmp_path)})
    assert result.returncode == 0
    assert 'compacted' in result.stdout
    assert 'auto' in result.stdout


def test_prime_hook_normal_source(tmp_path):
    """Prime hook does NOT output recall instruction on normal startup."""
    from importlib.resources import files as pkg_files
    script = str(
        pkg_files('mnemon.setup.assets')
        .joinpath('claude/prime.sh'))

    result = subprocess.run(
        ['bash', script],
        check=False, input='{"source": "startup"}',
        capture_output=True, text=True,
        env={**os.environ, 'HOME': str(tmp_path)})
    assert result.returncode == 0
    assert 'compacted' not in result.stdout


def test_stop_hook_script():
    """Stop hook outputs JSON block when inactive, silent when active."""
    from importlib.resources import files as pkg_files
    script = str(
        pkg_files('mnemon.setup.assets')
        .joinpath('claude/stop.sh'))

    result_inactive = subprocess.run(
        ['bash', script],
        check=False, input='{"stop_hook_active": false}',
        capture_output=True, text=True)
    assert result_inactive.returncode == 0
    output = json.loads(result_inactive.stdout.strip())
    assert output['decision'] == 'block'
    assert 'mnemon' in output['reason'].lower()

    result_active = subprocess.run(
        ['bash', script],
        check=False, input='{"stop_hook_active": true}',
        capture_output=True, text=True)
    assert result_active.returncode == 0
    assert result_active.stdout.strip() == ''
