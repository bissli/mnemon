"""Claude Code integration: install, eject, and setup orchestration."""

import os
import shutil
from importlib.resources import files as pkg_files
from pathlib import Path

from mnemon.setup.detect import detect_environments, home_dir
from mnemon.setup.markdown import eject_memory_block
from mnemon.setup.prompt import confirm, detection_line, is_interactive
from mnemon.setup.prompt import select_multi, select_one, status_error
from mnemon.setup.prompt import status_ok, status_skipped, status_updated
from mnemon.setup.settings import add_claude_hooks_selective
from mnemon.setup.settings import add_mnemon_permission, read_json_file
from mnemon.setup.settings import remove_claude_hooks, remove_if_empty
from mnemon.setup.settings import remove_mnemon_permission, write_json_file
from mnemon.setup.settings import write_or_remove_json_file


def _asset_bytes(rel_path: str) -> bytes:
    """Read an embedded asset file."""
    return (pkg_files('mnemon.setup.assets')
            .joinpath(rel_path).read_bytes())


def write_prompt_files() -> str:
    """Write guide.md and skill.md to ~/.mnemon/prompt/."""
    prompt_dir = os.path.join(home_dir(), '.mnemon', 'prompt')
    Path(prompt_dir).mkdir(mode=0o755, exist_ok=True, parents=True)

    guide_path = os.path.join(prompt_dir, 'guide.md')
    Path(guide_path).write_bytes(_asset_bytes('claude/guide.md'))
    Path(guide_path).chmod(0o644)

    skill_path = os.path.join(prompt_dir, 'skill.md')
    Path(skill_path).write_bytes(_asset_bytes('claude/SKILL.md'))
    Path(skill_path).chmod(0o644)

    return prompt_dir


def claude_write_skill(config_dir: str) -> str:
    """Write the mnemon skill to the config dir."""
    skill_dir = os.path.join(config_dir, 'skills', 'mnemon')
    Path(skill_dir).mkdir(mode=0o755, exist_ok=True, parents=True)
    skill_path = os.path.join(skill_dir, 'SKILL.md')
    Path(skill_path).write_bytes(_asset_bytes('claude/SKILL.md'))
    Path(skill_path).chmod(0o644)
    return skill_path


def claude_write_hook(config_dir: str, filename: str, content: bytes) -> str:
    """Write a hook script to the hooks dir."""
    hooks_dir = os.path.join(config_dir, 'hooks', 'mnemon')
    Path(hooks_dir).mkdir(mode=0o755, exist_ok=True, parents=True)
    hook_path = os.path.join(hooks_dir, filename)
    Path(hook_path).write_bytes(content)
    Path(hook_path).chmod(0o755)
    return hook_path


def claude_register_hooks(config_dir: str,
                          remind: bool, nudge: bool,
                          task_recall: bool = False) -> str:
    """Register selected hooks in settings.json."""
    hooks_dir = os.path.join(config_dir, 'hooks', 'mnemon')
    settings_path = os.path.join(config_dir, 'settings.json')
    data = read_json_file(settings_path)
    add_claude_hooks_selective(
        data, hooks_dir,
        remind=remind, nudge=nudge,
        task_recall=task_recall)
    write_json_file(settings_path, data)
    return settings_path


def claude_eject(config_dir: str) -> list[Exception]:
    """Remove mnemon integration from the given Claude Code config dir."""
    errs: list[Exception] = []

    print(f'\nRemoving Claude Code integration ({config_dir})...')

    hooks_dir = os.path.join(config_dir, 'hooks', 'mnemon')
    try:
        shutil.rmtree(hooks_dir, ignore_errors=True)
        status_ok(1, 3, 'Hooks', hooks_dir + ' removed')
    except Exception as e:
        status_error(1, 3, 'Hooks', e)
        errs.append(e)
    remove_if_empty(os.path.join(config_dir, 'hooks'))

    settings_path = os.path.join(config_dir, 'settings.json')
    try:
        data = read_json_file(settings_path)
        remove_claude_hooks(data)
        remove_mnemon_permission(data)
        write_or_remove_json_file(settings_path, data)
        status_ok(2, 3, 'Settings', settings_path + ' cleaned')
    except Exception as e:
        status_error(2, 3, 'Settings', e)
        errs.append(e)

    skill_dir = os.path.join(config_dir, 'skills', 'mnemon')
    try:
        shutil.rmtree(skill_dir, ignore_errors=True)
        status_ok(3, 3, 'Skill', skill_dir + ' removed')
    except Exception as e:
        status_error(3, 3, 'Skill', e)
        errs.append(e)
    remove_if_empty(os.path.join(config_dir, 'skills'))

    remove_if_empty(config_dir)
    return errs


