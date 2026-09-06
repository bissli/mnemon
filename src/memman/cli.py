"""Click CLI for memman.

This module is the entry point and argument-parsing surface only. Core
write-path orchestration lives in `memman.pipeline.remember`. Storage,
graph, search, embed, and LLM primitives live under their own packages.
"""

import json
import logging
import logging.handlers
import math
import os
import pathlib
import re
import sqlite3
import sys
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Self
from urllib.parse import quote

import click
import memman
from memman import config
from memman.store import factory
from memman.store.db import default_data_dir, open_db, portable_store_name
from memman.store.db import read_active, store_dir, store_exists
from memman.store.db import valid_store_name, write_active
from memman.store.errors import BackendError
from memman.store.factory import known_backends, list_stores

_BACKEND_CHOICES = sorted(known_backends())

from memman.embed import SUPPORTED_EMBED_PROVIDERS as _EMBED_PROVIDER_CHOICES
from memman.store.model import VALID_CATEGORIES, VALID_EDGE_TYPES, Edge
from memman.store.model import Insight, format_timestamp
from memman.store.model import insight_to_brief_dict, insight_to_full_dict
from memman.store.sqlite import open_ro_db
from tqdm import tqdm

logger = logging.getLogger('memman')

_LOG_FORMAT = '%(asctime)s %(levelname)s %(name)s: %(message)s'
_WORKER_LOG_MAX_BYTES = 5 * 1024 * 1024
_WORKER_LOG_BACKUPS = 3


def _configure_logging(data_dir: str, verbose: bool, debug: bool) -> None:
    """Configure the memman logger once per process.

    Runs on every CLI invocation including `memman install` (before
    the env file exists). The literal `'WARNING'` fall-through must
    equal `INSTALL_DEFAULTS[LOG_LEVEL]`; a unit test enforces that.

    Notes
    -----
    - Levels are PER HANDLER, not on the logger alone. The stream
      handler carries the configured level, so what an interactive
      caller sees is unchanged. Under the worker the file handler
      takes DEBUG and the logger is opened to DEBUG to feed it, which
      is what preserves a stack the stream must never print.
    - Raising the CLI seam to `logger.exception` would be the wrong
      way to keep that stack: the stream handler is attached
      unconditionally, so an ERROR-level record prints its traceback
      to interactive users and undoes the clean one-line exit.
    - Worker DEBUG volume is bounded by rotation, not by judgement:
      `_WORKER_LOG_MAX_BYTES` x (`_WORKER_LOG_BACKUPS` + 1) caps
      `logs/memman.log` at 20 MB -- the live file plus three backups.
      The budget is shared with routine drain DEBUG traffic, since the
      logger opens the whole `memman` tree, so a stack can rotate away
      while the pointer naming it sits in the unrotated `enrich.err`.
    """
    if debug:
        level = logging.DEBUG
    elif verbose:
        level = logging.INFO
    else:
        raw = config.get(config.LOG_LEVEL) or 'WARNING'
        level = getattr(logging, raw.upper(), logging.WARNING)

    logger.setLevel(level)

    stream = next(
        (h for h in logger.handlers
         if isinstance(h, logging.StreamHandler)
         and not isinstance(h, logging.FileHandler)
         and getattr(h, '_memman', False)),
        None)
    if stream is None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter(_LOG_FORMAT))
        stream._memman = True
        logger.addHandler(stream)
    stream.setLevel(level)

    if config.is_worker():
        handler = next(
            (h for h in logger.handlers
             if isinstance(h, logging.handlers.RotatingFileHandler)
             and getattr(h, '_memman', False)),
            None)
        if handler is None:
            log_dir = pathlib.Path(data_dir) / 'logs'
            log_dir.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                log_dir / 'memman.log',
                maxBytes=_WORKER_LOG_MAX_BYTES,
                backupCount=_WORKER_LOG_BACKUPS)
            handler.setFormatter(logging.Formatter(_LOG_FORMAT))
            handler._memman = True
            logger.addHandler(handler)
        handler.setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)


def _json_out(obj: object) -> None:
    """Write JSON to stdout with 2-space indent, sorted keys."""
    click.echo(json.dumps(obj, indent=2, sort_keys=True))


def _require_started(action: str) -> None:
    """Reject the current CLI invocation when the scheduler is stopped.

    Single gate for write-producing commands. When the scheduler is
    stopped, memman is recall-only — every write returns exit 1 with a
    fixed message that points the operator at `memman scheduler start`.
    """
    from memman.setup.scheduler import STATE_STOPPED, read_state
    if read_state() == STATE_STOPPED:
        raise click.ClickException(
            f"Scheduler is stopped; cannot {action}."
            " Run 'memman scheduler start' to enable.")


def _require_stopped(action: str) -> None:
    """Reject the current CLI invocation when the scheduler is started.

    Inverse of `_require_started`. Used by `memman embed reembed`,
    which cannot run while the worker may be claiming queued
    `remember` rows mid-sweep.
    """
    from memman.setup.scheduler import STATE_STOPPED, read_state
    if read_state() != STATE_STOPPED:
        raise click.ClickException(
            f"Scheduler is started; cannot {action}."
            " Run 'memman scheduler stop' first.")


def _resolve_store_name(data_dir: str, store_flag: str) -> str:
    """Resolve effective store name."""
    if store_flag:
        return store_flag
    env = os.environ.get(config.STORE, '')
    if env:
        return env
    return read_active(data_dir)


def _ensure_store_backend_key(store_name: str, data_dir: str) -> None:
    """Hot-path: write `MEMMAN_BACKEND_<store>` from the default if missing.

    Two-process safe via `_write_env_keys_with_flock`. No-op when the
    per-store key is already present. Single-machine only -- shared
    filesystems (NFS) are out of scope.
    """
    from memman.setup.scheduler import _write_env_keys_with_flock

    file_values = config.parse_env_file(config.env_file_path(data_dir))
    if config.BACKEND_FOR(store_name) in file_values:
        return
    default_kind = (file_values.get(config.DEFAULT_BACKEND)
                    or 'sqlite').lower()
    updates: dict[str, str] = {
        config.BACKEND_FOR(store_name): default_kind,
        }
    if default_kind == 'postgres':
        default_dsn = file_values.get(config.DEFAULT_PG_DSN)
        if default_dsn:
            updates[config.env_key_for('postgres', 'DSN', store_name)] = default_dsn
    _write_env_keys_with_flock(updates, data_dir=data_dir)


def _get_llm_client_or_fail(role: str) -> 'MemmanLLMClient':
    """Return a per-role LLM client, re-wrapping ConfigError as ClickException.

    Keeps `memman.llm` free of `click` — the CLI boundary is the only
    place that should know how to surface a user-facing config error.
    `role` is `'fast'`, `'slow_canonical'`, or `'slow_metadata'` (worker pipeline,
    operator rebuilds).
    """
    from memman.exceptions import ConfigError
    from memman.llm.client import get_llm_client
    try:
        return get_llm_client(role)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc


def _active_backend(
        ctx: click.Context, *,
        unchecked: bool = False,
        reindex_on_open: bool = True) -> 'Backend':
    """Click adapter around `memman.session.active_store`.

    Resolves data_dir and the active store name from the click context
    and yields the standard "active Backend" context manager. Use as:

        with _active_backend(ctx) as backend:
            ...

    Pass `unchecked=True` from diagnostics (`doctor`, `embed status`)
    that must run against a stale or fresh store without tripping the
    fingerprint assert. Pass `reindex_on_open=False` from recall so the
    on-open constants-hash reindex (potentially seconds of cosine work)
    stays off the user-facing hot path; the drainer's maintenance pass
    repairs drift instead.
    """
    from memman.session import active_store
    data_dir = ctx.obj['data_dir']
    name = _resolve_store_name(data_dir, ctx.obj['store'])
    return active_store(
        data_dir=data_dir, store=name,
        unchecked=unchecked, reindex_on_open=reindex_on_open)


def _parse_since(since: str) -> str:
    """Parse a relative time string (e.g. '7d', '24h') to ISO timestamp."""
    m = re.match(r'^(\d+)([dhm])$', since)
    if not m:
        raise click.ClickException(
            f'Invalid --since format: {since} (use e.g. 7d, 24h, 30m)')
    val, unit = int(m.group(1)), m.group(2)
    delta = {'d': timedelta(days=val), 'h': timedelta(hours=val),
             'm': timedelta(minutes=val)}[unit]
    cutoff = datetime.now(timezone.utc) - delta
    return format_timestamp(cutoff)


def _parse_entities(entities: str) -> list[str]:
    """Parse and validate comma-separated entities.

    Notes
    -----
    - Both caps below are MEASURED against the population they see,
      fleet-wide on 2026-09-03: 6,220 active rows carrying 116,057
      entities across 24 stores.
    - The 200-char per-entity cap is INERT. Longest observed entity
      is 137 chars, p99 is 42, and zero of 116,057 exceed 200. It has
      never fired and is kept only as a guard against a pathological
      argument.
    - The 50-entity cap governs THIS path alone, and it does not
      describe what the column holds: 74 rows (1.19%) already carry
      more than 50 entities, up to 181. `pipeline/remember.py` sets
      entities from the enrichment without passing through here, and
      merges them as a monotonic union on every reconciliation, so
      the machine path is deliberately unbounded while a caller is
      refused at 51. Raising or removing this cap is a design
      question, not a tuning one - see QUEUE-1 Q2.
    - Neither cap is the binding constraint on usefulness.
      `graph/entity.py` caps entity edges at MAX_TOTAL_ENTITY_EDGES
      = 50 and counts two per target (forward and reverse) at
      MAX_ENTITY_LINKS = 5 targets each, so about FIVE entities
      exhaust the whole edge budget and later ones produce no edge
      at all. Stored median is 20.
    """
    entity_list: list[str] = []
    if not entities:
        return entity_list
    for e in entities.split(','):
        e = e.strip()
        if e:
            if len(e) > 200:
                raise click.ClickException(
                    f'entity too long ({len(e)} chars, max 200):'
                    f' {e[:50]}')
            entity_list.append(e)
    if len(entity_list) > 50:
        raise click.ClickException(
            f'too many entities ({len(entity_list)}, max 50)')
    return entity_list


class MemmanGroup(click.Group):
    """Root group that reports a backend failure as a clean CLI error.

    Notes
    -----
    - One `invoke` override covers the whole command tree: a group
      runs its subcommands inside its own `invoke`, so a
      `BackendError` raised at any depth passes through here. The
      alternative, a handler per command, would repeat itself at
      every command that opens a store or the queue.
    - The caught type is `BackendError` AND its subclasses, so
      `store.errors.ConfigError` and `store.errors.IntegrityError`
      come here too. Their messages are user-facing, but a constraint
      violation is bug-shaped and now exits as one line like any
      other; `--debug` is what recovers its stack.
    - `session.active_store` keeps its own earlier catch, so a
      read-write store open never reaches here. This seam is what
      covers the paths that bypass it: the queue, the read-only
      opens, and every mid-command failure the Postgres backend
      translates.
    - `sqlite3.Error` is caught alongside, because the SQLite backend
      translates no statement failure of its own: `open_db` translates
      the OPEN, and nothing translates `database is locked` from a
      query. Without this arm the same condition exits as one clean
      line on Postgres and as a raw traceback on SQLite.
    - Translating HERE rather than in `DB._query` / `DB._exec` is what
      keeps the fifteen callers that branch on a driver type intact
      (`queue.claim`'s stale-claim reclaim, `recall`'s bookkeeping
      skip, `sqlite.py`'s `IntegrityError` arm). Their handlers sit
      deeper, so they run first and this seam never sees the error --
      the same ordering the Postgres backend gets by translating at
      the connection scope instead of at each statement.
    """

    def invoke(self, ctx: click.Context) -> Any:
        """Run the subcommand, reporting a backend failure as a message."""
        try:
            return super().invoke(ctx)
        except BackendError as exc:
            logger.debug('backend error reached the CLI seam', exc_info=True)
            raise click.ClickException(self._name_the_stack(str(exc))) from exc
        except sqlite3.Error as exc:
            logger.debug('sqlite error reached the CLI seam', exc_info=True)
            raise click.ClickException(
                self._name_the_stack(
                    f'sqlite query failed: {exc}')) from exc

    @staticmethod
    def _name_the_stack(user_message: str) -> str:
        """Append the log file carrying the stack, when one does.

        Parameters
        ----------
        user_message : str
            The one-line message Click prints.

        Returns
        -------
        str
            `user_message`, with the log file appended when a rotating
            file handler is attached to carry the stack.

        Notes
        -----
        - The pointer rides the MESSAGE rather than a log record of its
          own. A record would sit at the mercy of the stream handler's
          level, and `MEMMAN_LOG_LEVEL=ERROR` is an installable value
          that would drop it -- restoring the very symptom of one line
          and no route to the stack. Click prints this message through
          its own writer, so no level can suppress it.
        - The test is an ATTACHED HANDLER, never `is_worker()`:
          `scheduler serve` sets the worker flag inside the command,
          long after the root callback configured logging, so the flag
          can read true while no file holds any stack.
        - The worker's stderr is a systemd `append:` redirect that
          nothing rotates, so the stack cannot go there without growing
          `enrich.err` forever. It goes to the rotated
          `logs/memman.log`, which `memman log worker --stack` reads.
        - The suggested command carries `--data-dir` whenever the
          writer's data dir is not the default, and carries it BEFORE
          the subcommand, which is the only position Click accepts for
          a group option. The worker's `MEMMAN_DATA_DIR` comes from the
          unit or the launchd wrapper and never reaches the operator's
          shell, so a bare command would resolve `~/.memman` and tail
          a different install's log. The handler writes
          `<data_dir>/logs/memman.log`, so its own path supplies the
          value.
        - The `exc_info=True` calls stay in the `except` arms above
          rather than moving in here: outside a lexical handler the
          formatter reads them as dead and STRIPS the keyword, which
          silently discards the only copy of the stack.
        """
        for handler in logger.handlers:
            if (isinstance(handler, logging.handlers.RotatingFileHandler)
                    and getattr(handler, '_memman', False)):
                stack_file = pathlib.Path(handler.baseFilename)
                writer_data_dir = str(stack_file.parent.parent)
                pin = ('' if writer_data_dir == default_data_dir()
                       else f' --data-dir {writer_data_dir}')
                return (f'{user_message} (full traceback in'
                        f' {stack_file}; read it with'
                        f" 'memman{pin} log worker --stack')")
        return user_message


@click.group(cls=MemmanGroup)
@click.version_option(version=memman.__version__, prog_name='memman')
@click.option('--data-dir', default=None, help='Base data directory (env: MEMMAN_DATA_DIR)')
@click.option('--store', 'store_name', default='', help='Named memory store')
@click.option('--verbose', '-v', is_flag=True, default=False,
              help='INFO-level logging to stderr')
@click.option('--debug', is_flag=True, default=False,
              help='DEBUG-level logging to stderr (overrides --verbose)')
@click.pass_context
def cli(ctx: click.Context, data_dir: str | None, store_name: str,
        verbose: bool, debug: bool) -> None:
    """Persistent memory store for LLM agents."""
    if data_dir is None:
        data_dir = os.environ.get(config.DATA_DIR, default_data_dir())
    _configure_logging(data_dir, verbose, debug)
    ctx.ensure_object(dict)
    ctx.obj['data_dir'] = data_dir
    ctx.obj['store'] = store_name
    ctx.obj['verbose'] = verbose
    ctx.obj['debug'] = debug


def claude_callable(cmd):
    """Mark a Click command as safe for Claude Code auto-allow.

    `memman install` walks the CLI tree and emits a `permissions.allow`
    entry in `~/.claude/settings.json` for every command marked with
    this decorator. Adding or removing the marker on a subcommand
    automatically flows to the install-time allow list.
    """
    cmd.claude_callable = True
    return cmd


def list_claude_permissions() -> list[str]:
    """Return `permissions.allow` entries for every @claude_callable command.

    Order is stable: alphabetical by full dotted path.
    """
    def walk(group, prefix):
        out: list[str] = []
        for name, cmd in group.commands.items():
            path = (*prefix, name)
            if isinstance(cmd, click.Group):
                out.extend(walk(cmd, path))
            elif getattr(cmd, 'claude_callable', False):
                out.append(f'Bash(memman {" ".join(path)}:*)')
        return out
    return sorted(walk(cli, ()))


@cli.group()
def graph() -> None:
    """Graph operations on insights and edges."""


@cli.group(name='embed')
def embed_grp() -> None:
    """Embed-provider operations: status, re-embed on swap."""


@cli.group(no_args_is_help=True)
def scheduler() -> None:
    """Async write pipeline: scheduler state, queue, worker logs."""


