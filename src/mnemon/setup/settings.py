"""JSON configuration management with JSON5 support."""

import json
import os
from pathlib import Path


def strip_json5(s: str) -> str:
    """Remove // line comments and trailing commas from JSON5 input."""
    result = []
    in_string = False
    escaped = False
    i = 0
    while i < len(s):
        ch = s[i]
        if escaped:
            result.append(ch)
            escaped = False
            i += 1
            continue
        if in_string:
            if ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            result.append(ch)
            i += 1
            continue
        if ch == '"':
            in_string = True
            result.append(ch)
            i += 1
            continue
        if ch == '/' and i + 1 < len(s) and s[i + 1] == '/':
            while i < len(s) and s[i] != '\n':
                i += 1
            continue
        if ch == ',':
            j = i + 1
            while j < len(s) and s[j] in ' \t\n\r':
                j += 1
            if j < len(s) and s[j] in ']})':
                i += 1
                continue
        result.append(ch)
        i += 1
    return ''.join(result)


def read_json_file(path: str) -> dict:
    """Read a JSON file into a dict. Returns empty dict if file doesn't exist."""
    try:
        data = Path(path).read_text()
    except (OSError, FileNotFoundError):
        return {}
    if not data:
        return {}
    cleaned = strip_json5(data)
    return json.loads(cleaned)


def write_json_file(path: str, data: dict) -> None:
    """Write a dict to a JSON file atomically via .tmp + rename."""
    content = json.dumps(data, indent=2) + '\n'
    Path(Path(path).parent).mkdir(mode=0o755, exist_ok=True, parents=True)
    tmp = path + '.tmp'
    Path(tmp).write_text(content)
    Path(tmp).replace(path)


def write_or_remove_json_file(path: str, data: dict) -> None:
    """Write the settings, or remove the file if the dict is empty."""
    if not data:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass
        return
    write_json_file(path, data)


def _contains_mnemon(v: object) -> bool:
    """Recursively check if any string value contains 'mnemon'."""
    if isinstance(v, str):
        return 'mnemon' in v
    if isinstance(v, dict):
        return any(_contains_mnemon(val) for val in v.values())
    if isinstance(v, list):
        return any(_contains_mnemon(item) for item in v)
    return False


def _filter_hook_array(arr: list) -> list:
    """Remove entries that reference mnemon from a hook event array."""
    return [entry for entry in arr if not _contains_mnemon(entry)]


def remove_claude_hooks(data: dict) -> None:
    """Remove all mnemon-related entries from Claude Code hooks."""
    hooks = data.get('hooks')
    if not isinstance(hooks, dict):
        return
    for key in ('UserPromptSubmit', 'Stop', 'SessionStart',
                'PreCompact', 'PreToolUse'):
        arr = hooks.get(key)
        if not isinstance(arr, list):
            continue
        filtered = _filter_hook_array(arr)
        if not filtered:
            hooks.pop(key, None)
        else:
            hooks[key] = filtered
    if not hooks:
        data.pop('hooks', None)


def add_claude_hooks_selective(
        data: dict, hooks_dir: str,
        remind: bool = False, nudge: bool = False,
        task_recall: bool = False) -> None:
    """Idempotently set mnemon hooks in Claude Code settings."""
    remove_claude_hooks(data)
    hooks = data.setdefault('hooks', {})

    prime_entry = {
        'hooks': [
            {
                'type': 'command',
                'command': os.path.join(hooks_dir, 'prime.sh'),
                },
            ],
        }
    session_arr = hooks.get('SessionStart', [])
    if not isinstance(session_arr, list):
        session_arr = []
    session_arr.append(prime_entry)
    hooks['SessionStart'] = session_arr

    if remind:
        remind_entry = {
            'hooks': [
                {
                    'type': 'command',
                    'command': os.path.join(
                        hooks_dir, 'user_prompt.sh'),
                    },
                ],
            }
        arr = hooks.get('UserPromptSubmit', [])
        if not isinstance(arr, list):
            arr = []
        arr.append(remind_entry)
        hooks['UserPromptSubmit'] = arr

    if nudge:
        nudge_entry = {
            'hooks': [
                {
                    'type': 'command',
                    'command': os.path.join(hooks_dir, 'stop.sh'),
                    },
                ],
            }
        arr = hooks.get('Stop', [])
        if not isinstance(arr, list):
            arr = []
        arr.append(nudge_entry)
        hooks['Stop'] = arr

    if task_recall:
        task_recall_entry = {
            'hooks': [
                {
                    'type': 'command',
                    'command': os.path.join(
                        hooks_dir, 'task_recall.sh'),
                    },
                ],
            'matcher': 'Task',
            }
        arr = hooks.get('PreToolUse', [])
        if not isinstance(arr, list):
            arr = []
        arr.append(task_recall_entry)
        hooks['PreToolUse'] = arr


MNEMON_PERMISSION = 'Bash(mnemon:*)'


def add_mnemon_permission(data: dict) -> None:
    """Add Bash(mnemon:*) to permissions allow list. Idempotent."""
    perms = data.setdefault('permissions', {})
    allow = perms.setdefault('allow', [])
    if MNEMON_PERMISSION not in allow:
        allow.append(MNEMON_PERMISSION)


def remove_mnemon_permission(data: dict) -> None:
    """Remove Bash(mnemon:*) from permissions allow list."""
    perms = data.get('permissions')
    if not isinstance(perms, dict):
        return
    allow = perms.get('allow')
    if not isinstance(allow, list):
        return
    try:
        allow.remove(MNEMON_PERMISSION)
    except ValueError:
        pass


def remove_if_empty(dir_path: str) -> None:
    """Remove a directory only if it exists and contains no entries."""
    try:
        entries = os.listdir(dir_path)
        if not entries:
            Path(dir_path).rmdir()
    except OSError:
        pass
