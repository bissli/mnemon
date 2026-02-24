"""OpenClaw integration: install, eject."""

import json
import os
import shutil
from importlib.resources import files as pkg_files
from pathlib import Path

import mnemon
from mnemon.setup.detect import home_dir
from mnemon.setup.prompt import is_interactive, select_multi, select_one
from mnemon.setup.prompt import status_error, status_ok, status_updated
from mnemon.setup.settings import remove_if_empty


def _asset_bytes(rel_path: str) -> bytes:
    """Read an embedded asset file."""
    return (pkg_files('mnemon.setup.assets')
            .joinpath(rel_path).read_bytes())


def openclaw_write_skill(config_dir: str) -> str:
    """Write the SKILL.md to the OpenClaw skills directory."""
    skill_dir = os.path.join(config_dir, 'skills', 'mnemon')
    Path(skill_dir).mkdir(mode=0o755, exist_ok=True, parents=True)
    skill_path = os.path.join(skill_dir, 'SKILL.md')
    Path(skill_path).write_bytes(
        _asset_bytes('openclaw/SKILL.md'))
    Path(skill_path).chmod(0o644)
    return skill_path


def openclaw_write_hook(config_dir: str) -> str:
    """Write the mnemon-prime internal hook."""
    hook_dir = os.path.join(
        config_dir, 'hooks', 'mnemon-prime')
    Path(hook_dir).mkdir(mode=0o755, exist_ok=True, parents=True)

    hook_md_path = os.path.join(hook_dir, 'HOOK.md')
    Path(hook_md_path).write_bytes(
        _asset_bytes('openclaw/hooks/mnemon-prime/HOOK.md'))
    Path(hook_md_path).chmod(0o644)

    handler_path = os.path.join(hook_dir, 'handler.js')
    Path(handler_path).write_bytes(
        _asset_bytes('openclaw/hooks/mnemon-prime/handler.js'))
    Path(handler_path).chmod(0o644)

    return hook_dir


def openclaw_write_plugin(config_dir: str, ver: str) -> str:
    """Write the mnemon plugin to the OpenClaw extensions directory."""
    plugin_dir = os.path.join(
        config_dir, 'extensions', 'mnemon')
    Path(plugin_dir).mkdir(mode=0o755, exist_ok=True, parents=True)

    manifest = _asset_bytes(
        'openclaw/plugin/openclaw.plugin.json')
    if ver and ver != 'dev':
        try:
            m = json.loads(manifest)
            m['version'] = ver
            manifest = (
                json.dumps(m, indent=2) + '\n').encode()
        except Exception:
            pass

    file_list = [
        ('package.json',
         _asset_bytes('openclaw/plugin/package.json')),
        ('openclaw.plugin.json', manifest),
        ('index.js',
         _asset_bytes('openclaw/plugin/index.js')),
        ]
    for name, data in file_list:
        fpath = os.path.join(plugin_dir, name)
        Path(fpath).write_bytes(data)
        Path(fpath).chmod(0o644)

    return plugin_dir


def openclaw_register_plugin(config_dir: str,
                             remind: bool, nudge: bool) -> str:
    """Add the mnemon plugin entry to openclaw.json."""
    cfg_path = os.path.join(config_dir, 'openclaw.json')

    try:
        data = Path(cfg_path).read_text()
        cfg = json.loads(data)
    except (OSError, FileNotFoundError, json.JSONDecodeError):
        cfg = {}

    plugins = cfg.get('plugins')
    if not isinstance(plugins, dict):
        plugins = {}
    entries = plugins.get('entries')
    if not isinstance(entries, dict):
        entries = {}

    entries['mnemon'] = {
        'enabled': True,
        'config': {
            'remind': remind,
            'nudge': nudge,
            },
        }
    plugins['entries'] = entries
    cfg['plugins'] = plugins

    content = json.dumps(cfg, indent=2) + '\n'
    Path(cfg_path).write_text(content)
    Path(cfg_path).chmod(0o600)

    return cfg_path