@scheduler.group('queue', invoke_without_command=True)
@click.pass_context
def queue(ctx: click.Context) -> None:
    """Inspect and manage the deferred-write queue."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(queue_list)


@cli.group()
def insights() -> None:
    """Operations on stored insights (read, review, resolve)."""


@cli.group()
def log() -> None:
    """View memman logs (operation audit + worker output)."""


@cli.group(name='config')
def config_cmd() -> None:
    """Inspect and modify persisted memman settings."""


@config_cmd.command('set')
@click.argument('key')
@click.argument('value')
@click.pass_context
def config_set(ctx: click.Context, key: str, value: str) -> None:
    """Write `KEY=VALUE` to the env file, bypassing the install seed model.

    Use this to change a persistable setting after initial install --
    switching backends, rotating an API key, updating a DSN. Install
    flags remain sticky-seed (they never override an existing file
    value); `config set` is the explicit override path.

    Six shapes of key are accepted:
      * any member of `config.INSTALLABLE_KEYS`
      * `MEMMAN_BACKEND_<store>` (per-store backend routing)
      * `MEMMAN_POSTGRES_DSN_<store>` (per-store DSN)
      * `MEMMAN_RERANK_ENABLED_<store>` (per-store rerank toggle)
      * `MEMMAN_SURFACE_<store>` (per-store surface: code|claw)
      * `MEMMAN_AUTO_SEMANTIC_THRESHOLD_<store>` (per-store override:
        float in (0,1), or 'skip'/'none' to disable semantic edges)
    Bare canonicals (`MEMMAN_BACKEND`, `MEMMAN_POSTGRES_DSN`) are
    rejected with hints pointing at `MEMMAN_DEFAULT_*` or the
    per-store form.
    """
    bare_canonicals = {
        'MEMMAN_BACKEND': (
            'use MEMMAN_DEFAULT_BACKEND as the default backend'
            ' or MEMMAN_BACKEND_<store> for a specific store'),
        'MEMMAN_POSTGRES_DSN': (
            'use MEMMAN_DEFAULT_POSTGRES_DSN as the default Postgres DSN'
            ' or MEMMAN_POSTGRES_DSN_<store> for a specific store'),
        }
    if key in bare_canonicals:
        raise click.ClickException(
            f'{key!r} is no longer accepted under the per-store routing'
            f' model; {bare_canonicals[key]}')

    accepted = key in config.INSTALLABLE_KEYS
    if not accepted:
        for prefix, validator, _ in config.PER_STORE_KEY_SPECS:
            if not key.startswith(prefix):
                continue
            if not valid_store_name(key[len(prefix):]):
                break
            if validator is not None:
                err = validator(value)
                if err is not None:
                    raise click.ClickException(f'{key}={value!r}: {err}')
            accepted = True
            break
    if not accepted:
        shapes = ', '.join(p + '<store>'
                           for p, _, _ in config.PER_STORE_KEY_SPECS)
        raise click.ClickException(
            f'{key!r} is not a recognized config key. Accepted shapes:'
            f' INSTALLABLE_KEYS members or {shapes}.')
    from memman.setup.scheduler import _write_env_keys
    data_dir = ctx.obj['data_dir']
    _write_env_keys({key: value}, data_dir=data_dir)
    config.reset_file_cache()
    click.echo(f'set {key} in {config.env_file_path(data_dir)}')


@config_cmd.command('set-pg-dsn')
@click.option('--store', 'store', default=None,
              help='Per-store key: writes MEMMAN_POSTGRES_DSN_<store>.')
@click.option('--default', 'is_default', is_flag=True,
              help='Default fallback: writes MEMMAN_DEFAULT_POSTGRES_DSN.')
@click.pass_context
def config_set_pg_dsn(
        ctx: click.Context, store: str | None, is_default: bool) -> None:
    """Build a Postgres DSN from prompts and write it to the env file.

    Frees the operator from hand-typing libpq URIs. Prompts for host,
    port, user, password, and dbname; assembles a `postgresql://...`
    URI (URL-encoding credentials), and persists it under either
    `MEMMAN_DEFAULT_POSTGRES_DSN` (`--default`) or
    `MEMMAN_POSTGRES_DSN_<store>` (`--store NAME`). Exactly one of the two
    flags is required. No connectivity probe; verify with
    `memman doctor` or `memman migrate --dry-run`.
    """
    from memman.setup.scheduler import _write_env_keys
    from memman.trace import redact_dsn

    if is_default == bool(store):
        raise click.ClickException(
            'specify exactly one of --default or --store NAME')
    if store is not None and not valid_store_name(store):
        raise click.ClickException(
            f'invalid store name {store!r}'
            ' (alnum, dash, underscore; 1-64 chars)')

    host = click.prompt('host', default='localhost')
    port = click.prompt('port', type=int, default=5432)
    user = click.prompt('user')
    password = click.prompt(
        'password (leave empty to use ~/.pgpass)',
        hide_input=True, default='', show_default=False)
    dbname = click.prompt('dbname', default='memman')

    auth = quote(user, safe='')
    if password:
        auth = f'{auth}:{quote(password, safe="")}'
    dsn = f'postgresql://{auth}@{host}:{port}/{quote(dbname, safe="")}'

    key = config.DEFAULT_PG_DSN if is_default else config.env_key_for('postgres', 'DSN', store)
    data_dir = ctx.obj['data_dir']
    _write_env_keys({key: dsn}, data_dir=data_dir)
    config.reset_file_cache()
    click.echo(f'set {key}={redact_dsn(dsn)} in {config.env_file_path(data_dir)}')


@config_cmd.command('get')
@click.argument('key')
@click.pass_context
def config_get(ctx: click.Context, key: str) -> None:
    """Print the value of `KEY` from the env file (empty line if unset).

    Reads from `<MEMMAN_DATA_DIR>/env` only, matching the runtime
    resolver. DSN values are redacted; all other values print as
    stored. Exits 1 if the key is unset.
    """
    data_dir = ctx.obj['data_dir']
    parsed = config.parse_env_file(config.env_file_path(data_dir))
    value = parsed.get(key)
    if value is None or value == '':
        raise click.ClickException(f'{key} is not set')
    if 'POSTGRES_DSN' in key or 'API_KEY' in key:
        from memman.trace import redact_dsn
        click.echo(redact_dsn(value) if 'POSTGRES_DSN' in key
                   else '***REDACTED***')
    else:
        click.echo(value)


@config_cmd.command('show')
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """Dump effective config: env vars + on-disk files + scheduler state."""
    effective = config.enumerate_effective_config()
    data_dir = ctx.obj['data_dir']
    parsed = config.parse_env_file(config.env_file_path(data_dir))
    if parsed.get(config.DEFAULT_BACKEND):
        effective[config.DEFAULT_BACKEND] = parsed[config.DEFAULT_BACKEND]
    if parsed.get(config.DEFAULT_PG_DSN):
        effective[config.DEFAULT_PG_DSN] = '***REDACTED***'
    per_store: dict[str, str] = {}
    for key, value in sorted(parsed.items()):
        if not value:
            continue
        for prefix, _, secret in config.PER_STORE_KEY_SPECS:
            if key.startswith(prefix):
                per_store[key] = '***REDACTED***' if secret else value
                break
    out: dict = {
        'data_dir': data_dir,
        'env': effective,
        'per_store': per_store,
        'files': {},
        'active_store': _resolve_store_name(data_dir, ctx.obj['store']),
        }
    from memman.setup.scheduler import _state_file_path, read_state
    out['files']['scheduler.state'] = {
        'path': str(_state_file_path()),
        'value': read_state(),
        }
    try:
        from memman.setup.scheduler import status as scheduler_status_fn
        s = scheduler_status_fn()
        out['scheduler'] = {
            'state': s.get('state'),
            'installed': s.get('installed'),
            'platform': s.get('platform'),
            'interval_seconds': s.get('interval_seconds'),
            }
    except (ImportError, ModuleNotFoundError):
        pass
    _json_out(out)


@claude_callable
@cli.command()
@click.argument('content', nargs=-1, required=True)
@click.option('--cat', default='fact', help='Category')
@click.option('--imp', default=3, type=int,
              help='Sort key for listings and tie-breaks (1-5, default 3)')
@click.option('--source', default='user', help='Source')
@click.option('--entities', default='', help='Comma-separated entities')
@click.option('--no-reconcile', is_flag=True, default=False,
              help='Store the text verbatim: skip fact extraction and'
                   ' reconciliation, so the write cannot be dropped as'
                   ' trivial or folded into an existing insight')
@click.option('--session', default='',
              envvar=[config.SESSION_ID, config.CLAUDE_SESSION_ID],
              help='Session id for the temporal chain (defaults to'
                   ' $MEMMAN_SESSION_ID, then $CLAUDE_CODE_SESSION_ID)')
@click.pass_context
def remember(ctx: click.Context, content: tuple[str, ...], cat: str,
             imp: int, source: str, entities: str,
             no_reconcile: bool, session: str) -> None:
    """Store a new insight via the queue.

    Always enqueues. The worker drains the queue (under systemd/launchd
    on a host, or in-process when the trigger is inline). Rejected when
    the scheduler is stopped (memman is recall-only in that state).
    Content is screened by `check_content_quality` before enqueue;
    advisory warnings come back on the JSON response under
    `quality_warnings` without blocking the write.

    Notes
    -----
    - `--source` is provenance, stored verbatim (including the
      `user` default); `--session` is the temporal chain key (no
      session, no backbone edge); idempotency rides on a queue uuid
      minted at enqueue. One field per job.
    """
    _require_started('write')
    content_str = ' '.join(content)
    content_bytes = len(content_str.encode('utf-8'))
    if content_bytes > 8000:
        raise click.ClickException(
            f'content too long ({content_bytes} bytes, max 8000);'
            ' consider chunking into multiple remember calls')

    if cat not in VALID_CATEGORIES:
        valid = ', '.join(sorted(VALID_CATEGORIES))
        raise click.ClickException(
            f'invalid category {cat!r}; valid: {valid}')
    if imp < 1 or imp > 5:
        raise click.ClickException(
            f'importance must be 1-5, got {imp}')

    # Notes:
    # - Canonicalize here rather than in the drain: the enqueue
    #   reports success to the caller, so a list the worker would
    #   reject has to fail now or the write is lost silently.
    # - Storing the parsed form leaves the drain's own re-parse
    #   idempotent, so it cannot fail on a row that got this far.
    entities_clean = ','.join(_parse_entities(entities))

    from memman.search.quality import check_content_quality
    quality_warnings = check_content_quality(content_str)

    data_dir_val = ctx.obj['data_dir']
    name = _resolve_store_name(data_dir_val, ctx.obj['store'])

    from memman.queue import enqueue, queue_db
    with queue_db(data_dir_val) as conn:
        # Notes:
        # - Explicitness decides whether `_plan_fact` keeps the caller's
        #   category or defers to the extractor's per-fact guess, so a
        #   caller who types the default must not read as one who typed
        #   nothing.
        # - Importance has no extractor value to defer to and is stored
        #   as passed.
        from_cmdline = click.core.ParameterSource.COMMANDLINE
        cat_hint = (
            cat if ctx.get_parameter_source('cat') == from_cmdline
            else None)
        row_id, queue_uuid = enqueue(
            conn, store=name, content=content_str,
            hint_cat=cat_hint, hint_imp=imp,
            hint_source=source,
            hint_entities=entities_clean or None,
            hint_no_reconcile=no_reconcile,
            session_id=session or None,
            priority=0)
    _json_out({
        'action': 'queued',
        'queue_id': row_id,
        'queue_uuid': queue_uuid,
        'store': name,
        'quality_warnings': quality_warnings,
        })


_STOP_REQUESTED = False


def _request_stop() -> None:
    """Flip the module-level stop flag.

    Polled by the serve loop and `_drain_queue`'s inner row loop so a
    SIGTERM during a long drain exits within seconds rather than the
    full per-row timeout.
    """
    global _STOP_REQUESTED
    _STOP_REQUESTED = True


def _stop_requested() -> bool:
    """Return whether a stop has been signaled."""
    return _STOP_REQUESTED


def _reset_stop_for_tests() -> None:
    """Test-only: clear the stop flag between in-process serve invocations."""
    global _STOP_REQUESTED
    _STOP_REQUESTED = False


_LAST_HEARTBEAT_AT: dict[str, float] = {}
HEARTBEAT_MIN_INTERVAL_SECONDS = 60


def _reset_heartbeat_state() -> None:
    """Test-only: clear the heartbeat tracking dict.

    `_LAST_HEARTBEAT_AT` is module-level. In-process CliRunner tests
    share it across test invocations, which can cause cross-test
    contamination if data_dir paths are reused. Tests reset between
    invocations via an autouse conftest fixture.
    """
    _LAST_HEARTBEAT_AT.clear()


@scheduler.command('drain', hidden=True)
@click.option('--limit', default=100, type=int,
              help='Max blobs processed per invocation')
@click.option('--timeout', default=300, type=int,
              help='Max wall-clock seconds per invocation')
@click.option('--stores', default='',
              help='Comma-separated store names; default all')
@click.option('--progress', is_flag=True, default=False,
              help='Echo per-blob progress')
@click.option('--trace', is_flag=True, default=False,
              help='Write structured trace to ~/.memman/logs/debug.log')
@click.pass_context
def scheduler_drain(ctx: click.Context, limit: int,
                    timeout: int, stores: str, progress: bool,
                    trace: bool) -> None:
    """Run the worker drain loop. Hidden: invoked by the systemd/launchd
    unit's ExecStart. Operators should use `scheduler trigger` to kick
    the unit, or `scheduler queue list` to inspect pending rows.
    """
    if trace:
        os.environ[config.DEBUG] = '1'
    _drain_queue(ctx, limit, timeout, stores, progress)


def _maybe_fire_backup(
        data_dir: str, now: datetime,
        settle: Callable[[], None] | None = None) -> None:
    """Run a backup in-process when the cron matches this minute (serve only).

    Serve-mode only: on systemd/launchd hosts the native backup timer
    owns scheduled backups, so this defers to it (preventing a
    double-fire if a serve loop also runs there). Once-per-minute and
    restart-safe via ~/.memman/backup.state, which is stamped only
    after a successful run so a transient failure retries on the next
    iteration rather than being suppressed for the whole minute. When
    firing, `settle` (if given) drains the queue to empty so the
    snapshot captures a settled store; the bundle also includes
    queue.db, so residual pending writes are preserved regardless. The
    backup runs inline (build_bundle is local-disk-bound; cloud sync of
    the target is async). No drain.lock is taken -- the snapshot is
    online.
    """
    from memman.setup.scheduler import SCHEDULER_KIND_SERVE, detect_scheduler
    from memman.setup.scheduler import read_backup_state, write_backup_state
    try:
        if detect_scheduler() != SCHEDULER_KIND_SERVE:
            return
    except RuntimeError:
        return
    cron = config.get(config.BACKUP_CRON)
    if not cron:
        return
    from memman.backup.cron import cron_matches
    if not cron_matches(cron, now):
        return
    minute_key = now.strftime('%Y-%m-%dT%H:%M')
    if read_backup_state() == minute_key:
        return
    if settle is not None:
        settle()
    from memman.backup import run_backup
    try:
        run_backup(data_dir)
        write_backup_state(minute_key)
    except Exception as exc:
        logger.warning('scheduler serve: backup failed: %s', exc)


@scheduler.command('serve')
@click.option('--interval', default=None, type=int,
              help=('Seconds between drain iterations.'
                    ' Falls back to MEMMAN_INTERVAL, then 60.'))
@click.option('--once', is_flag=True, default=False,
              help='Run a single drain pass and exit')
@click.pass_context
def scheduler_serve(ctx: click.Context, interval: int | None,
                    once: bool) -> None:
    """Run the drain loop continuously as a long-lived process.

    Used as PID 1 in containers and by hosts where systemd/launchd are
    not available (set MEMMAN_SCHEDULER_KIND=serve). On SIGTERM/SIGINT
    the current drain finishes (bounded by the per-iteration timeout)
    and the process exits 0.

    Resolution order for the iteration interval:
      1. `--interval` flag (when passed)
      2. `MEMMAN_INTERVAL` from the env file
      3. 60 (the documented default)
    """
    import signal
    import socket
    import sys as _sys
    import time as _time

    from memman import __version__ as _memman_version
    from memman import trace
    from memman.queue import mark_stale_on_resume, queue_db
    from memman.setup.scheduler import STATE_STOPPED, clear_serve_interval
    from memman.setup.scheduler import read_state, write_serve_interval

    if interval is None:
        raw = config.get(config.INTERVAL)
        if raw is None or raw.strip() == '':
            interval = 60
        else:
            try:
                interval = int(raw)
            except ValueError:
                raise click.ClickException(
                    f'MEMMAN_INTERVAL must be an integer, got {raw!r}')
    if interval < 0:
        raise click.ClickException('--interval must be >= 0')

    os.environ[config.WORKER] = '1'
    # The root callback configured logging before this flag was set, so
    # without a second pass `serve` runs with no worker file handler and
    # drops every stack it was supposed to keep.
    _configure_logging(
        ctx.obj['data_dir'], ctx.obj.get('verbose', False),
        ctx.obj.get('debug', False))
    _reset_stop_for_tests()

    def _handle_stop(signum: int, frame: object) -> None:
        logger.info(
            f'scheduler serve: caught signal {signum}, finishing drain')
        _request_stop()

    prior_term = signal.signal(signal.SIGTERM, _handle_stop)
    prior_int = signal.signal(signal.SIGINT, _handle_stop)
    try:
        write_serve_interval(interval)

        data_dir_val = ctx.obj['data_dir']
        with queue_db(data_dir_val) as conn:
            reclaimed = mark_stale_on_resume(conn)
            if reclaimed:
                logger.info(
                    f'scheduler serve: reclaimed {reclaimed} stale rows')

        trace.setup()
        trace.event(
            'scheduler_serve_start',
            pid=os.getpid(),
            hostname=socket.gethostname(),
            python=_sys.version.split()[0],
            memman_version=_memman_version,
            interval=interval,
            once=once)

        per_drain_timeout = max(10, interval - 10) if interval > 0 else 300

        def _settle_queue() -> None:
            """Drain the queue to empty before a backup (bounded)."""
            for _ in range(50):
                drained = _drain_queue(
                    ctx, limit=100, timeout=per_drain_timeout,
                    stores_filter='', verbose=False)
                if not drained or drained.get('claimed', 0) == 0:
                    return

        while True:
            config.reset_file_cache()
            if read_state() == STATE_STOPPED:
                logger.info('scheduler serve: state=STOPPED, exiting')
                break
            result = _drain_queue(
                ctx, limit=100, timeout=per_drain_timeout,
                stores_filter='', verbose=False)
            _maybe_fire_backup(
                data_dir_val, datetime.now(), settle=_settle_queue)
            if _stop_requested() or once:
                break
            if interval > 0:
                slept = 0.0
                while slept < interval and not _stop_requested():
                    if read_state() == STATE_STOPPED:
                        break
                    _time.sleep(min(1.0, interval - slept))
                    slept += 1.0
            elif result and result.get('claimed', 0) == 0:
                _time.sleep(0.1)

        trace.event('scheduler_serve_stop', pid=os.getpid())
    finally:
        signal.signal(signal.SIGTERM, prior_term)
        signal.signal(signal.SIGINT, prior_int)
        try:
            clear_serve_interval()
        except OSError:
            pass


def _drain_queue(ctx: click.Context, limit: int, timeout: int,
                 stores_filter: str, verbose: bool) -> dict | None:
    """Claim and process queue rows until limit, timeout, or empty.

    Parameters
    ----------
    ctx : click.Context
        Carries `data_dir` and the active store selection.
    limit : int
        Maximum rows to claim in this drain.
    timeout : int
        Seconds to keep claiming before returning, whatever remains.
    stores_filter : str
        Comma-separated store names to drain; empty drains all.
    verbose : bool
        Echo a per-row line to stderr as each row completes.

    Returns
    -------
    dict or None
        `{claimed, processed, failed}`, so a caller can detect an
        empty drain. None when another drain already holds the lock.

    Notes
    -----
    - The emitted JSON carries one count the return value does not:
      `skipped_writes`, the rows that completed without storing an
      insight. Each is filed in the `skipped_writes` ledger and still
      counts toward `processed`.
    """
    import socket
    import sys as _sys
    import time as _time
    from contextlib import ExitStack

    from memman import __version__ as _memman_version
    from memman import trace
    from memman.drain_lock import DrainLockBusy, acquire, release
    from memman.llm import usage as llm_usage
    from memman.pipeline.remember import skip_reason_for_result
    from memman.queue import claim, clear_skipped_write, finish_worker_run
    from memman.queue import mark_done, mark_failed, queue_db, queue_db_path
    from memman.queue import record_skipped_write, start_worker_run, stats
    from memman.setup.scheduler import STATE_STOPPED, read_state

    data_dir_val = ctx.obj['data_dir']
    worker_pid = os.getpid()
    deadline = _time.monotonic() + timeout
    store_list = [s.strip() for s in stores_filter.split(',') if s.strip()]

    trace.setup()
    trace.event(
        'scheduler_fired',
        pid=worker_pid,
        hostname=socket.gethostname(),
        python=_sys.version.split()[0],
        memman_version=_memman_version,
        env=config.enumerate_effective_config())

    try:
        lock_fd = acquire(data_dir_val)
    except DrainLockBusy:
        logger.info('drain: another drain is in progress, skipping')
        trace.event('drain_skipped_locked', data_dir=data_dir_val)
        _json_out({
            'processed': 0,
            'failed': 0,
            'remaining': {'pending': 0, 'claimed': 0,
                          'failed': 0, 'done': 0},
            'llm_usage': {},
            'skipped': 'another drain in progress',
            })
        return None

    stack = ExitStack()
    try:
        trace.event(
            'drain_start',
            data_dir=data_dir_val,
            queue_db_path=queue_db_path(data_dir_val),
            limit=limit,
            timeout=timeout,
            stores=store_list)

        from concurrent.futures import ThreadPoolExecutor

        conn = stack.enter_context(queue_db(data_dir_val))
        processed = 0
        failed = 0
        claimed = 0
        skipped_writes = 0
        touched_stores: set[str] = set()
        store_contexts: dict[str, _StoreContext] = {}
        executor = ThreadPoolExecutor(max_workers=2)
        run_error: str | None = None

        last_hb = _LAST_HEARTBEAT_AT.get(data_dir_val, 0.0)
        record_run = (_time.monotonic() - last_hb) >= HEARTBEAT_MIN_INTERVAL_SECONDS
        run_id = start_worker_run(conn, worker_pid) if record_run else None
        # Snapshot-and-delta, never reset: the ledger is process-wide
        # and row-level deltas below must not clobber the drain total.
        drain_usage_snap = llm_usage.snapshot()
    except Exception:
        stack.close()
        release(lock_fd)
        raise

    try:
        while processed + failed < limit:
            if _stop_requested() or read_state() == STATE_STOPPED:
                logger.info('drain: stop requested, exiting loop')
                trace.event('drain_stop_requested')
                break
            if _time.monotonic() >= deadline:
                logger.info(f'enrich: timeout after {timeout}s')
                trace.event('drain_timeout', timeout=timeout)
                break
            row = claim(conn, worker_pid=worker_pid,
                        stores=store_list or None)
            if row is None:
                break
            claimed += 1

            trace.event(
                'queue_claim',
                row_id=row.id,
                store=row.store,
                priority=row.priority,
                attempts=row.attempts,
                content_len=len(row.content),
                hint_cat=row.hint_cat,
                hint_imp=row.hint_imp,
                hint_source=row.hint_source,
                hint_entities=row.hint_entities)

            ctx = store_contexts.get(row.store)
            if ctx is None:
                try:
                    ctx = stack.enter_context(
                        _StoreContext(row.store, data_dir_val))
                except Exception as exc:
                    mark_failed(
                        conn, row.id, f'{type(exc).__name__}: {exc}')
                    failed += 1
                    trace.event(
                        'queue_failed',
                        row_id=row.id,
                        store=row.store,
                        error_class=type(exc).__name__,
                        error_message=str(exc)[:500],
                        llm_usage={})
                    logger.exception(
                        f'enrich row {row.id} failed during store open')
                    continue
                store_contexts[row.store] = ctx
                if record_run:
                    ctx.begin_drain_run()

            embed_snap, insights_snap = ctx.snapshot_caches()
            row_usage_snap = llm_usage.snapshot()
            try:
                row_t0 = _time.monotonic()
                row_result = _process_queue_row(row, ctx, executor)
                row_elapsed_ms = int((_time.monotonic() - row_t0) * 1000)
                # Notes:
                # - The ledger is observability, so its own failure
                #   must not fail a row whose pipeline already ran:
                #   that would re-run extraction on every retry and
                #   burn the attempt budget.
                # - It is written before mark_done so a crash between
                #   the two leaves a retryable row, never a done row
                #   with no record.
                # - A retry that goes on to store retracts the
                #   earlier entry, or the ledger reports a stored
                #   write as lost forever.
                skip_reason = skip_reason_for_result(row_result)
                try:
                    if skip_reason:
                        record_skipped_write(
                            conn, row.id, row.store, row.content,
                            skip_reason, row.session_id)
                        skipped_writes += 1
                    else:
                        clear_skipped_write(conn, row.id)
                except Exception:
                    logger.exception(
                        f'skipped-write ledger update failed for queue'
                        f' row {row.id}')
                mark_done(conn, row.id)
                processed += 1
                touched_stores.add(row.store)
                ctx.beat_drain_run()
                trace.event(
                    'queue_done',
                    row_id=row.id,
                    store=row.store,
                    elapsed_ms=row_elapsed_ms,
                    llm_usage=llm_usage.delta(
                        row_usage_snap, llm_usage.snapshot()))
                if verbose:
                    click.echo(
                        f'[enrich] done id={row.id} store={row.store}',
                        err=True)
            except Exception as exc:
                ctx.restore_caches(embed_snap, insights_snap)
                mark_failed(conn, row.id, f'{type(exc).__name__}: {exc}')
                failed += 1
                trace.event(
                    'queue_failed',
                    row_id=row.id,
                    store=row.store,
                    error_class=type(exc).__name__,
                    error_message=str(exc)[:500],
                    llm_usage=llm_usage.delta(
                        row_usage_snap, llm_usage.snapshot()))
                from memman.exceptions import EmbedCredentialError
                if isinstance(exc, EmbedCredentialError):
                    trace.event(
                        'embedder_credential_missing',
                        row_id=row.id,
                        store=row.store,
                        provider=ctx.ec.name,
                        model=ctx.ec.model,
                        reason=str(exc)[:500])
                if verbose:
                    click.echo(
                        f'[enrich] fail id={row.id} store={row.store}'
                        f' err={exc}', err=True)
                logger.exception(f'enrich row {row.id} failed')
    except Exception as exc:
        run_error = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        executor.shutdown(wait=True)
        try:
            from memman.maintenance import run_maintenance
            run_maintenance(
                conn, data_dir_val, touched_stores,
                store_contexts, deadline)
        except Exception:
            logger.exception('drain maintenance phase failed')
        if processed > 0:
            record_run = True
        if record_run:
            try:
                if run_id is None:
                    run_id = start_worker_run(conn, worker_pid)
                finish_worker_run(
                    conn, run_id, claimed, processed, failed,
                    error=run_error)
                _LAST_HEARTBEAT_AT[data_dir_val] = _time.monotonic()
            except Exception:
                logger.exception('failed to stamp worker_runs finish row')
        s = stats(conn)
        stack.close()
        release(lock_fd)

    drain_usage = llm_usage.delta(drain_usage_snap, llm_usage.snapshot())
    trace.event('llm_usage_summary', usage=drain_usage)
    trace.event(
        'drain_end',
        processed=processed,
        failed=failed,
        skipped_writes=skipped_writes,
        remaining=s)
    _json_out({
        'processed': processed,
        'failed': failed,
        'skipped_writes': skipped_writes,
        'remaining': s,
        'llm_usage': drain_usage,
        })
    return {'claimed': claimed, 'processed': processed, 'failed': failed}


class _StoreContext:
    """Per-store drain-scope state hoisted out of the row loop.

    One context per store touched in a drain: the open store DB
    connection, the embedding+insight cache (built lazily on first
    use), and the slow-role LLM client + embed client. Reused across
    every row that targets the same store so that scans and HTTP
    setup amortize.
    """

    def __init__(self, store_name: str, data_dir: str) -> None:
        from memman.embed import fingerprint as _fp_mod
        from memman.exceptions import EmbedFingerprintError
        from memman.llm.client import get_llm_client
        from memman.store.factory import open_backend

        self.store_name = store_name
        self.data_dir = data_dir
        from memman.embed import get_client
        _ensure_store_backend_key(store_name, data_dir)
        self.backend = open_backend(store_name, data_dir)
        _fp_mod.seed_if_fresh(self.backend, get_client())
        stored = _fp_mod.stored_fingerprint(self.backend)
        if stored is None:
            raise EmbedFingerprintError(
                f"store {store_name!r} has no embed fingerprint and"
                " contains data; run 'memman embed reembed' to converge.")
        self.ec = _fp_mod.bound_embedder(self.backend)
        self._stored_fp = stored
        self.llm_client = get_llm_client('slow_canonical')
        self.embed_cache: dict[str, list[float]] = dict(
            self.backend.nodes.iter_embeddings_as_vecs())
        self.insights_by_id = {
            i.id: i for i in self.backend.nodes.get_all_active()}
        self._run_id: int | None = None

    def begin_drain_run(self) -> None:
        """Open a per-store drain run row (no-op on SQLite)."""
        if self._run_id is not None:
            return
        try:
            self._run_id = self.backend.start_run()
        except Exception:
            logger.exception(
                f'start_run failed for store {self.store_name!r};'
                ' continuing without heartbeat')
            self._run_id = None

    def beat_drain_run(self) -> None:
        """Advance the per-store drain heartbeat (no-op on SQLite)."""
        if self._run_id is None:
            return
        try:
            self.backend.beat_run(self._run_id)
        except Exception:
            logger.exception(
                f'beat_run failed for store {self.store_name!r};'
                ' continuing without heartbeat')

    def assert_fingerprint_unchanged(self) -> None:
        """Raise EmbedFingerprintError if the store's stored fingerprint
        diverged from the value captured at context construction.

        Per-row heartbeat: a swap that completes mid-drain would
        otherwise let the cached `ec` write vectors of the wrong dim.
        Callers must invoke this before every embed call inside a
        long-running drain loop.
        """
        from memman.embed import fingerprint as _fp_mod
        from memman.exceptions import EmbedFingerprintError
        current = _fp_mod.stored_fingerprint(self.backend)
        if current != self._stored_fp:
            raise EmbedFingerprintError(
                f'store {self.store_name!r} fingerprint changed during'
                f' drain: was {self._stored_fp.provider}:'
                f'{self._stored_fp.model}:{self._stored_fp.dim},'
                f' now {current.provider if current else None}:'
                f'{current.model if current else None}:'
                f'{current.dim if current else None};'
                ' row released for retry.')

    def snapshot_caches(self) -> tuple[dict, dict]:
        """Return shallow copies of the caches for rollback."""
        return dict(self.embed_cache), dict(self.insights_by_id)

    def restore_caches(self, embed: dict, insights: dict) -> None:
        """Restore caches to a prior snapshot after a failed row."""
        self.embed_cache.clear()
        self.embed_cache.update(embed)
        self.insights_by_id.clear()
        self.insights_by_id.update(insights)

    def close(self) -> None:
        """Close the active Backend's underlying connection."""
        if self._run_id is not None:
            try:
                self.backend.finish_run(self._run_id)
            except Exception:
                logger.exception(
                    f'finish_run failed for store {self.store_name!r}')
            self._run_id = None
        try:
            self.backend.close()
        except Exception:
            logger.exception(
                f'failed closing backend for store {self.store_name!r}')

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _process_queue_row(
        row: 'memman.queue.QueueRow',
        ctx: _StoreContext,
        executor: 'ThreadPoolExecutor') -> dict[str, Any]:
    """Run the full remember pipeline on a claimed queue row.

    The insight's `source` is `row.hint_source` verbatim (provenance
    survives the queue), falling back to `'user'` for programmatic
    enqueues that pass nothing. Crash-recovery idempotency is
    enforced unconditionally via `row.queue_uuid`; `row.session_id`
    carries the temporal chain key onto the stored insight.

    Hoisted state (db, embed_cache, insights_by_id, llm_client, ec,
    executor) comes from `ctx` and the drain-level executor. The
    drain loop snapshots and restores `ctx`'s caches around this call
    so a transaction failure can't pollute the next row's planning.

    Returns
    -------
    dict[str, Any]
        The `run_remember` result, or `{'action': 'already_committed'}`
        when the idempotency guard fired. The drain reads it through
        `skip_reason_for_result` to tell a write that stored an
        insight from one that stored nothing.
    """
    from memman import trace as _trace

    ctx.assert_fingerprint_unchanged()

    entity_list = _parse_entities(row.hint_entities or '')
    category = row.hint_cat or 'fact'
    importance = row.hint_imp if row.hint_imp is not None else 3
    source = row.hint_source or 'user'

    if category not in VALID_CATEGORIES:
        category = 'fact'
    if importance < 1 or importance > 5:
        importance = 3

    backend = ctx.backend

    _trace.event(
        'process_row',
        row_id=row.id,
        store=row.store,
        data_dir=ctx.data_dir,
        source=source,
        category=category,
        importance=importance)

    if backend.nodes.has_active_with_queue_uuid(row.queue_uuid):
        logger.info(
            f'queue row {row.id} already committed to store'
            f' {row.store!r}; skipping re-processing')
        _trace.event(
            'process_row_skipped',
            row_id=row.id,
            reason='already_committed')
        return {'action': 'already_committed'}

    now = datetime.now(timezone.utc)
    access_count = 0
    replaced_id = row.hint_replaced_id or ''
    redirected_from = ''
    if replaced_id:
        # Notes:
        # - The target may have been superseded between enqueue and
        #   claim, by an earlier queued replace of the same id. The
        #   replace follows the chain to its current head, so the
        #   topic ends with one current row instead of two.
        # - A forgotten or missing head passes the original id
        #   through, and `_apply_plan` degrades to a named add.
        old = backend.nodes.get_include_deleted(replaced_id)
        seen: set[str] = set()
        while (old is not None and old.superseded_by
                and old.id not in seen):
            seen.add(old.id)
            old = backend.nodes.get_include_deleted(old.superseded_by)
        if (old is not None and old.deleted_at is None
                and old.superseded_by is None):
            access_count = old.access_count
            if old.id != replaced_id:
                redirected_from = replaced_id
                replaced_id = old.id
    # Both fields must reach the parent Insight: _plan_fact copies
    # session_id and queue_uuid off it, so omitting either makes the
    # whole feature a silent no-op.
    insight = Insight(
        id=str(uuid.uuid4()), content=row.content,
        category=category, importance=importance,
        entities=entity_list, source=source,
        access_count=access_count,
        created_at=now, updated_at=now,
        session_id=row.session_id, queue_uuid=row.queue_uuid)

    from memman.pipeline.remember import run_remember
    result = run_remember(
        backend, insight, row.content,
        no_reconcile=row.hint_no_reconcile or bool(row.hint_replaced_id),
        replaced_id=replaced_id,
        cat_explicit=row.hint_cat is not None,
        embed_cache=ctx.embed_cache,
        insights_by_id=ctx.insights_by_id,
        executor=executor,
        llm_client=ctx.llm_client,
        ec=ctx.ec,
        store_name=ctx.store_name)
    if redirected_from:
        result['redirected_from'] = redirected_from
    _json_out(result)
    return result