def _select_optional_hooks(auto_yes: bool) -> tuple[bool, bool, bool]:
    """Prompt user for which optional hooks to enable."""
    remind, nudge, task_recall = True, True, True
    if auto_yes or not is_interactive():
        return remind, nudge, task_recall

    opts = [
        ('Remind  \u2014 remind agent to recall & remember'
         ' on each message (recommended)'),
        'Nudge   \u2014 remind about memory on session end',
        ('Recall  \u2014 remind agent to recall before'
         ' delegating to sub-agents (recommended)'),
        ]
    defs = [True, True, True]
    choices = select_multi('Select hooks to enable', opts, defs)
    return choices[0], choices[1], choices[2]


def _init_default_store(data_dir: str) -> None:
    """Ensure the default store exists."""
    from mnemon.store.db import DB, store_dir, store_exists

    if not store_exists(data_dir, 'default'):
        sdir = store_dir(data_dir, 'default')
        db = DB(sdir)
        db.close()
        print(f'  Initialized default store at {sdir}')


def _install_claude_code(env: dict, auto_yes: bool,
                         use_global: bool,
                         data_dir: str) -> None:
    """Install mnemon into Claude Code."""
    config_dir = env['config_dir']

    if not use_global and not auto_yes and is_interactive():
        home = home_dir()
        local_dir = '.claude'
        global_dir = home + '/.claude'
        idx = select_one(
            'Install scope',
            [f'Local \u2014 this project only ({local_dir}/)',
             f'Global \u2014 all projects ({global_dir}/)'],
            0)
        config_dir = global_dir if idx == 1 else local_dir

    print(f'\nSetting up Claude Code ({config_dir})...')

    print('\n[1/3] Skill')
    path = claude_write_skill(config_dir)
    status_ok(0, 0, 'Skill', path)

    print('\n[2/3] Prompts')
    path = write_prompt_files()
    status_ok(0, 0, 'Prompts', path)

    path = claude_write_hook(
        config_dir, 'prime.sh', _asset_bytes('claude/prime.sh'))
    status_ok(0, 0, 'Hook: prime', path)

    print('\n[3/3] Optional hooks')
    remind, nudge, task_recall = _select_optional_hooks(auto_yes)

    if remind:
        path = claude_write_hook(
            config_dir, 'user_prompt.sh',
            _asset_bytes('claude/user_prompt.sh'))
        status_ok(0, 0, 'Hook: remind', path)
    if nudge:
        path = claude_write_hook(
            config_dir, 'stop.sh',
            _asset_bytes('claude/stop.sh'))
        status_ok(0, 0, 'Hook: nudge', path)
    if task_recall:
        path = claude_write_hook(
            config_dir, 'task_recall.sh',
            _asset_bytes('claude/task_recall.sh'))
        status_ok(0, 0, 'Hook: recall', path)

    path = claude_register_hooks(
        config_dir, remind=remind, nudge=nudge,
        task_recall=task_recall)
    status_updated(0, 0, 'Settings', path)

    add_perm = auto_yes or (
        is_interactive() and confirm(
            'Add Bash(mnemon:*) to settings.json allow-list?'
            ' (allows recall/remember without prompting)',
            default_yes=True))
    if add_perm:
        settings_path = os.path.join(config_dir, 'settings.json')
        data = read_json_file(settings_path)
        add_mnemon_permission(data)
        write_json_file(settings_path, data)
        status_ok(0, 0, 'Permission',
                  'Bash(mnemon:*) added to settings.json')
    else:
        status_skipped(0, 0, 'Permission',
                       'Bash(mnemon:*) — skipped')

    hook_names = ['prime']
    if remind:
        hook_names.append('remind')
    if nudge:
        hook_names.append('nudge')
    if task_recall:
        hook_names.append('recall')

    print()
    print('Setup complete!')
    print(f'  Hooks   {", ".join(hook_names)}')
    print('  Prompts ~/.mnemon/prompt/ (guide.md, skill.md)')
    print()
    print('Start a new Claude Code session to activate.')
    print('Edit ~/.mnemon/prompt/guide.md to customize behavior.')
    print("Run 'mnemon setup --eject' to remove.")

    _init_default_store(data_dir)