def openclaw_eject(config_dir: str) -> list[Exception]:
    """Remove mnemon skill, hook, and plugin from OpenClaw."""
    errs: list[Exception] = []

    print(f'\nRemoving OpenClaw integration ({config_dir})...')

    targets = [
        ('Skill',
         os.path.join(config_dir, 'skills', 'mnemon')),
        ('Hook',
         os.path.join(config_dir, 'hooks', 'mnemon-prime')),
        ('Plugin',
         os.path.join(config_dir, 'extensions', 'mnemon')),
        ]

    for i, (label, path) in enumerate(targets):
        try:
            shutil.rmtree(path, ignore_errors=True)
            status_ok(i + 1, len(targets), label,
                      path + ' removed')
        except Exception as e:
            status_error(i + 1, len(targets), label, e)
            errs.append(e)

    remove_if_empty(os.path.join(config_dir, 'skills'))
    remove_if_empty(os.path.join(config_dir, 'hooks'))
    remove_if_empty(os.path.join(config_dir, 'extensions'))

    cfg_path = os.path.join(config_dir, 'openclaw.json')
    try:
        data = Path(cfg_path).read_text()
        cfg = json.loads(data)
        plugins = cfg.get('plugins', {})
        entries = plugins.get('entries', {})
        if isinstance(entries, dict):
            entries.pop('mnemon', None)
            plugins['entries'] = entries
            cfg['plugins'] = plugins
            content = json.dumps(cfg, indent=2) + '\n'
            Path(cfg_path).write_text(content)
            Path(cfg_path).chmod(0o600)
    except Exception:
        pass

    remove_if_empty(config_dir)
    return errs


def _select_openclaw_hooks(
        auto_yes: bool) -> tuple[bool, bool]:
    """Prompt user for which plugin hooks to enable."""
    remind, nudge = True, True
    if auto_yes or not is_interactive():
        return remind, nudge

    opts = [
        ('Remind  \u2014 recall relevant memories + remind'
         ' agent on each message (recommended)'),
        ('Nudge   \u2014 suggest remember sub-agent'
         ' after each reply'),
        ]
    defs = [True, True]
    choices = select_multi(
        'Select plugin hooks to enable', opts, defs)
    return choices[0], choices[1]


def install_openclaw(env: dict, auto_yes: bool,
                     use_global: bool,
                     data_dir: str) -> None:
    """Install mnemon into OpenClaw."""
    from mnemon.setup.claude import _init_default_store, write_prompt_files

    config_dir = env['config_dir']

    if not use_global and not auto_yes and is_interactive():
        home = home_dir()
        local_dir = '.openclaw'
        global_dir = home + '/.openclaw'
        idx = select_one(
            'Install scope',
            [f'Global \u2014 all projects ({global_dir}/)',
             f'Local  \u2014 this project only ({local_dir}/)'],
            0)
        config_dir = local_dir if idx == 1 else global_dir

    print(f'\nSetting up OpenClaw ({config_dir})...')

    print('\n[1/4] Skill')
    path = openclaw_write_skill(config_dir)
    status_ok(0, 0, 'Skill', path)

    print('\n[2/4] Prompts')
    path = write_prompt_files()
    status_ok(0, 0, 'Prompts', path)

    print('\n[3/4] Hook')
    path = openclaw_write_hook(config_dir)
    status_ok(0, 0, 'Hook: prime', path)

    print('\n[4/4] Plugin')
    remind, nudge = _select_openclaw_hooks(auto_yes)

    ver = mnemon.__version__
    path = openclaw_write_plugin(config_dir, ver)
    status_ok(0, 0, 'Plugin', path)

    path = openclaw_register_plugin(
        config_dir, remind=remind, nudge=nudge)
    status_updated(0, 0, 'Config', path)

    hook_names = ['prime']
    if remind:
        hook_names.append('remind')
    if nudge:
        hook_names.append('nudge')

    print()
    print('Setup complete!')
    print(f'  Skill   {config_dir}/skills/mnemon/SKILL.md')
    print(f'  Hook    {config_dir}/hooks/mnemon-prime/'
          ' (agent:bootstrap)')
    print(f'  Plugin  {config_dir}/extensions/mnemon/'
          f' (hooks: {", ".join(hook_names)})')
    print('  Prompts ~/.mnemon/prompt/ (guide.md, skill.md)')
    print()
    print('Restart the OpenClaw gateway to activate.')
    print('Edit ~/.mnemon/prompt/guide.md to customize behavior.')
    print("Run 'mnemon setup --eject' to remove.")

    _init_default_store(data_dir)