@claude_callable
@cli.command()
@click.argument('keyword', nargs=-1, required=True)
@click.option('--cat', default='', help='Filter by category')
@click.option('--limit', default=10, type=int, help='Max results')
@click.option('--source', default='',
              help='Filter by source (exact match on the stored provenance string)')
@click.option('--basic', is_flag=True, default=False, help='Simple SQL LIKE matching')
@click.option('--brief', is_flag=True, default=False,
              help='Project each row to id, category, importance, '
                   'created_at, summary')
@click.option('--intent', default='', help='Override intent')
@click.option('--expand', 'expand', is_flag=True, default=False,
              help='Run LLM query expansion before retrieval (off by default)')
@click.option('--min-score', 'min_score', default=0.0,
              type=click.FloatRange(0.0, 2.0),
              help='Drop rows whose keyword+similarity sum is below '
                   'this floor (0.0 = off, max 2.0)')
@click.option('--session', default='',
              envvar=[config.SESSION_ID, config.CLAUDE_SESSION_ID],
              help='Calling session id, recorded on the recall-detail '
                   'oplog row so a return is attributable to a session '
                   '(defaults to $MEMMAN_SESSION_ID, then '
                   '$CLAUDE_CODE_SESSION_ID, matching `remember`)')
@click.pass_context
def recall(ctx: click.Context, keyword: tuple[str, ...], cat: str,
           limit: int, source: str, basic: bool, brief: bool,
           intent: str, expand: bool, min_score: float,
           session: str) -> None:
    """Retrieve insights by keyword."""
    from memman import trace
    from memman.embed.fingerprint import assert_fingerprint_unchanged_for_sync
    from memman.embed.fingerprint import bound_embedder, stored_fingerprint
    from memman.llm import usage as llm_usage
    from memman.llm.extract import expand_query
    from memman.search.intent import intent_from_string
    from memman.search.recall import intent_aware_recall
    project = insight_to_brief_dict if brief else insight_to_full_dict
    keyword_str = ' '.join(keyword)
    if math.isnan(min_score):
        raise click.ClickException(
            '--min-score must be a number; NaN compares false against '
            'every floor and would silently disable the filter.')
    if basic and min_score > 0.0:
        raise click.ClickException(
            '--min-score needs the scored path; --basic is SQL LIKE '
            'matching and computes no scores.')
    # Validated above the `--basic` early return, or `--basic --intent
    # bogus` exits 0 reporting the flag as merely ignored -- telling the
    # caller their intent was well-formed but unusable here, which is a
    # worse answer than silence.
    typed_intent = None
    if intent:
        try:
            typed_intent = intent_from_string(intent)
        except ValueError as e:
            raise click.ClickException(str(e))
    store_name = _resolve_store_name(ctx.obj['data_dir'], ctx.obj['store'])
    per_store_rerank = config.get_store_rerank_enabled(store_name)
    rerank = (per_store_rerank if per_store_rerank is not None
              else config.get_bool(config.RERANK_ENABLED, default=True))
    with _active_backend(ctx, reindex_on_open=False) as backend:
        if basic:
            results = backend.nodes.query(
                keyword=keyword_str, category=cat,
                source=source, limit=limit)
            try:
                with backend.transaction():
                    for r in results:
                        backend.nodes.increment_access_count(r.id)
                        r.access_count += 1
                    backend.oplog.log(
                        operation='recall:basic', insight_id='',
                        detail=f'q={keyword_str} hits={len(results)}')
            except sqlite3.OperationalError as exc:
                logger.debug(
                    'recall_bookkeep_skipped basic q=%r: %s',
                    keyword_str, exc)
            # Notes:
            # - `--min-score` is rejected above, not named here: it
            #   is a FILTER, so ignoring it would certify every
            #   returned row as having cleared a floor it never met.
            # - `--intent` and `--expand` leave the caller a
            #   complete, visible result set, so naming them is
            #   enough; an exit code would break a hook that passes
            #   them out of habit.
            # - The tuple order below fixes the reported order.
            basic_meta: dict[str, Any] = {'basic': True}
            ignored = [
                name for name, was_given
                in (('intent', bool(intent)), ('expand', expand))
                if was_given]
            if ignored:
                basic_meta['ignored'] = ignored
            _json_out({
                'results': [project(r) for r in results],
                'meta': basic_meta,
                })
            return

        ec = bound_embedder(backend)
        bound_fp = stored_fingerprint(backend)

        expansion: dict = {}
        if expand:
            llm_client = _get_llm_client_or_fail('fast')
            # The recall process exits right after retrieval, so the
            # query_expansion bucket would die unreported without a
            # summary emitted here (drains never see this stage).
            expand_usage_snap = llm_usage.snapshot()
            expansion = expand_query(llm_client, keyword_str)
            keyword_str = expansion['expanded_query']
            trace.event(
                'llm_usage_summary',
                usage=llm_usage.delta(
                    expand_usage_snap, llm_usage.snapshot()))

        intent_override = typed_intent
        if not intent and expansion.get('intent'):
            try:
                intent_override = intent_from_string(
                    expansion['intent'])
            except ValueError:
                pass

        assert_fingerprint_unchanged_for_sync(backend, bound_fp)
        query_vec = None
        try:
            query_vec = ec.embed(keyword_str)
        except Exception as exc:
            logger.warning(
                'recall query embed failed (%s): %s;'
                ' degrading to keyword path',
                type(exc).__name__, exc)

        resp = intent_aware_recall(
            backend, keyword_str, query_vec,
            limit,
            intent_override=intent_override, rerank=rerank,
            category=cat, source=source, min_score=min_score)

        hits = [{'id': r['insight'].id[:8], 'via': r.get('via', ''),
                 'score': round(r['score'], 3),
                 'kw': round(r['signals']['keyword'], 3),
                 'sim': round(r['signals']['similarity'], 3),
                 'gr': round(r['signals']['graph'], 3)}
                for r in resp['results']]
        try:
            with backend.transaction():
                for r in resp['results']:
                    backend.nodes.increment_access_count(r['insight'].id)
                    r['insight'].access_count += 1
                # `limit` is the REQUESTED page size, not len(hits):
                # the returned count cannot distinguish a thin page
                # from a small ask, which is why the old payload
                # could not tell which one produced hits_median = 5.
                backend.oplog.log(
                    operation='recall-detail', insight_id='',
                    detail=json.dumps({'intent': resp['meta']['intent'],
                                       'q': keyword_str[:80],
                                       'limit': limit,
                                       'session': session,
                                       'hits': hits}))
        except sqlite3.OperationalError as exc:
            logger.debug(
                'recall_bookkeep_skipped detail q=%r: %s',
                keyword_str, exc)

        out = {
            'results': [
                {
                    'insight': project(r['insight']),
                    'score': r['score'],
                    'intent': r['intent'],
                    'signals': r['signals'],
                    }
                for r in resp['results']
                ],
            'meta': resp['meta'],
            }
        _json_out(out)


def _not_current_reason(ins: 'Insight | None', id: str) -> str:
    """Return why `ins` is not a current row, or `''` when it is.

    Parameters
    ----------
    ins : Insight | None
        The row as read through `get_include_deleted`, or None.
    id : str
        The id the caller asked for, for the message.

    Returns
    -------
    str
        `''` for a current row; otherwise one of `not found`,
        `was forgotten`, or `is superseded by <successor>`.
    """
    if ins is None:
        return f'insight {id} not found'
    if ins.deleted_at is not None:
        return f'insight {id} was forgotten'
    if ins.superseded_by:
        return f'insight {id} is superseded by {ins.superseded_by}'
    return ''