def _eject_markdown(file_path: str, prompt_text: str,
                    auto_yes: bool) -> None:
    """Optionally eject memory block from a markdown file."""
    if auto_yes:
        if eject_memory_block(file_path):
            print(f'  Memory guidance removed from {file_path}')
    elif is_interactive() and confirm(prompt_text, default_yes=True):
        if eject_memory_block(file_path):
            print(
                f'  Memory guidance removed from {file_path}')


def _eject_env(env: dict, auto_yes: bool) -> bool:
    """Eject mnemon from a single environment."""
    if env['name'] == 'claude-code':
        errs = claude_eject(env['config_dir'])
        _eject_markdown(
            'CLAUDE.md',
            'Remove memory guidance from ./CLAUDE.md?',
            auto_yes)
        return len(errs) > 0

    if env['name'] == 'openclaw':
        from mnemon.setup.openclaw import openclaw_eject
        errs = openclaw_eject(env['config_dir'])
        _eject_markdown(
            'AGENTS.md',
            'Remove memory guidance from ./AGENTS.md?',
            auto_yes)
        return len(errs) > 0

    return False


def run_setup(data_dir: str, target: str = '',
              eject: bool = False, auto_yes: bool = False,
              use_global: bool = False) -> None:
    """Main setup orchestrator called by cli.py."""
    if target and target not in {'claude-code', 'openclaw'}:
        raise SystemExit(
            f'invalid target {target!r}'
            ' (must be claude-code or openclaw)')

    envs = detect_environments(use_global)

    if eject:
        _run_eject_flow(
            envs, target=target, auto_yes=auto_yes)
        return
    _run_install_flow(
        envs, target=target, auto_yes=auto_yes,
        use_global=use_global, data_dir=data_dir)


def _run_install_flow(envs: list[dict], target: str,
                      auto_yes: bool, use_global: bool,
                      data_dir: str) -> None:
    """Install flow: detect, select, install."""
    if target:
        for env in envs:
            if env['name'] == target:
                _install_env(
                    env, auto_yes=auto_yes,
                    use_global=use_global, data_dir=data_dir)
                return
        raise SystemExit(f'unknown target {target!r}')

    print('Detecting LLM CLI environments...')
    print()

    detected = []
    for env in envs:
        detection_line(
            env['detected'], env['display'],
            env['version'], env['config_dir'])
        if env['detected']:
            detected.append(env)

    if not detected:
        print('\nNo supported LLM CLI environments detected.')
        print("Install Claude Code or OpenClaw,"
              " then run 'mnemon setup' again.")
        return

    if auto_yes:
        selected = detected
    elif is_interactive():
        options = [e['display'] for e in detected]
        idx = select_one('Select environment', options, 0)
        selected = [detected[idx]]
    else:
        selected = detected

    if not selected:
        print('\nNo environments selected.')
        return

    err_count = 0
    for env in selected:
        try:
            _install_env(
                env, auto_yes=auto_yes,
                use_global=use_global, data_dir=data_dir)
        except Exception:
            err_count += 1

    if err_count > 0:
        raise SystemExit(f'{err_count} error(s) during setup')


def _install_env(env: dict, auto_yes: bool,
                 use_global: bool, data_dir: str) -> None:
    """Install mnemon into a single environment."""
    if env['name'] == 'claude-code':
        _install_claude_code(
            env, auto_yes=auto_yes,
            use_global=use_global, data_dir=data_dir)
    elif env['name'] == 'openclaw':
        from mnemon.setup.openclaw import install_openclaw
        install_openclaw(
            env, auto_yes=auto_yes,
            use_global=use_global, data_dir=data_dir)


def _run_eject_flow(envs: list[dict], target: str,
                    auto_yes: bool) -> None:
    """Eject flow: detect, select, remove."""
    if target:
        for env in envs:
            if env['name'] == target:
                _eject_env(env, auto_yes)
                return
        raise SystemExit(f'unknown target {target!r}')

    print('Detecting LLM CLI environments...')
    print()

    installed = []
    for env in envs:
        detection_line(
            env['detected'], env['display'],
            env['version'], env['config_dir'])
        if env['detected']:
            installed.append(env)

    if not installed:
        print('\nNo environments detected.')
        return

    if auto_yes:
        selected = installed
    elif is_interactive():
        options = [e['display'] for e in installed]
        idx = select_one(
            'Select environment to remove', options, 0)
        selected = [installed[idx]]
    else:
        selected = installed

    if not selected:
        print('\nNo environments selected.')
        return

    err_count = 0
    for env in selected:
        if _eject_env(env, auto_yes):
            err_count += 1

    print()
    print('Done! All selected integrations removed.')

    if err_count > 0:
        raise SystemExit(f'{err_count} error(s) during eject')