def _forget_insight(backend: 'Backend', id: str) -> None:
    """Soft-delete `id` and write a forget oplog row carrying `before`.

    A superseded row may be forgotten; a missing or already forgotten
    one is refused with the reason.
    """
    from memman.store.model import insight_to_delta_dict
    with backend.transaction():
        before_ins = backend.nodes.get_include_deleted(id)
        if before_ins is None:
            raise click.ClickException(f'insight {id} not found')
        if before_ins.deleted_at is not None:
            raise click.ClickException(f'insight {id} was forgotten')
        if not backend.nodes.soft_delete(id):
            raise click.ClickException(f'insight {id} not found')
        backend.oplog.log(
            operation='forget', insight_id=id, detail='',
            before=insight_to_delta_dict(before_ins))


@claude_callable
@cli.command()
@click.argument('id')
@click.pass_context
def forget(ctx: click.Context, id: str) -> None:
    """Soft-delete an insight. Rejected when the scheduler is stopped."""
    _require_started('write')

    with _active_backend(ctx) as backend:
        _forget_insight(backend, id)
        _json_out({
            'id': id,
            'status': 'deleted',
            'message': 'Insight soft-deleted successfully',
            })


@claude_callable
@cli.command()
@click.argument('id')
@click.argument('content', nargs=-1, required=True)
@click.option('--cat', default='fact', help='Category')
@click.option('--imp', default=3, type=int,
              help='Sort key for listings and tie-breaks (1-5, default 3)')
@click.option('--source', default='user', help='Source')
@click.option('--entities', default='', help='Comma-separated entities')
@click.option('--reconcile/--no-reconcile', 'reconcile', default=False,
              help=('Run LLM reconciliation against existing insights.'
                    ' Default: skip — replace targets a specific id.'))
@click.option('--session', default='',
              envvar=[config.SESSION_ID, config.CLAUDE_SESSION_ID],
              help='Session id for the temporal chain (defaults to'
                   ' $MEMMAN_SESSION_ID, then $CLAUDE_CODE_SESSION_ID)')
@click.pass_context
def replace(ctx: click.Context, id: str, content: tuple[str, ...],
            cat: str, imp: int, source: str,
            entities: str, reconcile: bool, session: str) -> None:
    """Replace an insight by ID with new content via the queue.

    Notes
    -----
    - The replaced insight is superseded, not deleted: it keeps its
      content behind `superseded_by`, leaves every recall and listing,
      and its edges move to the successor. `insights show <id>
      --history` reads the chain back; `unsupersede` reverses it once
      the successor is forgotten.
    - The id must be current. A forgotten or already superseded id is
      refused, the latter naming its successor.
    - Unflagged `--cat` / `--imp` / `--source` / `--entities` inherit
      the replaced insight's values; the inherited source is passed
      through verbatim (idempotency rides on the queue uuid, so a
      non-null source hint no longer suppresses the replay check).
    - `--session` does not inherit: the successor carries the session
      that wrote it, so it enters that session's backbone chain.
      It also inherits the replaced insight's edges, including that
      row's own backbone edge, so a cross-session replace leaves the
      successor bridging both chains at full weight.
    """
    _require_started('write')

    content_str = ' '.join(content)
    content_bytes = len(content_str.encode('utf-8'))
    if content_bytes > 8000:
        raise click.ClickException(
            f'content too long ({content_bytes} bytes, max 8000);'
            ' consider chunking into multiple remember calls')

    if cat not in VALID_CATEGORIES:
        valid = ', '.join(sorted(VALID_CATEGORIES))
        raise click.ClickException(
            f'invalid category {cat!r}; valid: {valid}')
    if imp < 1 or imp > 5:
        raise click.ClickException(
            f'importance must be 1-5, got {imp}')

    from memman.search.quality import check_content_quality
    quality_warnings = check_content_quality(content_str)

    data_dir_val = ctx.obj['data_dir']
    name = _resolve_store_name(data_dir_val, ctx.obj['store'])

    with _active_backend(ctx) as backend:
        old = backend.nodes.get_include_deleted(id)
    reason = _not_current_reason(old, id)
    if reason:
        if old is not None and old.superseded_by:
            reason += (f'; replace {old.superseded_by}, or run'
                       f' insights show {id} --history')
        raise click.ClickException(reason)

    cat_src = ctx.get_parameter_source('cat')
    imp_src = ctx.get_parameter_source('imp')
    source_src = ctx.get_parameter_source('source')
    entities_src = ctx.get_parameter_source('entities')
    if cat_src != click.core.ParameterSource.COMMANDLINE:
        cat = old.category
    if imp_src != click.core.ParameterSource.COMMANDLINE:
        imp = old.importance
    if source_src != click.core.ParameterSource.COMMANDLINE:
        source = old.source
    if entities_src != click.core.ParameterSource.COMMANDLINE:
        entities = ','.join(old.entities) if old.entities else ''
    entities_clean = ','.join(_parse_entities(entities))

    from memman.queue import enqueue, queue_db
    with queue_db(data_dir_val) as conn:
        row_id, queue_uuid = enqueue(
            conn, store=name, content=content_str,
            hint_cat=cat, hint_imp=imp,
            hint_source=source,
            hint_entities=entities_clean or None,
            hint_replaced_id=id,
            hint_no_reconcile=not reconcile,
            session_id=session or None,
            priority=0)
    _json_out({
        'action': 'queued',
        'queue_id': row_id,
        'queue_uuid': queue_uuid,
        'store': name,
        'replaced_id': id,
        'quality_warnings': quality_warnings,
        })


@claude_callable
@cli.command()
@click.argument('predecessor_id')
@click.argument('successor_id')
@click.pass_context
def supersede(ctx: click.Context, predecessor_id: str,
              successor_id: str) -> None:
    """Mark one current insight as superseded by another current one.

    The manual counterpart of the reconciler's SUPERSEDE, and the only
    way to link two rows that BOTH already exist: `replace` always
    inserts a new row. Neither row's content changes. The predecessor
    leaves every recall and listing, keeps its content behind
    `superseded_by`, and hands its edges to the successor.

    \b
    Parameters
    ----------
    predecessor_id : str
        The row being superseded; must be current.
    successor_id : str
        The row that now holds the topic; must be current and a
        different row.

    \b
    Returns
    -------
    JSON
        `{predecessor, successor, edges_moved}`.

    \b
    Notes
    -----
    - Refused, naming the reason, when either id is missing, forgotten
      or already superseded, or when both name the same row. A
      successor may take a second predecessor: a correction the
      reconciler wrote as a merge already has one, and curating a
      sibling claim onto it joins the two chains.
    - `insights show <predecessor_id> --history` reads the link back;
      `unsupersede <predecessor_id>` reverses it once the successor is
      forgotten.

    \b
    Examples
    --------
    memman supersede 16c6c667-... b2b971ae-...
    memman insights show 16c6c667-... --history
    """  # noqa: D301, D410, D411
    _require_started('write')
    if predecessor_id == successor_id:
        raise click.ClickException(
            'predecessor and successor are the same insight')
    # Lazy: the pipeline module imports the LLM and embedding stacks,
    # which every read-only command would otherwise pay for at start.
    from memman.pipeline.remember import move_edges
    from memman.store.model import insight_to_delta_dict
    with _active_backend(ctx) as backend, backend.transaction():
        old = backend.nodes.get_include_deleted(predecessor_id)
        reason = _not_current_reason(old, predecessor_id)
        if reason:
            raise click.ClickException(reason)
        reason = _not_current_reason(
            backend.nodes.get_include_deleted(successor_id),
            successor_id)
        if reason:
            raise click.ClickException(reason)
        carried = backend.edges.by_node(predecessor_id)
        if not backend.nodes.supersede(predecessor_id, successor_id):
            raise click.ClickException(
                f'insight {predecessor_id} changed under this'
                ' command; re-read it')
        moved = move_edges(
            backend, predecessor_id, successor_id, carried)
        backend.oplog.log(
            operation='supersede', insight_id=predecessor_id,
            detail=f'replaced by {successor_id}',
            before=insight_to_delta_dict(old))
    _json_out({
        'predecessor': predecessor_id,
        'successor': successor_id,
        'edges_moved': moved,
        })


@claude_callable
@cli.command()
@click.argument('id')
@click.pass_context
def unsupersede(ctx: click.Context, id: str) -> None:
    """Bring a superseded insight back once its successor is gone.

    Clears the row's `superseded_by`, re-embeds its content with the
    store's embedder, refreshes its keyword tokens, and rebuilds its
    entity and semantic edges, so it re-enters recall as a current
    row. No temporal edge is minted (the row is not a new event), and
    the causal and manual edges the supersession moved onto the
    forgotten successor are not restored.

    \b
    Parameters
    ----------
    id : str
        A superseded row whose successor has been forgotten.

    \b
    Returns
    -------
    JSON
        `{id, was_superseded_by, edges_created: {entity, semantic}}`.

    \b
    Notes
    -----
    - Refused while the successor is current: two current rows for
      one fact is the state supersession removes. Forget or supersede
      the successor first. Refused likewise when the successor was
      itself superseded; the chain unwinds from its head.
    - Refused for a missing, forgotten, or not-superseded row, and
      when the embed fails: the row then stays superseded instead of
      returning current with a missing or stale-width vector.

    \b
    Examples
    --------
    memman forget b2b971ae-...
    memman unsupersede 16c6c667-...
    """  # noqa: D301, D410, D411
    _require_started('write')
    name = _resolve_store_name(ctx.obj['data_dir'], ctx.obj['store'])
    # Lazy: the embedding and graph stacks are only paid for here.
    import httpx
    from memman.embed.fingerprint import bound_embedder
    from memman.exceptions import EmbedCredentialError
    from memman.graph.engine import _resolve_semantic_threshold
    from memman.graph.entity import create_entity_edges
    from memman.graph.semantic import create_semantic_edges
    from memman.store.model import insight_to_delta_dict
    with _active_backend(ctx) as backend:
        row = backend.nodes.get_include_deleted(id)
        if row is None:
            raise click.ClickException(f'insight {id} not found')
        if row.deleted_at is not None:
            raise click.ClickException(f'insight {id} was forgotten')
        if not row.superseded_by:
            raise click.ClickException(f'insight {id} is not superseded')
        successor_id = row.superseded_by
        successor = backend.nodes.get_include_deleted(successor_id)
        if successor is not None and successor.deleted_at is None:
            if successor.superseded_by is None:
                raise click.ClickException(
                    f'insight {id} is superseded by {successor_id}, which'
                    f' is current; forget or supersede {successor_id}'
                    ' first')
            raise click.ClickException(
                f'insight {id} is superseded by {successor_id}, which was'
                ' itself superseded; unsupersede the chain from its head'
                ' first')
        before = insight_to_delta_dict(row)
        ec = bound_embedder(backend)
        threshold = _resolve_semantic_threshold(backend, store_name=name)
        # The embed is a network call; it runs before the transaction
        # so a slow provider never holds the store's write lock, and a
        # failure leaves the row superseded rather than current with a
        # missing or stale-width vector.
        try:
            vec = ec.embed(row.content)
        except EmbedCredentialError:
            raise
        except (httpx.HTTPError, RuntimeError) as exc:
            raise click.ClickException(
                f'insight {id} not restored: embedding failed ({exc});'
                ' the row stays superseded, retry when the provider'
                ' answers')
        with backend.transaction():
            if not backend.nodes.unsupersede(id, successor_id):
                raise click.ClickException(
                    f'insight {id} changed under this command; re-read it')
            backend.nodes.update_embedding(id, vec, ec.model)
            backend.nodes.update_entities(id, row.entities)
            row.superseded_by = None
            # Built after the pointer is cleared so the row's own
            # vector is in the cache the semantic builder reads.
            embed_cache = dict(backend.nodes.iter_embeddings_as_vecs())
            edges_created = {
                'entity': create_entity_edges(backend, row),
                'semantic': create_semantic_edges(
                    backend, row, embed_cache, threshold=threshold),
                }
            backend.nodes.stamp_linked(id)
            backend.oplog.log(
                operation='unsupersede', insight_id=id,
                detail=f'was superseded by {successor_id}', before=before)
    _json_out({
        'id': id,
        'was_superseded_by': successor_id,
        'edges_created': edges_created,
        })


@claude_callable
@graph.command('link')
@click.argument('source_id')
@click.argument('target_id')
@click.option('--type', 'edge_type', default='semantic', help='Edge type')
@click.option('--weight', default=0.5, type=float, help='Edge weight')
@click.option('--meta', default='', help='JSON metadata')
@click.pass_context
def graph_link(ctx: click.Context, source_id: str, target_id: str,
               edge_type: str, weight: float, meta: str) -> None:
    """Create a manual edge between two insights."""
    _require_started('create edges')

    if edge_type not in VALID_EDGE_TYPES:
        raise click.ClickException(
            f'invalid edge type {edge_type!r}')

    if weight < 0.0 or weight > 1.0:
        raise click.ClickException(
            'weight must be between 0.0 and 1.0')

    metadata: dict[str, str] = {}
    if meta:
        try:
            metadata = json.loads(meta)
        except json.JSONDecodeError as e:
            raise click.ClickException(
                f'invalid JSON metadata: {e}')
        if not isinstance(metadata, dict):
            raise click.ClickException(
                'metadata must be a JSON object, not '
                + type(metadata).__name__)
    metadata.setdefault('created_by', 'claude')

    if source_id == target_id:
        raise click.ClickException(
            'cannot link an insight to itself')

    now = datetime.now(timezone.utc)
    with _active_backend(ctx) as backend:
        with backend.transaction():
            if backend.nodes.get(source_id) is None:
                raise click.ClickException(
                    f'insight {source_id} not found or not current')
            if backend.nodes.get(target_id) is None:
                raise click.ClickException(
                    f'insight {target_id} not found or not current')

            existing_weight = backend.edges.get_weight(
                source_id, target_id, edge_type)

            backend.edges.upsert(Edge(
                source_id=source_id, target_id=target_id,
                edge_type=edge_type, weight=weight,
                metadata=metadata, created_at=now))
            backend.edges.upsert(Edge(
                source_id=target_id, target_id=source_id,
                edge_type=edge_type, weight=weight,
                metadata=metadata, created_at=now))
            backend.oplog.log(
                operation='link', insight_id=source_id,
                detail=f'{source_id} <-> {target_id} ({edge_type})')

        actual_weight = (
            backend.edges.get_weight(source_id, target_id, edge_type)
            or weight)
        out = {
            'status': 'linked',
            'source_id': source_id,
            'target_id': target_id,
            'edge_type': edge_type,
            'weight': actual_weight,
            'metadata': metadata,
            }
        if existing_weight is not None and existing_weight > weight:
            out['warning'] = (
                f'existing weight {existing_weight} > requested'
                f' {weight}; kept higher')
        _json_out(out)


@claude_callable
@graph.command('related')
@click.argument('id')
@click.option('--edge', default='', help='Filter by edge type')
@click.option('--depth', default=2, type=int, help='Max traversal depth')
@click.pass_context
def graph_related(ctx: click.Context, id: str, edge: str,
                  depth: int) -> None:
    """Find connected insights via graph traversal."""
    from memman.graph.bfs import BFSOptions, bfs

    with _active_backend(ctx) as backend:
        nodes = bfs(backend, id, BFSOptions(
            max_depth=depth, max_nodes=0, edge_filter=edge))
        out = []
        for n in nodes:
            entry: dict = {
                'id': n['insight'].id,
                'content': n['insight'].content,
                'category': n['insight'].category,
                'importance': n['insight'].importance,
                'depth': n['hop'],
                }
            if n.get('via_edge'):
                entry['via_edge_type'] = n['via_edge']
            out.append(entry)
        _json_out(out)


@queue.command('list')
@click.option('--limit', default=50, type=int, help='Max results')
@click.pass_context
def queue_list(ctx: click.Context, limit: int) -> None:
    """List recent queue rows."""
    from memman.queue import list_rows, queue_db, stats
    with queue_db(ctx.obj['data_dir']) as conn:
        _json_out({
            'stats': stats(conn),
            'rows': list_rows(conn, limit=limit),
            })


@queue.command('failed')
@click.option('--limit', default=50, type=int, help='Max results')
@click.pass_context
def queue_failed(ctx: click.Context, limit: int) -> None:
    """List failed queue rows."""
    from memman.queue import STATUS_FAILED, list_rows, queue_db, stats
    with queue_db(ctx.obj['data_dir']) as conn:
        _json_out({
            'stats': stats(conn),
            'rows': list_rows(conn, status=STATUS_FAILED, limit=limit),
            })


@queue.command('skipped')
@click.option('--limit', default=50, type=int, help='Max results')
@click.pass_context
def queue_skipped(ctx: click.Context, limit: int) -> None:
    """List drained writes that stored no insight.

    A write whose extraction came back empty, or whose every fact
    reconciled onto an existing insight, completes as `done` and is
    purged from the queue a minute later. This ledger keeps its full
    content and the reason nothing was stored.

    Parameters
    ----------
    limit : int, default 50
        Maximum ledger rows to return, newest first.

    Examples
    --------
    memman scheduler queue skipped --limit 20
    """
    from memman.queue import list_skipped, queue_db, stats
    with queue_db(ctx.obj['data_dir']) as conn:
        _json_out({
            'stats': stats(conn),
            'rows': list_skipped(conn, limit=limit),
            })


@queue.command('show')
@click.argument('row_id', type=int)
@click.pass_context
def queue_show(ctx: click.Context, row_id: int) -> None:
    """Print the full content of a queue row."""
    from memman.queue import get_row, queue_db
    with queue_db(ctx.obj['data_dir']) as conn:
        row = get_row(conn, row_id)
        if row is None:
            raise click.ClickException(f'queue row {row_id} not found')
        _json_out(row)


@queue.command('retry')
@click.argument('row_id', type=int, required=False)
@click.option('--all-stale', 'all_stale', is_flag=True, default=False,
              help='Re-queue every row currently in status=stale')
@click.pass_context
def queue_retry(
        ctx: click.Context,
        row_id: int | None,
        all_stale: bool) -> None:
    """Re-queue a failed row by id, or every stale row with --all-stale."""
    from memman.queue import queue_db, retry_row, retry_stale
    if all_stale and row_id is not None:
        raise click.ClickException(
            'pass either ROW_ID or --all-stale, not both')
    if not all_stale and row_id is None:
        raise click.ClickException(
            'pass a ROW_ID or --all-stale')
    with queue_db(ctx.obj['data_dir']) as conn:
        if all_stale:
            count = retry_stale(conn)
            _json_out({'action': 'requeued', 'count': count})
            return
        if not retry_row(conn, row_id):
            raise click.ClickException(
                f'queue row {row_id} not found or not in failed state')
        _json_out({'action': 'requeued', 'queue_id': row_id})


@queue.command('purge')
@click.option('--done', is_flag=True, default=False,
              help='Delete all rows with status=done')
@click.option('--stale', 'stale', is_flag=True, default=False,
              help='Delete all rows with status=stale')
@click.option('--skipped', 'skipped', is_flag=True, default=False,
              help='Empty the skipped-write ledger')
@click.pass_context
def queue_purge(ctx: click.Context, done: bool, stale: bool,
                skipped: bool) -> None:
    """Remove completed or stale queue rows, or empty the skip ledger.

    Parameters
    ----------
    done : bool
        Delete every row in status `done`.
    stale : bool
        Delete every row in status `stale`.
    skipped : bool
        Empty the `skipped_writes` ledger. Nothing else prunes it:
        `purge_done` never reaches it and `purge_store` clears only
        one store, so the full content of every skipped write is kept
        until this runs.

    Examples
    --------
    memman scheduler queue purge --done
    memman scheduler queue purge --skipped
    """
    chosen = [f for f in (done, stale, skipped) if f]
    if len(chosen) > 1:
        raise click.ClickException(
            'pass exactly one of --done, --stale, --skipped')
    if not chosen:
        raise click.ClickException(
            'pass --done, --stale, or --skipped to confirm deletion')
    from memman.queue import purge_done, purge_skipped, purge_stale, queue_db
    with queue_db(ctx.obj['data_dir']) as conn:
        if skipped:
            deleted = purge_skipped(conn)
        elif done:
            deleted = purge_done(conn)
        else:
            deleted = purge_stale(conn)
        _json_out({'deleted': deleted})


@scheduler.command('status')
@click.option('--text', 'text_output', is_flag=True, default=False,
              help='Human-readable output (default: JSON)')
@click.pass_context
def scheduler_status(ctx: click.Context, text_output: bool) -> None:
    """Show scheduler install state, interval, next run, log paths,
    and the most recent worker-drain summary from worker_runs.
    """
    from memman.queue import last_worker_run, queue_db
    from memman.setup.scheduler import status
    result = status()
    logs_dir = pathlib.Path.home() / '.memman' / 'logs'
    log_path = logs_dir / 'enrich.log'
    err_path = logs_dir / 'enrich.err'
    # The rotated worker log follows --data-dir while the two unit
    # redirects beside it never do, so it cannot be built off logs_dir.
    stack_path = pathlib.Path(ctx.obj['data_dir']) / 'logs' / 'memman.log'
    result['log_path'] = str(log_path)
    result['err_path'] = str(err_path)
    result['stack_path'] = str(stack_path)
    for key, path in (('log_mtime', log_path), ('err_mtime', err_path),
                      ('stack_mtime', stack_path)):
        try:
            result[key] = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
                ).isoformat()
        except OSError:
            result[key] = None

    result['last_run'] = None
    try:
        with queue_db(ctx.obj['data_dir']) as conn:
            result['last_run'] = last_worker_run(conn)
    except Exception as exc:
        logger.debug(f'worker_runs lookup failed: {exc}')

    _scheduler_emit(result, text_output)


@scheduler.command('start')
@click.option('--text', 'text_output', is_flag=True, default=False,
              help='Human-readable output (default: JSON)')
@click.pass_context
def scheduler_start(ctx: click.Context, text_output: bool) -> None:
    """Start the scheduler. Worker drains; writes are accepted.

    Idempotent. Sweeps long-stalled queue rows to `stale` so they can
    be retried with `scheduler queue retry --all-stale`.
    """
    from memman.queue import mark_stale_on_resume, queue_db
    from memman.setup.scheduler import start
    try:
        result = start()
    except (FileNotFoundError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc

    data_dir_val = ctx.obj['data_dir']
    with queue_db(data_dir_val) as conn:
        n_stale = mark_stale_on_resume(conn)
    if n_stale:
        result['marked_stale'] = n_stale
        result.setdefault('actions', []).append(
            f"moved {n_stale} long-pending rows to status='stale'"
            ' (retry with `memman scheduler queue retry --all-stale`)')
    _scheduler_emit(result, text_output)


@scheduler.command('stop')
@click.option('--text', 'text_output', is_flag=True, default=False,
              help='Human-readable output (default: JSON)')
@click.pass_context
def scheduler_stop(ctx: click.Context, text_output: bool) -> None:
    """Stop the scheduler. Trigger files stay; memman becomes recall-only.

    Writes (`remember`/`replace`/`forget`/`graph link`/`graph
    rebuild`) reject until `scheduler start` re-arms the worker. Use
    `memman uninstall` to remove trigger files entirely.
    """
    from memman.setup.scheduler import stop
    try:
        result = stop()
    except (FileNotFoundError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    _scheduler_emit(result, text_output)


def _scheduler_emit(result: dict, text_output: bool) -> None:
    """Render a scheduler-command result dict.

    JSON by default for script consumers; `--text` renders flat
    key:value lines and an indented `actions` list when present.
    """
    if not text_output:
        _json_out(result)
        return
    actions = result.get('actions') or []
    for key, value in result.items():
        if key == 'actions':
            continue
        click.echo(f'{key}: {value}')
    if actions:
        click.echo('actions:')
        for action in actions:
            click.echo(f'  - {action}')


@scheduler.command('install')
@click.option('--interval', type=int, default=None,
              help=('Polling interval in seconds (min 60 for'
                    ' systemd/launchd). Default: 60. For sub-minute'
                    ' intervals, use serve mode instead'
                    ' (`memman scheduler serve --interval N`).'))
@click.option('--llm-endpoint', type=str, default=None,
              help='LLM endpoint URL to seed into the env file.')
@click.option('--embed-provider',
              type=click.Choice(list(_EMBED_PROVIDER_CHOICES)),
              default=None,
              help='Embed provider to seed into the env file.')
@click.pass_context
def scheduler_install(ctx: click.Context, interval: int | None,
                      llm_endpoint: str | None,
                      embed_provider: str | None) -> None:
    """Install the scheduler unit only (no agent integration).

    Reads the API keys required by the configured endpoint + embed
    provider (e.g., MEMMAN_LLM_API_KEY + MEMMAN_VOYAGE_API_KEY for the
    default OpenRouter + voyage pair) from env and writes them to
    ~/.memman/env (mode 600), then installs the systemd timer or
    launchd plist that runs the worker every interval. For full
    agent-integration setup (hooks, skill, scheduler), use
    `memman install`.
    """
    from memman.exceptions import ConfigError
    from memman.setup.claude import _reject_flag_file_conflicts
    from memman.setup.scheduler import DEFAULT_INTERVAL_SECONDS
    from memman.setup.scheduler import _write_env_keys, install

    data_dir = ctx.obj['data_dir']
    _reject_flag_file_conflicts(
        data_dir=data_dir, backend=None, pg_dsn=None,
        llm_endpoint=llm_endpoint, embed_provider=embed_provider)
    endpoint_seed: dict[str, str] = {}
    if llm_endpoint:
        endpoint_seed[config.LLM_ENDPOINT] = llm_endpoint
    if embed_provider:
        endpoint_seed[config.EMBED_PROVIDER] = embed_provider
    if endpoint_seed:
        _write_env_keys(endpoint_seed, data_dir=data_dir)
        config.reset_file_cache()

    try:
        knobs = config.collect_install_knobs(data_dir)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    seconds = interval if interval is not None else DEFAULT_INTERVAL_SECONDS
    if seconds < 60:
        raise click.ClickException(
            '--interval must be at least 60 seconds for systemd/launchd.'
            ' For sub-minute intervals, set MEMMAN_SCHEDULER_KIND=serve'
            ' and run `memman scheduler serve --interval N` instead.')
    try:
        result = install(data_dir, knobs, seconds)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    _json_out(result)


@scheduler.command('uninstall')
@click.pass_context
def scheduler_uninstall(ctx: click.Context) -> None:
    """Remove the scheduler unit only (leaves agent integration intact).

    Clears scheduler state files and removes the systemd timer/service or
    launchd plist. `memman uninstall` does this AND removes hooks/skill
    integration.
    """
    from memman.setup.scheduler import uninstall
    _json_out(uninstall(data_dir=ctx.obj['data_dir']))


@scheduler.command('interval')
@click.option('--seconds', type=int, default=None,
              help=('New interval in seconds. Omit to show current.'
                    ' min 60 for systemd/launchd; 0 (continuous) or any'
                    ' non-negative value allowed for serve mode.'))
@click.pass_context
def scheduler_interval(ctx: click.Context, seconds: int | None) -> None:
    """Show or set the scheduler interval."""
    from memman.setup.scheduler import change_interval, status
    if seconds is None:
        s = status()
        _json_out({
            'platform': s['platform'],
            'interval_seconds': s['interval_seconds'],
            'installed': s['installed'],
            })
        return
    try:
        result = change_interval(ctx.obj['data_dir'], seconds)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    _json_out(result)


@scheduler.command('trigger')
@click.pass_context
def scheduler_trigger(ctx: click.Context) -> None:
    """Dispatch a drain now. The command does not wait for it to finish.

    Rejected when the scheduler is stopped. systemd uses
    `systemctl --user start --no-block` and launchd uses `launchctl
    start`, so a `dispatched` response means the run was queued, not
    that it has started or finished. Read `memman log worker` for the
    outcome.
    """
    _require_started('trigger drain')
    from memman.setup.scheduler import trigger
    try:
        result = trigger()
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    _json_out(result)


@scheduler.group('debug', no_args_is_help=True)
def scheduler_debug() -> None:
    """Toggle persistent JSONL trace state for scheduler-fired runs.

    Writes ~/.memman/debug.state which `trace.is_enabled()` reads as a
    fallback when MEMMAN_DEBUG is unset. Affects future scheduler-fired
    drains and any CLI invocation in a shell that does not export
    MEMMAN_DEBUG. Trace logs land at ~/.memman/logs/debug.log (mode
    600) and include raw LLM request/response bodies — including
    memory content. Turn off when done.
    """


@scheduler_debug.command('on')
@click.pass_context
def scheduler_debug_on(ctx: click.Context) -> None:
    """Enable persistent debug traces."""
    from memman.setup.scheduler import set_debug
    actions = set_debug(True)
    logs_dir = pathlib.Path.home() / '.memman' / 'logs'
    click.echo(
        '[memman] debug traces ENABLED -- raw LLM request/response bodies'
        f' (including memory content) will be written to {logs_dir}/debug.log'
        ' (mode 600). Turn off with: memman scheduler debug off',
        err=True)
    _json_out({'debug': True, 'actions': actions})


@scheduler_debug.command('off')
@click.pass_context
def scheduler_debug_off(ctx: click.Context) -> None:
    """Disable persistent debug traces; existing debug.log files are kept."""
    from memman.setup.scheduler import set_debug
    actions = set_debug(False)
    _json_out({'debug': False, 'actions': actions})


@scheduler_debug.command('status')
@click.pass_context
def scheduler_debug_status(ctx: click.Context) -> None:
    """Show whether persistent debug traces are enabled."""
    from memman.setup.scheduler import get_debug
    logs_dir = pathlib.Path.home() / '.memman' / 'logs'
    debug_log = logs_dir / 'debug.log'
    _json_out({
        'debug': get_debug(),
        'debug_log': str(debug_log),
        'debug_log_exists': debug_log.is_file(),
        })


@cli.group(invoke_without_command=True)
@click.pass_context
def store(ctx: click.Context) -> None:
    """Manage named memory stores."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(store_list)


@store.command('list')
@click.pass_context
def store_list(ctx: click.Context) -> None:
    """List all stores as JSON (stores[], active)."""
    data_dir = ctx.obj['data_dir']
    stores = list_stores(data_dir)
    active = _resolve_store_name(data_dir, ctx.obj['store']) if stores else None
    _json_out({'stores': stores, 'active': active})


@store.command('create')
@click.argument('name')
@click.pass_context
def store_create(ctx: click.Context, name: str) -> None:
    """Create a new store."""
    data_dir = ctx.obj['data_dir']
    if not valid_store_name(name):
        raise click.ClickException(
            f'invalid store name {name!r}')
    if name in factory.list_stores(data_dir):
        raise click.ClickException(
            f'store "{name}" already exists')
    from memman.session import active_store
    with active_store(data_dir=data_dir, store=name) as backend:
        path = backend.path
    _json_out({'action': 'created', 'store': name, 'path': path})


@store.command('use')
@click.argument('name')
@click.pass_context
def store_use(ctx: click.Context, name: str) -> None:
    """Switch the active store."""
    data_dir = ctx.obj['data_dir']
    if name not in factory.list_stores(data_dir):
        raise click.ClickException(
            f"store \"{name}\" does not exist"
            f" (use 'memman store create {name}' first)")
    write_active(data_dir, name)
    _json_out({'action': 'set', 'store': name})


@store.command('remove')
@click.argument('name')
@click.option('--yes', is_flag=True, default=False,
              help='Skip confirmation prompt (for scripted use).')
@click.pass_context
def store_remove(ctx: click.Context, name: str, yes: bool) -> None:
    """Remove a store (prompts unless --yes).

    Also drops the store's per-store env keys, so a removed store
    leaves no backend selection or Postgres DSN (password included)
    behind in the env file.
    """
    data_dir = ctx.obj['data_dir']
    if name not in factory.list_stores(data_dir):
        raise click.ClickException(
            f"store \"{name}\" does not exist"
            f" (use 'memman store create {name}' first)")
    active = read_active(data_dir)
    if name == active:
        raise click.ClickException(
            f"cannot remove the active store \"{name}\""
            f" (switch first with 'memman store use <other>')")
    if not yes:
        click.confirm(
            f'Drop store "{name}" (and all of its data)?',
            abort=True)
    # Notes:
    # - A backend refusing the drop is an operator-facing failure,
    #   not a bug: an unreachable Postgres, or a store name the
    #   backend will not accept as an identifier.
    # - Raising here leaves the env keys in place on purpose. The
    #   store still exists, and dropping its routing would send the
    #   next read to the default backend instead of the one holding
    #   the data.
    try:
        factory.drop_store(name, data_dir)
    except BackendError as exc:
        raise click.ClickException(
            f'could not remove store {name!r}: {exc}')
    # Lazy import: memman.setup.scheduler costs ~4 ms of interpreter
    # startup (measured), which every CLI call would pay -- including
    # the per-prompt recall hook -- for a cold-path command.
    from memman.setup.scheduler import _write_env_keys_with_flock
    per_store_keys = {
        f'{prefix}{name}' for prefix, _, _ in config.PER_STORE_KEY_SPECS}
    stale = per_store_keys & set(
        config.parse_env_file(config.env_file_path(data_dir)))
    if stale:
        _write_env_keys_with_flock({}, removes=stale, data_dir=data_dir)
    _json_out({
        'action': 'removed',
        'store': name,
        'env_keys_removed': sorted(stale),
        })


@cli.group(invoke_without_command=True)
@click.pass_context
def backup(ctx: click.Context) -> None:
    """External, scheduled backups of the whole store layout.

    Backups write only to a user-specified external directory, never
    into ~/.memman/ (that dir is per-host and disposable). No
    subcommand is claude-callable: `run` writes an external filesystem
    and `restore` is destructive.
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(backup_status)


@backup.command('run')
@click.argument('target', required=False)
@click.pass_context
def backup_run(ctx: click.Context, target: str | None) -> None:
    """Build one backup bundle now (TARGET or MEMMAN_BACKUP_TARGET)."""
    data_dir = ctx.obj['data_dir']
    target = target or config.get(config.BACKUP_TARGET)
    if not target:
        raise click.ClickException(
            'no target; pass TARGET or set MEMMAN_BACKUP_TARGET'
            " via 'memman backup schedule'")
    from memman.backup import run_backup
    _json_out({'action': 'backed_up', **run_backup(data_dir, target)})


@backup.command('schedule')
@click.argument('cron')
@click.argument('target')
@click.option('--keep', type=int, default=7,
              help='Number of bundles to retain (default 7).')
@click.pass_context
def backup_schedule(ctx: click.Context, cron: str, target: str,
                    keep: int) -> None:
    """Install a scheduled backup: CRON expression writing to TARGET dir.

    CRON is a 5-field expression (e.g. '0 3 * * *' for 03:00 daily).
    TARGET is created if it does not exist. The cron string is
    translated to the host's native scheduler at install time.
    """
    data_dir = ctx.obj['data_dir']
    from memman.backup.cron import cron_to_oncalendar
    try:
        cron_to_oncalendar(cron)
    except ValueError as exc:
        raise click.ClickException(f'invalid cron expression: {exc}')
    fields = cron.split()
    if len(fields) == 5 and fields[2] != '*' and fields[4] != '*':
        from memman.setup.scheduler import detect_scheduler
        try:
            systemd_host = detect_scheduler() == 'systemd'
        except RuntimeError:
            systemd_host = False
        if systemd_host:
            click.echo(
                'Warning: cron restricts BOTH day-of-month and day-of-week.'
                ' systemd OnCalendar evaluates these as AND (cron uses OR),'
                ' so the backup fires only when both match.', err=True)
    target_path = os.path.expanduser(target)
    os.makedirs(target_path, exist_ok=True)
    from memman.setup.scheduler import _write_env_keys, install_backup
    _write_env_keys(
        {config.BACKUP_CRON: cron,
         config.BACKUP_TARGET: target_path,
         config.BACKUP_KEEP: str(keep)},
        data_dir=data_dir)
    result = install_backup(data_dir, cron)
    _json_out({
        'action': 'scheduled', 'cron': cron,
        'target': target_path, 'keep': keep, **result})


@backup.command('unschedule')
@click.pass_context
def backup_unschedule(ctx: click.Context) -> None:
    """Remove the scheduled backup trigger (keeps the env config)."""
    data_dir = ctx.obj['data_dir']
    from memman.setup.scheduler import uninstall_backup
    _json_out({'action': 'unscheduled', **uninstall_backup(data_dir)})


@backup.command('list')
@click.argument('target', required=False)
@click.pass_context
def backup_list(ctx: click.Context, target: str | None) -> None:
    """List bundles at TARGET (or MEMMAN_BACKUP_TARGET) from sidecars."""
    target = target or config.get(config.BACKUP_TARGET)
    if not target:
        raise click.ClickException(
            'no target; pass TARGET or set MEMMAN_BACKUP_TARGET')
    target_path = pathlib.Path(os.path.expanduser(target))
    backups: list[dict] = []
    if target_path.is_dir():
        for sidecar in sorted(
                target_path.glob('memman-backup-*.tar.gz.manifest.json')):
            bundle = sidecar.with_name(
                sidecar.name[:-len('.manifest.json')])
            try:
                manifest = json.loads(sidecar.read_text())
            except (OSError, json.JSONDecodeError):
                manifest = {}
            backups.append({
                'bundle': str(bundle),
                'created_at_utc': manifest.get('created_at_utc'),
                'host': manifest.get('host'),
                'stores': [
                    s.get('name') for s in manifest.get('stores', [])],
                'size_bytes': (
                    bundle.stat().st_size if bundle.exists() else None),
                })
    _json_out({'target': str(target_path), 'backups': backups})


@backup.command('status')
@click.pass_context
def backup_status(ctx: click.Context) -> None:
    """Report backup config, schedule, last fire, and latest bundle."""
    from memman.setup import scheduler as sched

    cron = config.get(config.BACKUP_CRON)
    target = config.get(config.BACKUP_TARGET)
    keep = config.get(config.BACKUP_KEEP)
    out: dict = {
        'cron': cron,
        'target': target,
        'keep': int(keep) if keep and keep.isdigit() else None,
        'last_fired': sched.read_backup_state(),
        'scheduler': None,
        'installed': False,
        'next_run': None,
        'latest_bundle': None,
        }
    try:
        kind = sched.detect_scheduler()
    except RuntimeError:
        kind = None
    out['scheduler'] = kind
    if kind == 'systemd':
        timer = (sched._systemd_unit_dir()
                 / sched.SYSTEMD_BACKUP_TIMER_NAME)
        out['installed'] = timer.exists()
        if out['installed']:
            import subprocess
            try:
                shown = subprocess.run(
                    ['systemctl', '--user', 'show',
                     '--property=NextElapseUSecRealtime', '--value',
                     sched.SYSTEMD_BACKUP_TIMER_NAME],
                    capture_output=True, text=True,
                    check=False, timeout=5)
                out['next_run'] = shown.stdout.strip() or None
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
    elif kind == 'launchd':
        plist = (sched._launchd_agent_dir()
                 / f'{sched.LAUNCHD_BACKUP_LABEL}.plist')
        out['installed'] = plist.exists()
    if target:
        target_path = pathlib.Path(os.path.expanduser(target))
        if target_path.is_dir():
            bundles = sorted(
                target_path.glob('memman-backup-*.tar.gz'))
            out['latest_bundle'] = str(bundles[-1]) if bundles else None
    _json_out(out)


@backup.command('restore')
@click.argument('bundle')
@click.option('--yes', is_flag=True, default=False,
              help='Skip the overwrite confirmation (for scripted use).')
@click.pass_context
def backup_restore(ctx: click.Context, bundle: str, yes: bool) -> None:
    """Restore stores + non-secret config from BUNDLE into the data dir.

    Overwrites local stores. Postgres stores need their DSN configured
    on this host (the DSN is a secret and is not in the bundle); a
    store with no DSN is skipped and reported.
    """
    import shutil
    import tarfile

    data_dir = ctx.obj['data_dir']
    from memman.backup import restore
    from memman.exceptions import EmbedFingerprintError
    from memman.migrate import MigrateError, held_drain_lock

    needs_pg = False
    try:
        with tarfile.open(bundle, 'r:gz') as tar:
            member = tar.extractfile('./manifest.json')
            if member is not None:
                manifest = json.loads(member.read().decode())
                needs_pg = any(
                    s.get('backend') == 'postgres'
                    and s.get('status') != 'failed'
                    for s in manifest.get('stores', []))
    except (OSError, tarfile.TarError, json.JSONDecodeError) as exc:
        raise click.ClickException(f'cannot read bundle: {exc}')
    if needs_pg and shutil.which('pg_restore') is None:
        raise click.ClickException(
            'bundle contains postgres stores but pg_restore is not on'
            ' PATH; install postgresql-client and retry')

    click.echo(
        f'About to restore {bundle} into {data_dir} (overwrites local'
        ' stores).', err=True)
    if not yes:
        click.confirm(
            'Proceed? This overwrites local stores.',
            default=False, abort=True)
    try:
        with held_drain_lock(data_dir):
            result = restore(bundle, data_dir)
    except (MigrateError, RuntimeError, EmbedFingerprintError) as exc:
        raise click.ClickException(str(exc))
    _json_out({'action': 'restored', **result})


@backup.command('worker', hidden=True)
@click.pass_context
def backup_worker(ctx: click.Context) -> None:
    """Hidden: run one backup now. The scheduler unit's ExecStart target."""
    from memman.backup import run_backup
    try:
        run_backup(ctx.obj['data_dir'])
    except RuntimeError as exc:
        raise click.ClickException(str(exc))


@claude_callable
@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show database statistics."""
    from memman.store.factory import list_stores, resolve_store_backend

    data_dir = ctx.obj['data_dir']
    store_name = _resolve_store_name(data_dir, ctx.obj['store'])
    with _active_backend(ctx) as backend:
        node_stats = backend.nodes.stats()
        declared = {
            key[len('MEMMAN_BACKEND_'):]
            for key in config.parse_env_file(config.env_file_path(data_dir))
            if key.startswith('MEMMAN_BACKEND_')
            }
        all_stores = set(list_stores(data_dir)) | declared
        backends_in_use = sorted({
            resolve_store_backend(s, data_dir) for s in all_stores
            })
        try:
            from memman.pipeline.remember import compute_prompt_version
            active_pv = compute_prompt_version()
            stale_insights: int | None = backend.nodes.count_stale_insights(
                active_pv)
        except Exception:
            stale_insights = None
        out = {
            'store': store_name,
            'backend': resolve_store_backend(store_name, data_dir),
            'backends_in_use': backends_in_use,
            'total_insights': node_stats.total_insights,
            'superseded_insights': node_stats.superseded_insights,
            'deleted_insights': node_stats.deleted_insights,
            'stale_insights': stale_insights,
            'edge_count': node_stats.edge_count,
            'oplog_count': node_stats.oplog_count,
            'by_category': node_stats.by_category,
            'top_entities': node_stats.top_entities,
            'storage_path': backend.path,
            }
        _json_out(out)


@claude_callable
@cli.command()
@click.option('--text', 'text_output', is_flag=True, default=False,
              help='Human-readable colored output (default: JSON)')
@click.pass_context
def doctor(ctx: click.Context, text_output: bool) -> None:
    """Run health checks on the database, scheduler, and providers.

    Exits 0 on pass/warn, 1 on fail — usable as a CI/scripted gate.
    """
    from memman.doctor import run_all_checks

    with _active_backend(ctx, unchecked=True) as backend:
        store_name = _resolve_store_name(
            ctx.obj['data_dir'], ctx.obj['store'])
        result = run_all_checks(
            backend, data_dir=ctx.obj['data_dir'],
            store_name=store_name)
        result['store'] = store_name
        result['db_path'] = backend.path
        if text_output:
            _doctor_text_report(result)
        else:
            _json_out(result)
    if result.get('status') == 'fail':
        ctx.exit(1)


def _doctor_text_report(result: dict) -> None:
    """Render a doctor result dict as colored PASS/WARN/FAIL lines.
    """
    colors = {'pass': 'green', 'warn': 'yellow',
              'fail': 'red', 'empty': 'cyan'}
    overall = result.get('status', 'unknown')
    click.secho(
        f'memman doctor: {overall.upper()}',
        fg=colors.get(overall, 'white'), bold=True)
    click.echo(f"store: {result.get('store', '?')}")
    click.echo(f"db:    {result.get('db_path', '?')}")
    click.echo(f"active insights: {result.get('total_active', 0)}")
    click.echo('')
    for check in result.get('checks', []):
        st = check.get('status', 'unknown')
        click.secho(
            f"  [{st.upper():>4}] {check.get('name', '?')}",
            fg=colors.get(st, 'white'))
        detail = check.get('detail') or {}
        if detail and st != 'pass':
            for key, value in detail.items():
                click.echo(f'         {key}: {value}')


@log.command('list')
@click.option('--limit', default=20, type=int, help='Max entries')
@click.option('--since', default='', help='Time window (e.g. 7d, 24h)')
@click.option('--stats', is_flag=True, default=False,
              help='Show summary statistics (grouped by operation)')
@click.option('--text', 'text_output', is_flag=True, default=False,
              help='Human-readable text table (default: JSON)')
@click.pass_context
def log_list(ctx: click.Context, limit: int, since: str,
             stats: bool, text_output: bool) -> None:
    """Show the operation audit log (default JSON; --text for human view)."""
    since_ts = ''
    if since:
        since_ts = _parse_since(since)

    with _active_backend(ctx) as backend:
        if stats:
            stats_data = backend.oplog.stats(since=since_ts)
            _json_out({
                'operation_counts': stats_data.operation_counts,
                'never_accessed': stats_data.never_accessed,
                'total_active': stats_data.total_active,
                })
            return

        entry_objs = backend.oplog.recent(limit=limit, since=since_ts)
        entries = [
            {
                'created_at': format_timestamp(e.created_at),
                'operation': e.operation,
                'insight_id': e.insight_id,
                'detail': e.detail,
                }
            for e in entry_objs
            ]

        if not text_output:
            _json_out({'entries': entries, 'meta': {'count': len(entries)}})
            return

        if not entries:
            click.echo('No operations recorded yet.')
            return

        headers = ['TIME', 'OP', 'INSIGHT', 'DETAIL']
        sep = ['----', '--', '-------', '------']
        rows = []
        for e in entries:
            detail = e['detail']
            if len(detail) > 60:
                detail = detail[:57] + '...'
            rows.append([
                e['created_at'],
                e['operation'],
                e['insight_id'] or '',
                detail,
                ])

        all_rows = [headers, sep] + rows
        widths = [0] * 4
        for row in all_rows:
            for i, col in enumerate(row):
                widths[i] = max(widths[i], len(col))

        for row in all_rows:
            line = '  '.join(
                col.ljust(widths[i]) for i, col in enumerate(row))
            click.echo(line.rstrip())


@log.command('worker')
@click.option('--errors', is_flag=True, default=False,
              help='Read enrich.err instead of enrich.log.')
@click.option('--stack', is_flag=True, default=False,
              help='Read the rotated worker log that preserves tracebacks.')
@click.option('--lines', type=int, default=50,
              help='Number of tail lines to print (default 50).')
@click.pass_context
def log_worker(ctx: click.Context, errors: bool, stack: bool,
               lines: int) -> None:
    """Print the tail of one worker log target.

    `enrich.log` and `enrich.err` are the enrichment worker's stdout
    and stderr. `memman.log` is the rotated DEBUG log holding the
    tracebacks a one-line CLI error cannot carry, and the two targets
    do not share a directory.

    \b
    Parameters
    ----------
    errors : bool
        Read `enrich.err` rather than `enrich.log`.
    stack : bool
        Read `memman.log` and its rotation backups. Rejected together
        with `--errors`.
    lines : int
        Tail length; a non-positive value prints everything read.

    \b
    Notes
    -----
    - `enrich.log` and `enrich.err` sit under `~/.memman/logs`
      whatever `--data-dir` says: the systemd unit pins those two
      redirects to `%h/.memman/logs`, and the launchd plist bakes the
      absolute home in at install time. Neither reads the data dir.
    - `memman.log` is the one target that follows `--data-dir`, since
      the rotating handler builds its path from it. Under a
      non-default data dir the targets live in two directories, and
      this command resolves each from its own source.
    - `--stack` reads the rotation backups as well as the live file,
      oldest first. Rotation caps the set at 20 MB over four files, and
      BOTH the enrichment worker and the backup worker write it, so a
      traceback reaches a backup file sooner than its size suggests.
    - Nothing rotates `enrich.err`, so a pointer naming a stack can
      outlive the stack itself once all four files have turned over.

    \b
    Examples
    --------
    memman log worker --errors
    memman log worker --stack --lines 200
    """  # noqa: D301, D410, D411
    if errors and stack:
        raise click.UsageError(
            '--errors and --stack name different files; pass one.')
    if stack:
        named = pathlib.Path(ctx.obj['data_dir']) / 'logs' / 'memman.log'
        # Oldest backup first, so one tail spans a rotation. The
        # traceback a CLI error pointed at is often already in
        # memman.log.1: two workers share this file and rotation keeps
        # only _WORKER_LOG_BACKUPS of it, so reading the live file
        # alone reports no traceback while the stack is still on disk.
        candidates = [
            named.with_name(f'{named.name}.{i}')
            for i in range(_WORKER_LOG_BACKUPS, 0, -1)]
        candidates.append(named)
    else:
        logs_dir = pathlib.Path.home() / '.memman' / 'logs'
        named = logs_dir / ('enrich.err' if errors else 'enrich.log')
        candidates = [named]
    present = [p for p in candidates if p.is_file()]
    if not present:
        click.echo(f'[memman] no log file yet at {named}', err=True)
        return
    content: list[str] = []
    for path in present:
        try:
            content.extend(path.read_text(errors='replace').splitlines())
        except OSError as exc:
            raise click.ClickException(
                f'failed to read {path}: {exc}') from exc
    tail = content[-lines:] if lines > 0 else content
    for line_str in tail:
        click.echo(line_str)


@claude_callable
@insights.command('review')
@click.option('--limit', default=20, type=int, help='Max flagged results')
@click.pass_context
def insights_review(ctx: click.Context, limit: int) -> None:
    """Scan stored insights for content quality issues.

    Flags transient phrasing and low-signal content, so an
    operator can decide whether to `memman forget` a row.
    """
    from memman.search.quality import check_content_quality

    with _active_backend(ctx) as backend:
        all_active = backend.nodes.get_all_active()
        flagged = []
        for ins in all_active:
            warnings = check_content_quality(ins.content)
            if warnings:
                flagged.append({'insight': ins, 'quality_warnings': warnings})
            if len(flagged) >= limit:
                break
        _json_out({
            'review_results': [{
                'id': f['insight'].id,
                'content': f['insight'].content,
                'importance': f['insight'].importance,
                'quality_warnings': f['quality_warnings'],
                } for f in flagged],
            'total_flagged': len(flagged),
            'actions': {'forget': 'memman forget <id>'},
            })


@claude_callable
@insights.command('show')
@click.argument('id')
@click.option('--history', is_flag=True,
              help='Walk the supersession chain through this id.')
@click.pass_context
def insights_show(ctx: click.Context, id: str, history: bool) -> None:
    """Read one insight by id, or walk its supersession chain.

    Without `--history`: the full insight, including a superseded
    row (its `superseded_by` names the successor). A forgotten row is
    refused. With `--history`: every row in the chain through this
    id, oldest first, forgotten rows included and marked.

    \b
    Parameters
    ----------
    id : str
        Any stored id. With `--history` a forgotten id is accepted so
        a chain whose oldest row was forgotten stays walkable.

    \b
    Returns
    -------
    JSON
        Without `--history`, the insight dict. With it,
        `{requested, chain}` where each chain entry is `{id,
        created_at, state, superseded_by, content}`; `state` is one of
        `current`, `superseded`, `forgotten`, and a forgotten entry
        carries no `content`.

    \b
    Notes
    -----
    - The walk follows `superseded_by` forward and every row pointing
      at a chain member backward, so a successor with two predecessors
      (a merge joined by a curated sibling) lists both.
    - Order is chain order, not timestamp order: rows written within
      one second still list predecessor first.

    \b
    Examples
    --------
    memman insights show 16c6c667-...
    memman insights show 16c6c667-... --history
    """  # noqa: D301, D410, D411
    with _active_backend(ctx) as backend:
        ins = backend.nodes.get_include_deleted(id)
        if ins is None:
            raise click.ClickException(f'insight {id} not found')
        if not history:
            if ins.deleted_at is not None:
                raise click.ClickException(f'insight {id} was forgotten')
            _json_out(insight_to_full_dict(ins))
            return
        rows: dict[str, Insight] = {ins.id: ins}
        frontier = [ins]
        while frontier:
            row = frontier.pop()
            found = list(backend.nodes.predecessors(row.id))
            if row.superseded_by and row.superseded_by not in rows:
                successor = backend.nodes.get_include_deleted(
                    row.superseded_by)
                if successor is not None:
                    found.append(successor)
            for other in found:
                if other.id not in rows:
                    rows[other.id] = other
                    frontier.append(other)

    def depth(row: Insight) -> int:
        steps, seen = 0, set()
        while (row.superseded_by in rows and row.id not in seen):
            seen.add(row.id)
            row = rows[row.superseded_by]
            steps += 1
        return steps

    chain = []
    for row in sorted(rows.values(),
                      key=lambda r: (-depth(r), r.created_at or '', r.id)):
        if row.deleted_at is not None:
            state = 'forgotten'
        elif row.superseded_by:
            state = 'superseded'
        else:
            state = 'current'
        entry: dict[str, Any] = {
            'id': row.id,
            'created_at': format_timestamp(row.created_at),
            'state': state,
            'superseded_by': row.superseded_by,
            }
        if state != 'forgotten':
            entry['content'] = row.content
        chain.append(entry)
    _json_out({'requested': id, 'chain': chain})


@claude_callable
@insights.command('by-queue')
@click.argument('queue_uuid')
@click.pass_context
def insights_by_queue(ctx: click.Context, queue_uuid: str) -> None:
    """List the insights one queued write produced.

    Resolves the `queue_uuid` that `remember` and `replace` return
    into the rows that write actually stored, once the scheduler has
    drained it. This is the write-to-read join: the queue row itself
    is purged about a minute after the drain, while the uuid is
    stamped on every insight the write produced and survives.

    \b
    Parameters
    ----------
    queue_uuid : str
        The `queue_uuid` from a `remember` / `replace` response, or
        from `memman scheduler queue show <row_id>`.

    \b
    Returns
    -------
    JSON
        `{queue_uuid, store, count, results}`. `store` is the store
        SEARCHED, not the store the write targeted. `results` holds
        one full insight dict per row, oldest first; a row a later
        write superseded is included with its `superseded_by` set,
        since it is still where THIS write landed.

    \b
    Notes
    -----
    - `count: 0` is a real answer, not an error, and has three
      causes: the write is still queued, it stored nothing (see
      `memman scheduler queue skipped`), or it went to a different
      store. The queue is process-global while this command reads
      one store, so a uuid from `remember --store shop` resolves to
      nothing under any other store.
    - After the drain, one write resolves to one row only under
      `--no-reconcile`, which bypasses extraction; otherwise it can
      split into several.
    - A malformed uuid is rejected rather than answered `count: 0`,
      so grabbing `queue_id` instead of `queue_uuid` fails loudly.

    \b
    Examples
    --------
    memman remember "a durable fact" --no-reconcile
    memman insights by-queue 7f3c1e00-0d1a-4f7e-9c2b-2a1d5b8e4c60
    """  # noqa: D301, D410, D411
    try:
        uuid.UUID(queue_uuid)
    except ValueError:
        raise click.ClickException(
            f'{queue_uuid!r} is not a queue uuid. Pass the `queue_uuid`'
            ' from a remember/replace response, not the `queue_id`.')
    name = _resolve_store_name(ctx.obj['data_dir'], ctx.obj['store'])
    with _active_backend(ctx) as backend:
        rows = backend.nodes.get_by_queue_uuid(queue_uuid)
    _json_out({
        'queue_uuid': queue_uuid,
        'store': name,
        'count': len(rows),
        'results': [insight_to_full_dict(r) for r in rows],
        })


@cli.command()
@click.option('--target', default='',
              help='Target environment (claude-code | openclaw | nanoclaw)')
@click.option('--backend', type=click.Choice(_BACKEND_CHOICES),
              default=None,
              help='Storage backend; bypasses the wizard prompt when set.')
@click.option('--pg-dsn', default=None,
              help='Postgres DSN (postgresql://...); required with'
                   ' --backend postgres in non-interactive mode.')
@click.option('--llm-endpoint', type=str, default=None,
              help='LLM endpoint URL; bypasses the wizard prompt when set.')
@click.option('--embed-provider',
              type=click.Choice(list(_EMBED_PROVIDER_CHOICES)),
              default=None,
              help='Embed provider; bypasses the wizard prompt when set.')
@click.option('--no-wizard', is_flag=True,
              help='Disable interactive prompts; flags + defaults only.')
@click.pass_context
def install(ctx: click.Context, target: str, backend: str | None,
            pg_dsn: str | None, llm_endpoint: str | None,
            embed_provider: str | None, no_wizard: bool) -> None:
    """Install memman integration: skill, hooks, scheduler."""
    from memman.setup.claude import run_install
    run_install(
        ctx.obj['data_dir'],
        target=target,
        backend=backend,
        pg_dsn=pg_dsn,
        llm_endpoint=llm_endpoint,
        embed_provider=embed_provider,
        no_wizard=no_wizard)


@cli.command()
@click.option('--target', default='',
              help='Target environment (claude-code | openclaw | nanoclaw)')
@click.pass_context
def uninstall(ctx: click.Context, target: str) -> None:
    """Remove memman integration (reverse of `memman install`)."""
    from memman.setup.claude import run_uninstall
    run_uninstall(ctx.obj['data_dir'], target=target)


@cli.command()
@click.option('--store', default='',
              help='Store to migrate. Required unless --all.')
@click.option('--all', 'migrate_all', is_flag=True,
              help='Migrate every store under the data dir.')
@click.option('--to', 'target_backend',
              type=click.Choice(_BACKEND_CHOICES),
              default='postgres',
              help='Target backend for the migration. Default: postgres.')
@click.option('--dry-run', is_flag=True,
              help='Report the plan without writing or prompting.')
@click.option('--yes', is_flag=True, default=False,
              help='Skip the confirmation prompt (for scripted use).')
@click.pass_context
def migrate(
        ctx: click.Context, store: str, migrate_all: bool,
        target_backend: str, dry_run: bool, yes: bool) -> None:
    """Migrate memman stores between SQLite and Postgres backends.

    Migration is symmetric. `--to postgres` (default) copies SQLite
    stores into a Postgres schema, archives the SQLite source, and
    flips MEMMAN_BACKEND_<store>=postgres. `--to sqlite` dumps the
    Postgres schema to archive/, copies rows into a fresh SQLite
    store, drops the postgres schema, and flips
    MEMMAN_BACKEND_<store>=sqlite. Stores already on the target
    backend emit a warning and are skipped (idempotent). The shared
    drain.lock is held throughout so a scheduler-fired drain cannot
    race the migration.
    """
    import shutil

    from memman import config
    from memman.migrate import MigrateError, SchemaState
    from memman.migrate import _verify_destination_counts, held_drain_lock
    from memman.migrate import inspect_target_schemas, preflight
    from memman.setup.scheduler import _write_env_keys
    from memman.store.db import list_local_store_dirs, store_dir
    from memman.store.errors import ConfigError
    from memman.store.factory import list_stores, resolve_store_backend
    from memman.store.factory import resolve_store_pg_dsn
    from memman.store.postgres import PostgresMigrator, _connection
    from memman.store.postgres import _store_schema, drop_postgres_store
    from memman.store.sqlite import SqliteMigrator
    from memman.trace import redact_dsn

    data_dir = ctx.obj['data_dir']

    if shutil.which('pg_dump') is None:
        raise click.ClickException(
            'pg_dump not found on PATH. memman migrate requires'
            ' pg_dump regardless of direction so the postgres source'
            ' can be archived before any destructive step.'
            ' Install postgresql-client:'
            '\n  apt: sudo apt install postgresql-client'
            '\n  brew: brew install libpq && brew link --force libpq')

    if not migrate_all and not store:
        raise click.UsageError('pass --store NAME or --all')
    if migrate_all and store:
        raise click.UsageError(
            'pass either --store NAME or --all, not both')
    if dry_run and target_backend == 'sqlite':
        raise click.UsageError(
            '--dry-run is not supported with --to sqlite')

    if migrate_all:
        if target_backend == 'sqlite':
            stores_all = list_stores(data_dir)
        else:
            stores_all = list_local_store_dirs(data_dir)
    else:
        # Notes:
        # - Check existence before the naming guard below. Without
        #   it an unknown name falls through to that guard, which
        #   then describes a store that is not there.
        # - Only a sqlite-routed store is judged here, from the
        #   filesystem. Deciding a postgres-routed one needs the
        #   server, and during an outage that reports a live store as
        #   missing; those fall through and fail on their own error.
        if (resolve_store_backend(store, data_dir) == 'sqlite'
                and not store_exists(data_dir, store)):
            raise click.ClickException(
                f'store {store!r} does not exist'
                f' (list them with `memman store list`)')
        stores_all = [store]
    if not stores_all:
        click.echo('no stores to migrate', err=True)
        return

    todo: list[str] = []
    skipped: list[str] = []
    for s in stores_all:
        current = resolve_store_backend(s, data_dir)
        if current == target_backend:
            click.echo(
                f'Store {s!r} is already on {target_backend} backend.'
                f' Nothing to migrate.')
            skipped.append(s)
            continue
        todo.append(s)

    if not todo:
        if migrate_all:
            click.echo(f'migrated=0 skipped={len(skipped)}')
        return

    # Notes:
    # - Both directions touch a postgres schema, so both need the
    #   name to be one. Guarding only `--to postgres` leaves the
    #   same ConfigError escaping `except MigrateError` on the way
    #   back, after the plan prints and the drain lock is held.
    # - Store names reach here unchecked: `list_local_store_dirs`
    #   scans the filesystem, and a postgres route can be hand
    #   written into the env file.
    # - `_store_schema` is the oracle rather than a second regex, so
    #   this gate agrees with the call that raised.
    # - Runs before the DSN lookup so a naming fault never depends
    #   on a reachable server.
    unhostable: list[tuple[str, str]] = []
    for s in list(todo):
        try:
            _store_schema(s)
        except ConfigError as exc:
            unhostable.append((s, str(exc)))
            todo.remove(s)

    # Notes:
    # - `_store_schema` refuses on character class AND on length, so
    #   the remedy quotes its message rather than asserting a reason.
    #   Hardcoding the character-class text told the owner of a
    #   60-character name that it failed a pattern it matches.
    # - The rewrite is not injective and does not shorten, so it can
    #   return the name just refused, or one already claimed by
    #   another store in this run. Say so instead of naming it.
    # - `known` comes from the filesystem and this run, never from
    #   `list_stores`: that reaches the server, and the whole point
    #   of this gate is to work without one.
    known = set(list_local_store_dirs(data_dir)) | set(stores_all)
    taken: set[str] = set()

    def _remedy(name: str) -> str:
        suggestion = portable_store_name(name)
        if suggestion == name:
            return 'rename it to a shorter plain-identifier name'
        if suggestion in known | taken:
            return (f'the portable form {suggestion!r} is already'
                    f' taken, so pick another name')
        taken.add(suggestion)
        return f'create {suggestion!r} and migrate that'

    if unhostable and not migrate_all:
        bad, reason = unhostable[0]
        raise click.ClickException(
            f'store {bad!r} cannot be migrated: {reason}.'
            f' A sqlite-backed store of that name stays fully'
            f' usable. To move it, {_remedy(bad)}.')
    for s, reason in unhostable:
        click.echo(
            f'Skipping {s!r}: {reason}. To move it, {_remedy(s)}.',
            err=True)
        skipped.append(s)
    if not todo:
        verb = 'planned' if dry_run else 'migrated'
        click.echo(f'{verb}=0 skipped={len(skipped)}')
        return

    if target_backend == 'postgres':
        if migrate_all:
            dsn = config.get(config.DEFAULT_PG_DSN)
            if not dsn:
                raise click.UsageError(
                    'MEMMAN_DEFAULT_POSTGRES_DSN is not set; --all requires a'
                    ' default DSN. Run `memman config set-pg-dsn'
                    ' --default`, or migrate one store at a time with'
                    ' --store NAME.')
        else:
            dsn = (config.get(config.env_key_for('postgres', 'DSN', todo[0]))
                   or config.get(config.DEFAULT_PG_DSN))
            if not dsn:
                raise click.UsageError(
                    f'no DSN for store {todo[0]!r}: set'
                    f' {config.env_key_for("postgres", "DSN", todo[0])} or'
                    f' {config.DEFAULT_PG_DSN} (run `memman config'
                    f' set-pg-dsn --store {todo[0]}` or `--default`).')

        try:
            preflight(dsn)
        except MigrateError as exc:
            raise click.ClickException(str(exc))

        try:
            states = inspect_target_schemas(dsn, todo)
        except MigrateError as exc:
            raise click.ClickException(str(exc))

        populated = [s for s in todo
                     if states[s] == SchemaState.POPULATED]

        click.echo('Migration plan:')
        click.echo(f'  Source:      {data_dir}/data/')
        click.echo(f'  Destination: {redact_dsn(dsn)}')
        click.echo(f'  Stores ({len(todo)}):')
        width = max((len(s) for s in todo), default=0)
        for s in todo:
            st = states[s]
            if st == SchemaState.ABSENT:
                note = 'will create'
            elif st == SchemaState.EMPTY:
                note = 'EMPTY, will recreate'
            else:
                note = 'POPULATED, will DROP CASCADE and recreate'
            click.echo(
                f'    {s.ljust(width)} -> store_{s}    [{note}]')
        if populated:
            click.echo('')
            click.echo(
                f'WARNING: {len(populated)} store(s) will be'
                f' destructively overwritten.')
        if not dry_run:
            click.echo('')
            click.echo(
                'After successful migrate,'
                ' MEMMAN_BACKEND_<store>=postgres'
                ' and MEMMAN_POSTGRES_DSN_<store>=<dsn> will be written to'
                ' the env file for each migrated store.')

        if dry_run:
            src_migrator = SqliteMigrator(data_dir)
            for s in todo:
                try:
                    src_migrator.preflight_source(s)
                    payload = src_migrator.gather(s)
                    click.echo(
                        f'{s}: insights={len(payload.insights)}'
                        f' edges={len(payload.edges)}'
                        f' oplog={len(payload.oplog)}'
                        f' meta={len(payload.meta)} (dry-run)')
                # Notes:
                # - All three types are reachable. `apply` and
                #   `_verify_destination_counts` reach the Postgres
                #   connection scope and raise `BackendError`, while
                #   `SqliteMigrator.gather` runs its selects unwrapped
                #   -- `_connect_ro` translates only the connect and
                #   the `pragma schema_version` probe -- so a store
                #   that opens and then fails mid-read raises a bare
                #   `sqlite3.Error`. Miss either and the store name is
                #   lost, and `--all` cannot say which store failed.
                except (MigrateError, BackendError, sqlite3.Error) as exc:
                    raise click.ClickException(f'{s}: {exc}')
            # Without this a run that skipped stores looked like a
            # clean full plan, while the all-skipped branch printed a
            # count.
            if migrate_all and skipped:
                click.echo(
                    f'planned={len(todo)} skipped={len(skipped)}')
            return

        if not yes:
            click.echo('')
            click.confirm('Proceed?', default=False, abort=True)

        try:
            with held_drain_lock(data_dir):
                src_migrator = SqliteMigrator(data_dir)
                tgt_migrator = PostgresMigrator(data_dir, dsn=dsn)
                tgt_migrator.preflight_target(todo[0])
                for s in todo:
                    try:
                        src_migrator.preflight_source(s)
                        if states[s] in {
                                SchemaState.EMPTY,
                                SchemaState.POPULATED}:
                            drop_postgres_store(s, dsn)
                        payload = src_migrator.gather(s)
                        tgt_migrator.apply(s, payload)
                        with _connection(dsn, autocommit=True) as conn:
                            _verify_destination_counts(
                                conn, _store_schema(s), s,
                                expected={
                                    'insights': len(payload.insights),
                                    'edges': len(payload.edges),
                                    'oplog': len(payload.oplog),
                                    'meta': len(payload.meta),
                                    })
                        click.echo(
                            f'{s}: insights={len(payload.insights)}'
                            f' edges={len(payload.edges)}'
                            f' oplog={len(payload.oplog)}'
                            f' meta={len(payload.meta)} (verified)')
                        _write_env_keys({
                            config.BACKEND_FOR(s): 'postgres',
                            config.env_key_for('postgres', 'DSN', s): dsn,
                            }, data_dir=data_dir)
                        click.echo(
                            f'  Wrote {config.BACKEND_FOR(s)}=postgres'
                            f' to {data_dir}/env.')
                        artifact = src_migrator.archive(s, data_dir)
                        if (artifact.kind == 'filesystem'
                                and artifact.location):
                            click.echo(
                                f'  Archived source to'
                                f' {artifact.location}.')
                    except (MigrateError, BackendError,
                            sqlite3.Error) as exc:
                        raise click.ClickException(f'{s}: {exc}')
                    except OSError as exc:
                        click.echo(
                            f'  WARNING: could not archive source'
                            f' for {s!r}: {exc}; leaving in place.'
                            ' Run `memman doctor` to track.',
                            err=True)
        except MigrateError as exc:
            raise click.ClickException(str(exc))

        click.echo('')
        click.echo(
            f'Migration complete: {len(todo)} store(s)'
            f' copied to Postgres.')
        click.echo(
            f'Sources archived to'
            f' {data_dir}/archive/<store>/<YYYYMMDD>_<NN>/.'
            f' Remove with `rm -rf` when no longer needed.')
        if migrate_all and skipped:
            click.echo(
                f'migrated={len(todo)} skipped={len(skipped)}')
        click.echo('')
        click.echo('Recommended next step:')
        click.echo(
            '  memman doctor    # verify the postgres backend health')
        return

    store_dsns: dict[str, str] = {}
    for s in todo:
        dsn = resolve_store_pg_dsn(s, data_dir)
        if not dsn:
            raise click.UsageError(
                f'no postgres DSN for store {s!r}: set'
                f' {config.env_key_for("postgres", "DSN", s)} or'
                f' {config.DEFAULT_PG_DSN}.')
        store_dsns[s] = dsn

    target_paths: dict[str, pathlib.Path] = {
        s: pathlib.Path(store_dir(data_dir, s)) for s in todo
        }
    for s in todo:
        if target_paths[s].exists():
            raise click.ClickException(
                f'target directory {target_paths[s]} already exists;'
                f' move it aside (`mv {target_paths[s]}'
                f' {target_paths[s]}.bak`) and re-run.')

    click.echo('Migration plan (postgres -> sqlite):')
    click.echo(f'  Target: {data_dir}/data/')
    click.echo(f'  Stores ({len(todo)}):')
    width = max((len(s) for s in todo), default=0)
    for s in todo:
        click.echo(
            f'    {s.ljust(width)} <- {redact_dsn(store_dsns[s])}'
            f' (schema store_{s})')
    click.echo('')
    click.echo(
        'After successful migrate, MEMMAN_BACKEND_<store>=sqlite will'
        ' be written and MEMMAN_POSTGRES_DSN_<store> removed for each'
        ' migrated store. Postgres schemas will be archived to'
        f' {data_dir}/archive/<store>/<YYYYMMDD>_<NN>/dump.pgdump'
        ' and dropped.')

    if not yes:
        click.echo('')
        click.confirm('Proceed?', default=False, abort=True)

    import tempfile

    from memman.setup.archive import archive_postgres_schema

    try:
        with held_drain_lock(data_dir):
            for s in todo:
                dsn = store_dsns[s]
                target = target_paths[s]
                scratch = pathlib.Path(tempfile.mkdtemp(
                    dir=data_dir, prefix='migrate-'))
                (scratch / 'data').mkdir()
                produced = scratch / 'data' / s
                try:
                    src_migrator = PostgresMigrator(data_dir, dsn=dsn)
                    src_migrator.preflight_source(s)
                    payload = src_migrator.gather(s)
                    tgt_migrator = SqliteMigrator(str(scratch))
                    tgt_migrator.preflight_target(s)
                    tgt_migrator.apply(s, payload)
                    click.echo(
                        f'{s}: insights={len(payload.insights)}'
                        f' edges={len(payload.edges)}'
                        f' oplog={len(payload.oplog)}'
                        f' meta={len(payload.meta)} (verified)')
                # Notes:
                # - BackendError joins MigrateError here so a backend
                #   rejecting the store still removes the scratch
                #   dir; escaping this handler stranded a
                #   `migrate-*` directory in the data dir. It
                #   subsumes the ConfigError this once named, and
                #   also covers the bare BackendError that
                #   `gather` / `apply` / `_verify_destination_counts`
                #   raise from the Postgres connection scope.
                except (MigrateError, BackendError,
                        sqlite3.Error) as exc:
                    shutil.rmtree(scratch, ignore_errors=True)
                    raise click.ClickException(f'{s}: {exc}')

                shutil.move(str(produced), str(target))
                shutil.rmtree(scratch, ignore_errors=True)
                click.echo(f'  Wrote sqlite store at {target}.')

                try:
                    archive_dest = archive_postgres_schema(
                        data_dir, s, dsn)
                    click.echo(
                        f'  Archived postgres schema to'
                        f' {archive_dest}.')
                except Exception as exc:
                    raise click.ClickException(
                        f'{s}: archive_postgres_schema failed: {exc}')

                _write_env_keys(
                    {config.BACKEND_FOR(s): 'sqlite'},
                    removes={config.env_key_for('postgres', 'DSN', s)},
                    data_dir=data_dir)
                click.echo(
                    f'  Wrote {config.BACKEND_FOR(s)}=sqlite to'
                    f' {data_dir}/env (removed'
                    f' {config.env_key_for("postgres", "DSN", s)}).')

                try:
                    drop_postgres_store(s, dsn)
                    click.echo(f'  Dropped postgres schema store_{s}.')
                except Exception as exc:
                    click.echo(
                        f'  WARNING: failed to drop postgres schema'
                        f' for {s!r}: {exc}; remove manually with'
                        f' `psql -c "drop schema store_{s} cascade"`.',
                        err=True)
    except MigrateError as exc:
        raise click.ClickException(str(exc))

    click.echo('')
    click.echo(
        f'Migration complete: {len(todo)} store(s)'
        f' migrated to SQLite.')
    click.echo(
        f'Postgres schemas archived to'
        f' {data_dir}/archive/<store>/<YYYYMMDD>_<NN>/dump.pgdump.'
        f' Replay with `pg_restore -d <dsn> <archive>` if needed.')
    if migrate_all and skipped:
        click.echo(f'migrated={len(todo)} skipped={len(skipped)}')
    click.echo('')
    click.echo('Recommended next step:')
    click.echo('  memman doctor    # verify the sqlite backend health')


def _emit_guide(session_id: str = '') -> None:
    """Write shipped guide.md to stdout.

    Parameters
    ----------
    session_id : str, default ''
        Substituted for the guide's literal `$SESSION_ID`; empty
        leaves the placeholder in place.

    Notes
    -----
    - Only `memman prime` passes an id. `memman guide`, the openclaw
      bootstrap entry, does not, so that host reads the placeholder
      verbatim.
    """
    from importlib.resources import files as pkg_files
    shipped = (pkg_files('memman.setup.assets')
               .joinpath('claude/guide.md').read_text())
    if session_id:
        shipped = shipped.replace('$SESSION_ID', session_id)
    click.echo(shipped, nl=False)


@cli.command(hidden=True)
def guide() -> None:
    """Print the memman behavioral guide. Hidden — called by openclaw bootstrap."""
    _emit_guide()


@cli.command(hidden=True)
def prime() -> None:
    """Hook shim: emit status + optional compact hint + guide. Invoked by
    the SessionStart hook (claude/prime.sh). Not meant for direct use.
    """
    input_raw = '{}'
    if not sys.stdin.isatty():
        try:
            input_raw = sys.stdin.read()
        except OSError:
            input_raw = '{}'
    try:
        session = json.loads(input_raw) if input_raw.strip() else {}
    except json.JSONDecodeError:
        session = {}

    source = session.get('source', '')
    session_id = session.get('session_id', '')

    status_line = '[memman] Memory active.'
    try:
        data_dir = os.environ.get(config.DATA_DIR, default_data_dir())
        env_store = os.environ.get(config.STORE, '').strip()
        name = env_store or read_active(data_dir)
        from memman.store.factory import resolve_store_backend
        backend_name = resolve_store_backend(name, data_dir)
        if backend_name == 'sqlite':
            from memman.store.node import get_stats
            if store_exists(data_dir, name):
                with open_ro_db(store_dir(data_dir, name)) as db:
                    stats = get_stats(db)
                status_line = (f"[memman] Memory active "
                               f"({stats['total_insights']} insights, "
                               f"{stats['edge_count']} edges).")
        else:
            from memman.session import active_store
            with active_store(
                    data_dir=data_dir, store=name,
                    unchecked=True) as backend:
                s = backend.nodes.stats()
                status_line = (f'[memman] Memory active '
                               f'({s.total_insights} insights, '
                               f'{s.edge_count} edges).')
    except Exception as exc:
        logger.debug('prime status fallback: %s', exc)
    click.echo(status_line)

    if source == 'compact':
        flag = (pathlib.Path.home() / '.memman' / 'compact'
                / f'{session_id}.json')
        trigger = 'auto'
        if flag.is_file():
            try:
                flag_data = json.loads(flag.read_text())
                trigger = flag_data.get('trigger', 'auto') or 'auto'
            except (json.JSONDecodeError, OSError):
                pass
        click.echo(f'[memman] Context was just compacted ({trigger}). '
                   f'Recall critical context now: '
                   f'memman recall "<topic>" --limit 5')

    _emit_guide(session_id)


def _graph_rebuild_stale_only(
        ctx: click.Context, *, dry_run: bool,
        progress_jsonl: bool) -> None:
    """Stale-only branch of `graph rebuild`.

    Filters work to rows whose persisted `prompt_version` no longer
    matches `compute_prompt_version()` -- the enrichment and causal
    prompts plus the `slow_metadata` model, which is exactly the set
    this command replays. Works on SQLite and Postgres
    (the wholesale rebuild's SQLite-only guard does not apply here:
    the per-row writes through `link_pending` are the same traffic
    the `remember` hot path already exercises). Lock + predicate +
    reset run inside a single `reembed_lock('rebuild')` window so
    a concurrent wholesale rebuild cannot race.
    """
    from memman.embed.fingerprint import bound_embedder
    from memman.graph.engine import MAX_LINK_BATCH, link_pending
    from memman.pipeline.remember import compute_prompt_version

    if not dry_run:
        _require_started('rebuild')

    try:
        active_pv = compute_prompt_version()
    except Exception as exc:
        raise click.ClickException(
            f'cannot resolve active prompt version: {exc}')
    # Reported, never compared: `active_pv` already folds this model
    # in, so the payload names it only so a reader can tell WHICH
    # input drifted without un-folding the hash.
    try:
        enrich_model: str | None = config.require(
            config.LLM_MODEL_SLOW_METADATA)
    except Exception:
        enrich_model = None

    data_dir = ctx.obj['data_dir']
    store_name = _resolve_store_name(data_dir, ctx.obj['store'])

    with _active_backend(ctx) as backend:
        if dry_run:
            stale = backend.nodes.count_stale_insights(active_pv)
            _json_out({
                'mode': 'stale-only', 'total': stale, 'dry_run': 1,
                'active_pv': active_pv,
                'active_enrich_model': enrich_model,
                })
            return

        with backend.reembed_lock('rebuild') as held:
            if not held:
                raise click.ClickException(
                    'another graph rebuild is in progress on this store')

            stale_ids = backend.nodes.iter_stale_insight_ids(active_pv)
            total_count = len(stale_ids)

            if total_count == 0:
                stats = {
                    'processed': 0, 'remaining': 0,
                    'mode': 'stale-only',
                    'skipped': 'no_stale_rows',
                    'active_pv': active_pv,
                    'active_enrich_model': enrich_model,
                    }
                _json_out(stats)
                return

            llm_client = _get_llm_client_or_fail('slow_canonical')
            metadata_llm_client = _get_llm_client_or_fail('slow_metadata')
            ec = bound_embedder(backend)

            embed_cache = dict(backend.nodes.iter_embeddings_as_vecs())
            processed = 0

            bar = tqdm(
                total=total_count, desc='Rebuilding (stale)',
                unit='insight', file=sys.stderr,
                dynamic_ncols=True,
                disable=not sys.stderr.isatty())

            done_count = 0

            def _on_progress(stage: str, insight: Insight) -> None:
                nonlocal done_count
                preview = insight.content[:40].replace('\n', ' ')
                bar.set_description(f'{stage}: {preview}')
                if stage == 'done':
                    bar.update(1)
                    done_count += 1
                    if progress_jsonl:
                        sys.stderr.write(json.dumps({
                            'event': 'progress',
                            'stage': 'done',
                            'n': done_count,
                            'total': total_count,
                            }) + '\n')
                        sys.stderr.flush()

            for i in range(0, total_count, MAX_LINK_BATCH):
                batch_ids = stale_ids[i:i + MAX_LINK_BATCH]
                backend.nodes.reset_for_rebuild(batch_ids)

                while True:
                    count = link_pending(
                        backend, embed_cache=embed_cache,
                        llm_client=llm_client,
                        metadata_llm_client=metadata_llm_client,
                        embed_client=ec,
                        on_progress=_on_progress,
                        store_name=store_name)
                    processed += count
                    if count == 0:
                        break

            bar.set_description('Done')
            bar.close()

            remaining = backend.nodes.count_pending_links()

            stats = {
                'processed': processed, 'remaining': remaining,
                'mode': 'stale-only',
                'active_pv': active_pv,
                'active_enrich_model': enrich_model,
                }
            backend.oplog.log(
                operation='rebuild', insight_id='',
                detail=json.dumps(stats))
            _json_out(stats)


@graph.command('rebuild')
@click.option('--dry-run', is_flag=True, default=False,
              help='Show counts without modifying DB')
@click.option('--progress-jsonl', is_flag=True, default=False,
              help='Emit one JSON line per done event to stderr'
                   ' (for parents that capture stderr and need streaming'
                   ' progress while the inner tqdm bar is suppressed).')
@click.option('--stale-only', is_flag=True, default=False,
              help='Re-enrich only rows whose prompt_version no longer'
                   ' matches the active config -- the enrichment and'
                   ' causal prompts plus the slow_metadata model, which'
                   ' is exactly what this command replays. Cross-backend'
                   ' (works on Postgres). NULL provenance rows are not'
                   ' swept; they need a separate backfill.')
@click.pass_context
def graph_rebuild(ctx: click.Context, dry_run: bool,
                  progress_jsonl: bool, stale_only: bool) -> None:
    """Re-enrich all insights through the full LLM pipeline."""
    if stale_only:
        _graph_rebuild_stale_only(
            ctx, dry_run=dry_run, progress_jsonl=progress_jsonl)
        return

    if not dry_run:
        _require_started('rebuild')
    from memman.embed.fingerprint import bound_embedder
    from memman.graph.engine import MAX_LINK_BATCH, link_pending

    data_dir = ctx.obj['data_dir']
    store_name = _resolve_store_name(data_dir, ctx.obj['store'])

    with _active_backend(ctx) as backend:
        llm_client = _get_llm_client_or_fail('slow_canonical')
        metadata_llm_client = _get_llm_client_or_fail('slow_metadata')
        ec = bound_embedder(backend)

        all_ids = backend.nodes.get_active_ids()
        total_count = len(all_ids)

        if dry_run:
            _json_out({'total': total_count, 'dry_run': 1})
            return

        if total_count == 0:
            _json_out({'processed': 0, 'remaining': 0})
            return

        with backend.reembed_lock('rebuild') as held:
            if not held:
                raise click.ClickException(
                    'another graph rebuild is in progress on this store')

            embed_cache = dict(backend.nodes.iter_embeddings_as_vecs())
            processed = 0

            bar = tqdm(
                total=total_count, desc='Rebuilding',
                unit='insight', file=sys.stderr,
                dynamic_ncols=True,
                disable=not sys.stderr.isatty())

            done_count = 0

            def _on_progress(stage: str, insight: Insight) -> None:
                nonlocal done_count
                preview = insight.content[:40].replace('\n', ' ')
                bar.set_description(f'{stage}: {preview}')
                if stage == 'done':
                    bar.update(1)
                    done_count += 1
                    if progress_jsonl:
                        sys.stderr.write(json.dumps({
                            'event': 'progress',
                            'stage': 'done',
                            'n': done_count,
                            'total': total_count,
                            }) + '\n')
                        sys.stderr.flush()

            for i in range(0, total_count, MAX_LINK_BATCH):
                batch_ids = all_ids[i:i + MAX_LINK_BATCH]
                backend.nodes.reset_for_rebuild(batch_ids)

                while True:
                    count = link_pending(
                        backend, embed_cache=embed_cache,
                        llm_client=llm_client,
                        metadata_llm_client=metadata_llm_client,
                        embed_client=ec,
                        on_progress=_on_progress,
                        store_name=store_name)
                    processed += count
                    if count == 0:
                        break

            bar.set_description('Done')
            bar.close()

            remaining = backend.nodes.count_pending_links()

            stats = {'processed': processed, 'remaining': remaining}
            backend.oplog.log(
                operation='rebuild', insight_id='',
                detail=json.dumps(stats))
            _json_out(stats)


@embed_grp.command('status')
@click.pass_context
def embed_status(ctx: click.Context) -> None:
    """Show the store's stored fingerprint, swap state, and credential
    availability for that fingerprint's provider.

    Under per-store embedder sovereignty, the store's stored
    fingerprint is the source of truth -- there is no env-active
    fingerprint to compare against.
    """
    from memman.embed import registry as _ec_registry
    from memman.embed.fingerprint import stored_fingerprint
    from memman.embed.swap import read_progress

    with _active_backend(ctx, unchecked=True) as backend:
        stored = stored_fingerprint(backend)
        progress = read_progress(backend)

    out: dict = {
        'stored': None if stored is None else {
            'provider': stored.provider,
            'model': stored.model,
            'dim': stored.dim,
            },
        }
    if stored is not None:
        ec = _ec_registry.get_for(stored.provider, stored.model)
        out['credentials_available'] = ec.available()
        if not ec.available():
            out['hint'] = ec.unavailable_message()
    else:
        out['hint'] = (
            "DB not initialized. Run 'memman embed reembed'.")
    if progress.state:
        out['swap'] = {
            'state': progress.state,
            'cursor': progress.cursor,
            'target_provider': progress.target_provider,
            'target_model': progress.target_model,
            'target_dim': progress.target_dim,
            }
    _json_out(out)


_REEMBED_BATCH = 50


def _count_active_rows(sdir: str) -> int:
    """Return the count of non-deleted insights in the given store.
    """
    from memman.store.node import count_active_insights

    with open_ro_db(sdir) as db:
        return count_active_insights(db)


def _reembed_one_store(
        sdir: str, ec: 'EmbeddingProvider', target: 'Fingerprint',
        dry_run: bool, bar: 'tqdm | None' = None) -> dict:
    """Re-embed a single store with the active client.

    Walk all active insights, comparing each to `target`. Skip rows
    that already match; re-embed rows that differ. Per-row blob +
    cursor advance is one transaction; the final fingerprint write +
    cursor reset + state=idle + edge reindex is another.
    """
    from memman.embed.fingerprint import write_fingerprint
    from memman.graph.engine import reindex_auto_edges
    from memman.store.node import iter_for_reembed
    from memman.store.sqlite import SqliteBackend

    store_name = pathlib.Path(sdir).name
    with open_db(sdir) as db:
        backend = SqliteBackend(db)
        with backend.reembed_lock('reembed') as held:
            if not held:
                raise click.ClickException(
                    f'another reembed is in progress on {store_name}')

            cur_state = backend.meta.get('embed_reembed_state')
            cursor = backend.meta.get('embed_reembed_cursor') or ''

            scanned = 0
            reembedded = 0

            if not dry_run and cur_state != 'in_progress':
                with backend.transaction():
                    backend.meta.set('embed_reembed_state', 'in_progress')
                    backend.meta.set('embed_reembed_cursor', '')
                cursor = ''

            if bar is not None:
                bar.set_description(f'reembed {store_name}')

            while True:
                rows = iter_for_reembed(db, cursor, _REEMBED_BATCH)
                if not rows:
                    break

                for row_id, content, row_model, blob_len in rows:
                    scanned += 1
                    row_dim = (blob_len // 8) if blob_len else 0
                    matches = (
                        row_model == target.model
                        and row_dim == target.dim
                        and blob_len)
                    if not matches and not dry_run:
                        new_vec = ec.embed(content)
                        with backend.transaction():
                            backend.nodes.update_embedding(
                                row_id, new_vec, target.model)
                            backend.meta.set(
                                'embed_reembed_cursor', row_id)
                        reembedded += 1
                    else:
                        if not dry_run:
                            backend.meta.set(
                                'embed_reembed_cursor', row_id)
                    cursor = row_id
                    if bar is not None:
                        bar.update(1)

            if dry_run:
                return {
                    'store': store_name,
                    'scanned': scanned,
                    'would_reembed': reembedded,
                    }

            with backend.transaction():
                write_fingerprint(backend, target)
                backend.meta.set('embed_reembed_cursor', '')
                backend.meta.set('embed_reembed_state', 'idle')

            edge_stats = reindex_auto_edges(backend, store_name=store_name)

            stats = {
                'store': store_name,
                'scanned': scanned,
                'reembedded': reembedded,
                'edges': edge_stats,
                }
            backend.oplog.log(
                operation='embed_reembed', insight_id='',
                detail=json.dumps(stats))
            return stats


@embed_grp.command('reembed')
@click.option(
    '--dry-run', is_flag=True, default=False,
    help='Count rows that would be re-embedded; no DB writes.')
@click.pass_context
def embed_reembed(ctx: click.Context, dry_run: bool) -> None:
    """Sweep every store with the active client; write fingerprints.

    Always global: iterates all stores under the configured
    data_dir. The active embed provider is set by a single global
    env var, so a swap necessarily applies to every store; per-store
    scoping is intentionally not supported.

    Three cases through one walk per store:
    1. Empty DB - zero rows; only the fingerprint is written.
    2. Existing DB on the same provider - rows match; skip re-embed.
    3. Provider swap - rows mismatch; re-embed each.

    The sweep is resumable per store: progress is tracked in each
    store's `meta.embed_reembed_state` and `meta.embed_reembed_cursor`.
    A crash mid-sweep leaves state='in_progress'; re-running picks
    up from the cursor.
    """
    from memman.embed import get_client
    from memman.embed.fingerprint import Fingerprint
    from memman.store.db import list_local_store_dirs, store_dir
    from memman.store.factory import resolve_store_backend

    data_dir = ctx.obj['data_dir']
    active_name = _resolve_store_name(data_dir, ctx.obj['store'])
    if resolve_store_backend(active_name, data_dir) != 'sqlite':
        raise click.ClickException(
            'embed reembed is SQLite-only; Postgres reembed requires'
            ' a separate workflow (track in a follow-up issue)')

    if not dry_run:
        _require_stopped('reembed')

    ec = get_client()
    if not ec.available():
        raise click.ClickException(ec.unavailable_message())

    target = Fingerprint.from_client(ec)
    names = [
        n for n in list_local_store_dirs(data_dir)
        if resolve_store_backend(n, data_dir) == 'sqlite']

    grand_total = sum(
        _count_active_rows(store_dir(data_dir, name)) for name in names)
    bar = tqdm(
        total=grand_total, desc='reembed', unit='row',
        file=sys.stderr, dynamic_ncols=True,
        disable=not sys.stderr.isatty())

    per_store = []
    total_scanned = 0
    total_reembedded = 0
    try:
        for name in names:
            sdir = store_dir(data_dir, name)
            result = _reembed_one_store(
                sdir, ec, target, dry_run, bar=bar)
            per_store.append(result)
            total_scanned += result.get('scanned', 0)
            total_reembedded += result.get(
                'reembedded' if not dry_run else 'would_reembed', 0)
            bar.set_postfix(
                reembedded=total_reembedded, refresh=False)
    finally:
        bar.close()

    out: dict = {
        'fingerprint': {
            'provider': target.provider,
            'model': target.model,
            'dim': target.dim,
            },
        'stores': per_store,
        'total_scanned': total_scanned,
        }
    if dry_run:
        out['total_would_reembed'] = total_reembedded
        out['dry_run'] = 1
    else:
        out['total_reembedded'] = total_reembedded
    _json_out(out)


@embed_grp.command('swap')
@click.option(
    '--to', 'to_model', default='',
    help="Target embed model (e.g. 'voyage-3-large'). Resolved with"
         " the active provider unless --provider is given.")
@click.option(
    '--provider', 'to_provider', default='',
    help='Target embed provider (default: active provider from env).')
@click.option(
    '--resume', 'resume', is_flag=True, default=False,
    help='Continue an in-flight swap from the recorded cursor.')
@click.option(
    '--abort', 'abort', is_flag=True, default=False,
    help='Discard the in-flight swap. Drops embedding_pending and'
         ' clears all swap meta. After cutover, this is one-way: the'
         ' old embeddings are gone, so reverting requires running swap'
         ' again with the old model (full re-embed cost).')
@click.pass_context
def embed_swap(
        ctx: click.Context, to_model: str, to_provider: str,
        resume: bool, abort: bool) -> None:
    """Online per-store swap to a new embed model.

    Postgres: shadow `embedding_pending vector(N)` column with HNSW
    built CONCURRENTLY, backfilled `WHERE embedding_pending IS NULL`,
    cut over in one transaction (drop + rename). SQLite: shadow
    `embedding_pending BLOB` column populated under
    `write_lock("embed_swap")`, cutover is `update insights set
    embedding=embedding_pending, embedding_pending=null`. Recall
    keeps reading `embedding` throughout.

    Rollback note: cutover is one-way -- old embeddings are dropped.
    Reverting requires running swap again with the old model (full
    re-embed cost). Use `--abort` BEFORE cutover to discard the
    in-flight backfill safely.

    `MEMMAN_EMBED_SWAP_BATCH_SIZE` (default 200) tunes the HTTP
    batch size; `MEMMAN_EMBED_SWAP_INDEX_TIMEOUT` (default 0 =
    unlimited) caps the Postgres HNSW build.
    """
    from memman.embed import registry as _ec_registry
    from memman.embed.fingerprint import Fingerprint, stored_fingerprint
    from memman.embed.fingerprint import write_fingerprint
    from memman.embed.swap import SwapPlan
    from memman.embed.swap import abort_swap as _abort_swap
    from memman.embed.swap import read_progress, run_swap
    from memman.store.factory import open_backend

    if abort and resume:
        raise click.ClickException(
            '--abort and --resume are mutually exclusive')

    data_dir = ctx.obj['data_dir']
    name = _resolve_store_name(data_dir, ctx.obj['store'])
    with open_backend(name, data_dir) as backend:
        if abort:
            _abort_swap(backend)
            _json_out({'store': name, 'state': 'aborted'})
            return

        progress = read_progress(backend)
        if resume:
            if progress.state == '':
                raise click.ClickException(
                    f'no in-flight swap on store {name!r}')
            target_provider = progress.target_provider
            target_model = progress.target_model
            target_dim = progress.target_dim
        else:
            if progress.state and progress.state not in {'', 'done'}:
                raise click.ClickException(
                    f'store {name!r} has an in-flight swap'
                    f' (state={progress.state}); use --resume or'
                    ' --abort')
            if not to_model:
                raise click.ClickException(
                    '--to <model> is required to start a new swap')
            if not to_provider:
                to_provider = (
                    config.get(config.EMBED_PROVIDER) or 'voyage')
            target_provider = to_provider
            target_model = to_model
            target_dim = 0

        ec_new = _ec_registry.get_for(target_provider, target_model)
        if not ec_new.available():
            raise click.ClickException(ec_new.unavailable_message())
        if target_dim == 0:
            target_dim = ec_new.dim
        if target_dim <= 0:
            raise click.ClickException(
                f'failed to discover dim for {target_provider}:'
                f'{target_model}; provider should expose dim after'
                ' prepare()')

        plan = SwapPlan(
            target_provider=target_provider,
            target_model=target_model,
            target_dim=target_dim)

        with backend.swap_lock() as held:
            if not held:
                raise click.ClickException(
                    f'another swap is in progress on store {name!r}')
            _require_stopped('swap')
            progress = run_swap(backend, ec_new, plan)

        # Notes:
        # - Everything below runs AFTER the one-way cutover, so a
        #   backend failure here must not read as a retryable swap.
        #   `run_swap` already wrote the fingerprint in one
        #   transaction with its own state cleanup, which makes this
        #   block a confirming re-read; the generic seam message
        #   would describe it exactly as it describes a failure to
        #   connect before any data moved.
        # - Re-running the same swap IS safe, and that is worth
        #   saying rather than leaving the operator to guess:
        #   `run_swap` returns DONE at once when the stored
        #   fingerprint already matches the target, so it re-embeds
        #   nothing.
        try:
            fp = stored_fingerprint(backend) or Fingerprint(
                provider=plan.target_provider,
                model=plan.target_model,
                dim=plan.target_dim)
            if fp.provider != plan.target_provider:
                write_fingerprint(
                    backend,
                    Fingerprint(
                        provider=plan.target_provider,
                        model=plan.target_model,
                        dim=plan.target_dim))
                fp = Fingerprint(
                    provider=plan.target_provider,
                    model=plan.target_model,
                    dim=plan.target_dim)
        # Notes:
        # - BOTH types are reachable and neither is redundant: the
        #   Postgres backend translates at its connection scope, while
        #   the SQLite backend deliberately does not translate a
        #   statement failure, so the same read raises `BackendError`
        #   on one backend and `sqlite3.Error` on the other. Catching
        #   only the first left this unfixed on the default backend.
        except (BackendError, sqlite3.Error) as exc:
            raise click.ClickException(
                f'store {name!r}: the vector cutover to'
                f' {plan.target_provider}:{plan.target_model} COMPLETED,'
                f' and only the fingerprint check after it failed:'
                f' {exc}. No data is pending and nothing was rolled'
                ' back. Re-run the same `memman embed swap --to` to'
                ' confirm the state; it re-embeds nothing when the'
                ' stored fingerprint already matches.') from exc

        _json_out({
            'store': name,
            'state': progress.state,
            'fingerprint': {
                'provider': fp.provider,
                'model': fp.model,
                'dim': fp.dim,
                },
            })
