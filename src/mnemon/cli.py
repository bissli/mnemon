"""Click CLI for mnemon — all 14 commands."""

import json
import os
import pathlib
import uuid
from datetime import datetime, timezone

import click
import mnemon
from mnemon.model import VALID_CATEGORIES, VALID_EDGE_TYPES, Edge, Insight
from mnemon.model import format_timestamp, is_immune
from mnemon.store.db import default_data_dir, list_stores
from mnemon.store.db import open_db, open_read_only, read_active, store_dir
from mnemon.store.db import store_exists, valid_store_name, write_active


def _json_out(obj: object) -> None:
    """Write JSON to stdout with 2-space indent, sorted keys."""
    click.echo(json.dumps(obj, indent=2, sort_keys=True))


def _resolve_store_name(data_dir: str, store_flag: str) -> str:
    """Resolve effective store name."""
    if store_flag:
        return store_flag
    env = os.environ.get('MNEMON_STORE', '')
    if env:
        return env
    return read_active(data_dir)


def _open_db(ctx: click.Context) -> 'DB':
    """Open the database using context options."""
    data_dir = ctx.obj['data_dir']
    store_flag = ctx.obj['store']
    read_only = ctx.obj['readonly']

    name = _resolve_store_name(data_dir, store_flag)
    sdir = store_dir(data_dir, name)

    if read_only:
        return open_read_only(sdir)

    return open_db(sdir)


def _trunc_id(id: str) -> str:
    """Truncate an ID to 8 characters for display."""
    return id[:8] if len(id) > 8 else id


def _insight_to_dict(i: Insight) -> dict:
    """Serialize an Insight for JSON output."""
    d = {
        'id': i.id,
        'content': i.content,
        'category': i.category,
        'importance': i.importance,
        'tags': i.tags,
        'entities': i.entities,
        'source': i.source,
        'access_count': i.access_count,
        'created_at': format_timestamp(i.created_at),
        'updated_at': format_timestamp(i.updated_at),
        }
    if i.deleted_at:
        d['deleted_at'] = format_timestamp(i.deleted_at)
    return d


@click.group()
@click.version_option(version=mnemon.__version__, prog_name='mnemon')
@click.option('--data-dir', default=None, help='Base data directory (env: MNEMON_DATA_DIR)')
@click.option('--store', 'store_name', default='', help='Named memory store')
@click.option('--readonly', is_flag=True, default=False, help='Open database in read-only mode')
@click.pass_context
def cli(ctx: click.Context, data_dir: str | None, store_name: str, readonly: bool) -> None:
    """Memory daemon for LLM agents."""
    if data_dir is None:
        data_dir = os.environ.get('MNEMON_DATA_DIR', default_data_dir())
    ctx.ensure_object(dict)
    ctx.obj['data_dir'] = data_dir
    ctx.obj['store'] = store_name
    ctx.obj['readonly'] = readonly


@cli.command()
@click.argument('content', nargs=-1, required=True)
@click.option('--cat', default='general', help='Category')
@click.option('--imp', default=3, type=int, help='Importance (1-5)')
@click.option('--tags', default='', help='Comma-separated tags')
@click.option('--source', default='user', help='Source')
@click.option('--entities', default='', help='Comma-separated entities')
@click.option('--no-diff', is_flag=True, default=False, help='Skip duplicate detection')
@click.pass_context
def remember(ctx: click.Context, content: tuple[str, ...], cat: str,
             imp: int, tags: str, source: str, entities: str,
             no_diff: bool) -> None:
    """Store a new insight."""
    content_str = ' '.join(content)
    content_bytes = len(content_str.encode('utf-8'))
    if content_bytes > 8000:
        raise click.ClickException(
            f'content too long ({content_bytes} chars, max 8000);'
            ' consider chunking into multiple remember calls')

    if cat not in VALID_CATEGORIES:
        raise click.ClickException(
            f'invalid category {cat!r}; valid: preference, decision,'
            ' fact, insight, context, general')
    if imp < 1 or imp > 5:
        raise click.ClickException(
            f'importance must be 1-5, got {imp}')

    tag_list: list[str] = []
    if tags:
        for t in tags.split(','):
            t = t.strip()
            if t:
                if len(t) > 100:
                    raise click.ClickException(
                        f'tag too long ({len(t)} chars, max 100):'
                        f' {t[:50]}')
                tag_list.append(t)
        if len(tag_list) > 20:
            raise click.ClickException(
                f'too many tags ({len(tag_list)}, max 20)')

    entity_list: list[str] = []
    if entities:
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

    now = datetime.now(timezone.utc)
    insight = Insight(
        id=str(uuid.uuid4()), content=content_str,
        category=cat, importance=imp, tags=tag_list,
        entities=entity_list, source=source,
        created_at=now, updated_at=now)

    db = _open_db(ctx)
    try:
        _remember_impl(db, insight, content_str, no_diff)
    finally:
        db.close()


def _remember_impl(db: 'DB', insight: Insight, content: str, no_diff: bool) -> None:
    """Core remember implementation."""
    from mnemon.embed.ollama import Client as EmbedClient
    from mnemon.embed.vector import deserialize_vector, serialize_vector
    from mnemon.graph.causal import find_causal_candidates
    from mnemon.graph.engine import on_insight_created
    from mnemon.graph.semantic import find_semantic_candidates
    from mnemon.search.diff import diff as run_diff
    from mnemon.search.quality import check_content_quality
    from mnemon.store.node import MAX_INSIGHTS, auto_prune
    from mnemon.store.node import get_all_active_insights
    from mnemon.store.node import get_all_embeddings, insert_insight
    from mnemon.store.node import refresh_effective_importance
    from mnemon.store.node import soft_delete_insight, update_embedding
    from mnemon.store.node import update_entities
    from mnemon.store.oplog import log_op

    ec = EmbedClient()
    embedding_blob = None
    embedding_vec = None
    if ec.available():
        try:
            embedding_vec = ec.embed(content)
            embedding_blob = serialize_vector(embedding_vec)
        except Exception:
            pass

    embed_cache: dict[str, list[float]] | None = None
    if ec.available():
        db_embeds = get_all_embeddings(db)
        if db_embeds:
            embed_cache = {}
            for eid, _content, blob in db_embeds:
                v = deserialize_vector(blob)
                if v is not None:
                    embed_cache[eid] = v

    diff_action = 'added'
    replaced_id = ''
    diff_suggestion = 'ADD'

    if no_diff:
        diff_action = 'added'
        diff_suggestion = 'ADD'
    else:
        all_insights = get_all_active_insights(db)
        existing_embed = None
        if embed_cache:
            existing_embed = list(embed_cache.items())
        result = run_diff(
            all_insights, content, limit=5,
            new_embedding=embedding_vec,
            existing_embed=existing_embed)
        diff_suggestion = result['suggestion']

        if diff_suggestion == 'DUPLICATE':
            diff_action = 'skipped'
            if result['matches']:
                replaced_id = result['matches'][0]['id']
        elif diff_suggestion in {'CONFLICT', 'UPDATE'}:
            diff_action = 'updated'
            if result['matches']:
                replaced_id = result['matches'][0]['id']
        else:
            diff_action = 'added'

    quality_warnings = check_content_quality(content)

    if diff_action == 'skipped':
        log_op(db, 'diff-skip', insight.id,
               f'duplicate of {replaced_id}')
        output = {
            'id': insight.id,
            'content': content,
            'action': 'skipped',
            'diff_suggestion': diff_suggestion,
            'replaced_id': replaced_id,
            'quality_warnings': quality_warnings,
            }
        _json_out(output)
        return

    edge_stats = {'temporal': 0, 'entity': 0, 'causal': 0, 'semantic': 0}
    ei = 0.0
    pruned = 0
    embedded = False

    def tx_body() -> None:
        nonlocal edge_stats, ei, pruned, embedded, embed_cache

        if diff_action == 'updated' and replaced_id:
            try:
                soft_delete_insight(db, replaced_id)
                log_op(db, 'diff-replace', replaced_id,
                       f'replaced by {insight.id}')
                if embed_cache and replaced_id in embed_cache:
                    del embed_cache[replaced_id]
            except Exception as e:
                click.echo(
                    f'warning: soft-delete {replaced_id}: {e}',
                    err=True)

        insert_insight(db, insight)

        if embedding_blob is not None:
            update_embedding(db, insight.id, embedding_blob)
            embedded = True
            if embed_cache is not None:
                embed_cache[insight.id] = embedding_vec

        edge_stats = on_insight_created(db, insight, embed_cache)

        if insight.entities:
            update_entities(db, insight.id, insight.entities)

        try:
            ei_val = refresh_effective_importance(db, insight.id)
        except Exception:
            ei_val = 0.0

        try:
            pruned_val = auto_prune(db, MAX_INSIGHTS, [insight.id])
        except Exception:
            pruned_val = 0

        nonlocal ei, pruned
        ei = ei_val
        pruned = pruned_val

        log_op(db, 'remember', insight.id, insight.content)

    try:
        db.in_transaction(tx_body)
    except Exception:
        embed_cache = None
        raise

    semantic_candidates = find_semantic_candidates(
        db, insight, embed_cache)
    if semantic_candidates is None:
        semantic_candidates = []

    causal_candidates = find_causal_candidates(db, insight)
    if causal_candidates is None:
        causal_candidates = []

    output: dict = {
        'id': insight.id,
        'content': insight.content,
        'category': insight.category,
        'importance': insight.importance,
        'tags': insight.tags,
        'entities': insight.entities,
        'action': diff_action,
        'diff_suggestion': diff_suggestion,
        'created_at': format_timestamp(insight.created_at),
        'edges_created': edge_stats,
        'semantic_candidates': semantic_candidates,
        'causal_candidates': causal_candidates,
        'quality_warnings': quality_warnings,
        'embedded': embedded,
        'effective_importance': ei,
        'auto_pruned': pruned,
        }
    if replaced_id:
        output['replaced_id'] = replaced_id
    _json_out(output)


@cli.command()
@click.argument('keyword', nargs=-1, required=True)
@click.option('--cat', default='', help='Filter by category')
@click.option('--limit', default=10, type=int, help='Max results')
@click.option('--source', default='', help='Filter by source')
@click.option('--basic', is_flag=True, default=False, help='Simple SQL LIKE matching')
@click.option('--smart', is_flag=True, default=False, hidden=True)
@click.option('--intent', default='', help='Override intent')
@click.pass_context
def recall(ctx: click.Context, keyword: tuple[str, ...], cat: str,
           limit: int, source: str, basic: bool, smart: bool,
           intent: str) -> None:
    """Retrieve insights by keyword."""
    from mnemon.embed.ollama import Client as EmbedClient
    from mnemon.graph.entity import extract_entities
    from mnemon.search.intent import intent_from_string
    from mnemon.search.recall import intent_aware_recall
    from mnemon.store.node import increment_access_count, query_insights
    from mnemon.store.oplog import log_op

    keyword_str = ' '.join(keyword)
    db = _open_db(ctx)
    try:
        if basic:
            results = query_insights(
                db, keyword=keyword_str, category=cat,
                source=source, limit=limit)
            for r in results:
                increment_access_count(db, r.id)
            log_op(db, 'recall:basic', '',
                   f'q={keyword_str} hits={len(results)}')
            _json_out([_insight_to_dict(r) for r in results])
            return

        intent_override = None
        if intent:
            try:
                intent_override = intent_from_string(intent)
            except ValueError as e:
                raise click.ClickException(str(e))

        ec = EmbedClient()
        query_vec = None
        if ec.available():
            try:
                query_vec = ec.embed(keyword_str)
            except Exception:
                pass

        query_entities = extract_entities(keyword_str)

        resp = intent_aware_recall(
            db, keyword_str, query_vec, query_entities,
            limit, intent_override)

        for r in resp['results']:
            increment_access_count(db, r['insight'].id)

        log_op(db, 'recall', '',
               f'q={keyword_str} hits={len(resp["results"])}')

        out = {
            'results': [
                {
                    'insight': _insight_to_dict(r['insight']),
                    'score': r['score'],
                    'intent': r['intent'],
                    'signals': r['signals'],
                    **({'via': r['via']} if r.get('via') else {}),
                    }
                for r in resp['results']
                ],
            'meta': resp['meta'],
            }
        _json_out(out)
    finally:
        db.close()


@cli.command()
@click.argument('query', nargs=-1, required=True)
@click.option('--limit', default=10, type=int, help='Max results')
@click.pass_context
def search(ctx: click.Context, query: tuple[str, ...], limit: int) -> None:
    """Token-based keyword search."""
    from mnemon.search.keyword import keyword_search
    from mnemon.store.node import get_all_active_insights
    from mnemon.store.node import increment_access_count
    from mnemon.store.oplog import log_op

    query_str = ' '.join(query)
    db = _open_db(ctx)
    try:
        all_insights = get_all_active_insights(db)
        results = keyword_search(all_insights, query_str, limit)
        for ins, _score in results:
            increment_access_count(db, ins.id)
        log_op(db, 'search', '',
               f'q={query_str} hits={len(results)}')
        out = [
            {
                'id': ins.id,
                'content': ins.content,
                'category': ins.category,
                'importance': ins.importance,
                'tags': ins.tags,
                'score': score,
                }
            for ins, score in results
            ]
        _json_out(out)
    finally:
        db.close()


@cli.command()
@click.argument('id')
@click.pass_context
def forget(ctx: click.Context, id: str) -> None:
    """Soft-delete an insight."""
    from mnemon.store.node import soft_delete_insight
    from mnemon.store.oplog import log_op

    db = _open_db(ctx)
    try:
        soft_delete_insight(db, id)
        log_op(db, 'forget', id, '')
        _json_out({
            'id': id,
            'status': 'deleted',
            'message': 'Insight soft-deleted successfully',
            })
    except ValueError as e:
        raise click.ClickException(str(e))
    finally:
        db.close()


@cli.command()
@click.argument('source_id')
@click.argument('target_id')
@click.option('--type', 'edge_type', default='semantic', help='Edge type')
@click.option('--weight', default=0.5, type=float, help='Edge weight')
@click.option('--meta', default='', help='JSON metadata')
@click.pass_context
def link(ctx: click.Context, source_id: str, target_id: str,
         edge_type: str, weight: float, meta: str) -> None:
    """Create a manual edge between two insights."""
    from mnemon.store.edge import insert_edge
    from mnemon.store.node import get_insight_by_id
    from mnemon.store.oplog import log_op

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
    metadata['created_by'] = 'claude'

    now = datetime.now(timezone.utc)
    db = _open_db(ctx)
    try:
        if get_insight_by_id(db, source_id) is None:
            raise click.ClickException(
                f'insight {source_id} not found')
        if get_insight_by_id(db, target_id) is None:
            raise click.ClickException(
                f'insight {target_id} not found')

        insert_edge(db, Edge(
            source_id=source_id, target_id=target_id,
            edge_type=edge_type, weight=weight,
            metadata=metadata, created_at=now))
        insert_edge(db, Edge(
            source_id=target_id, target_id=source_id,
            edge_type=edge_type, weight=weight,
            metadata=metadata, created_at=now))
        log_op(db, 'link', source_id,
               f'{source_id} <-> {target_id} ({edge_type})')
        _json_out({
            'status': 'linked',
            'source_id': source_id,
            'target_id': target_id,
            'edge_type': edge_type,
            'weight': weight,
            'metadata': metadata,
            })
    finally:
        db.close()


@cli.command()
@click.argument('id')
@click.option('--edge', default='', help='Filter by edge type')
@click.option('--depth', default=2, type=int, help='Max traversal depth')
@click.pass_context
def related(ctx: click.Context, id: str, edge: str,
            depth: int) -> None:
    """Find connected insights via graph traversal."""
    from mnemon.graph.bfs import BFSOptions, bfs

    db = _open_db(ctx)
    try:
        nodes = bfs(db, id, BFSOptions(
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
    finally:
        db.close()


@cli.group(invoke_without_command=True)
@click.pass_context
def store(ctx: click.Context) -> None:
    """Manage named memory stores."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(store_list)


@store.command('list')
@click.pass_context
def store_list(ctx: click.Context) -> None:
    """List all stores."""
    data_dir = ctx.obj['data_dir']
    stores = list_stores(data_dir)
    if not stores:
        click.echo(
            "  (no stores yet — run 'mnemon store create <name>'"
            " or any command to create default)")
        return
    active = read_active(data_dir)
    for name in stores:
        prefix = '* ' if name == active else '  '
        click.echo(f'{prefix}{name}')


@store.command('create')
@click.argument('name')
@click.pass_context
def store_create(ctx: click.Context, name: str) -> None:
    """Create a new store."""
    data_dir = ctx.obj['data_dir']
    if not valid_store_name(name):
        raise click.ClickException(
            f'invalid store name {name!r}')
    if store_exists(data_dir, name):
        raise click.ClickException(
            f'store "{name}" already exists')
    sdir = store_dir(data_dir, name)
    db = open_db(sdir)
    db.close()
    click.echo(f'Created store "{name}"')


@store.command('set')
@click.argument('name')
@click.pass_context
def store_set(ctx: click.Context, name: str) -> None:
    """Set the active store."""
    data_dir = ctx.obj['data_dir']
    if not store_exists(data_dir, name):
        raise click.ClickException(
            f"store \"{name}\" does not exist"
            f" (use 'mnemon store create {name}' first)")
    write_active(data_dir, name)
    click.echo(f'Active store set to "{name}"')


@store.command('remove')
@click.argument('name')
@click.pass_context
def store_remove(ctx: click.Context, name: str) -> None:
    """Remove a store."""
    import shutil
    data_dir = ctx.obj['data_dir']
    if not store_exists(data_dir, name):
        raise click.ClickException(
            f"store \"{name}\" does not exist"
            f" (use 'mnemon store create {name}' first)")
    active = read_active(data_dir)
    if name == active:
        raise click.ClickException(
            f"cannot remove the active store \"{name}\""
            f" (switch first with 'mnemon store set <other>')")
    sdir = store_dir(data_dir, name)
    shutil.rmtree(sdir)
    click.echo(f'Removed store "{name}"')


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show database statistics."""
    from mnemon.store.node import get_stats

    db = _open_db(ctx)
    try:
        stats = get_stats(db)
        stats['db_path'] = db.path
        try:
            stats['db_size_bytes'] = pathlib.Path(db.path).stat().st_size
        except OSError:
            stats['db_size_bytes'] = 0
        _json_out(stats)
    finally:
        db.close()


@cli.command()
@click.option('--limit', default=20, type=int, help='Max entries')
@click.pass_context
def log(ctx: click.Context, limit: int) -> None:
    """Show operation log."""
    from mnemon.store.oplog import get_oplog

    db = _open_db(ctx)
    try:
        entries = get_oplog(db, limit)
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
                _trunc_id(e['insight_id']) if e['insight_id'] else '',
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
    finally:
        db.close()


@cli.command()
@click.option('--threshold', default=0.5, type=float, help='EI threshold')
@click.option('--limit', default=20, type=int, help='Max candidates')
@click.option('--keep', default='', help='Insight ID to keep')
@click.pass_context
def gc(ctx: click.Context, threshold: float, limit: int, keep: str) -> None:
    """Garbage collection / retention lifecycle."""
    from mnemon.store.node import MAX_INSIGHTS, boost_retention
    from mnemon.store.node import get_insight_by_id
    from mnemon.store.node import get_retention_candidates
    from mnemon.store.node import refresh_effective_importance
    from mnemon.store.oplog import log_op

    db = _open_db(ctx)
    try:
        if keep:
            ins = get_insight_by_id(db, keep)
            if ins is None:
                raise click.ClickException(
                    f'insight {keep} not found or already deleted')
            boost_retention(db, keep)
            ei = refresh_effective_importance(db, keep)
            new_access = ins.access_count + 3
            log_op(db, 'gc-keep', keep, f'access+3, ei={ei:.4f}')
            _json_out({
                'status': 'retained',
                'id': keep,
                'content': ins.content,
                'new_access': new_access,
                'effective_importance': ei,
                'immune': is_immune(ins.importance, new_access),
                })
            return

        candidates, total = get_retention_candidates(
            db, threshold, limit)

        out_candidates = []
        for c in candidates:
            ins = c['insight']
            out_candidates.append({
                'id': ins.id,
                'content': ins.content,
                'category': ins.category,
                'importance': ins.importance,
                'access_count': ins.access_count,
                'effective_importance': c['effective_importance'],
                'days_since_access': c['days_since_access'],
                'edge_count': c['edge_count'],
                'immune': c['immune'],
                })

        _json_out({
            'total_insights': total,
            'threshold': threshold,
            'candidates_found': len(candidates),
            'candidates': out_candidates,
            'max_insights': MAX_INSIGHTS,
            'actions': {
                'purge': 'mnemon forget <id>',
                'keep': 'mnemon gc --keep <id>',
                },
            })
    finally:
        db.close()


@cli.command()
@click.option('--format', 'fmt', default='dot', help='Output format: dot or html')
@click.option('-o', '--output', 'output_path', default='-', help='Output file (- for stdout)')
@click.pass_context
def viz(ctx: click.Context, fmt: str, output_path: str) -> None:
    """Export mnemon graph for visualization."""
    from mnemon.store.edge import get_all_edges
    from mnemon.store.node import get_all_active_insights

    db = _open_db(ctx)
    try:
        insights = get_all_active_insights(db)
        edges = get_all_edges(db)

        if fmt == 'dot':
            out = _render_dot(insights, edges)
        elif fmt == 'html':
            out = _render_html(insights, edges)
        else:
            raise click.ClickException(
                f'unsupported format: {fmt} (use dot or html)')

        if output_path in {'', '-'}:
            click.echo(out, nl=False)
        else:
            pathlib.Path(output_path).write_text(out)
            click.echo(f'written to {output_path}', err=True)
    finally:
        db.close()


@cli.command()
@click.argument('id', required=False, default=None)
@click.option('--all', 'backfill', is_flag=True, default=False, help='Backfill all insights')
@click.option('--status', 'show_status', is_flag=True, default=False, help='Show coverage stats')
@click.pass_context
def embed(ctx: click.Context, id: str | None, backfill: bool, show_status: bool) -> None:
    """Manage embeddings."""
    from mnemon.embed.ollama import Client as EmbedClient
    from mnemon.embed.vector import serialize_vector
    from mnemon.store.node import embedding_stats, get_insight_by_id
    from mnemon.store.node import get_insights_without_embedding
    from mnemon.store.node import update_embedding

    db = _open_db(ctx)
    try:
        ec = EmbedClient()

        if show_status:
            total, embedded = embedding_stats(db)
            coverage = f'{embedded * 100 // total}%' if total > 0 else '0%'
            _json_out({
                'total_insights': total,
                'embedded': embedded,
                'coverage': coverage,
                'ollama_available': ec.available(),
                'model': ec.model,
                })
            return

        if backfill:
            if not ec.available():
                raise click.ClickException(ec.unavailable_message())
            missing = get_insights_without_embedding(db, 1000)
            if not missing:
                _json_out({
                    'status': 'complete',
                    'message':
                        'all insights already have embeddings',
                    })
                return
            succeeded = 0
            failed = 0
            for ins in missing:
                try:
                    vec = ec.embed(ins.content)
                    blob = serialize_vector(vec)
                    update_embedding(db, ins.id, blob)
                    succeeded += 1
                except Exception:
                    failed += 1
            _json_out({
                'status': 'backfill_complete',
                'succeeded': succeeded,
                'failed': failed,
                'model': ec.model,
                })
            return

        if id:
            if not ec.available():
                raise click.ClickException(ec.unavailable_message())
            ins = get_insight_by_id(db, id)
            if ins is None:
                raise click.ClickException(
                    f'insight {id} not found')
            vec = ec.embed(ins.content)
            blob = serialize_vector(vec)
            update_embedding(db, id, blob)
            _json_out({
                'status': 'embedded',
                'id': id,
                'dimension': len(vec),
                'model': ec.model,
                })
            return

        raise click.ClickException(
            'specify --all to backfill, --status to check coverage,'
            ' or provide an insight ID')
    finally:
        db.close()


@cli.command()
@click.option('--target', default='', help='Target environment')
@click.option('--eject', is_flag=True, default=False, help='Remove integration')
@click.option('--yes', 'auto_yes', is_flag=True, default=False, help='Skip confirmation')
@click.option('--global', 'use_global', is_flag=True, default=False, help='Use global scope')
@click.pass_context
def setup(ctx: click.Context, target: str, eject: bool, auto_yes: bool, use_global: bool) -> None:
    """Set up LLM CLI integration."""
    from mnemon.setup.claude import run_setup
    data_dir = ctx.obj['data_dir']
    run_setup(data_dir, target=target, eject=eject,
              auto_yes=auto_yes, use_global=use_global)


def _node_label(i: Insight) -> str:
    """Return a short display label for a node."""
    content = i.content.replace('\n', ' ')
    if len(content) > 60:
        content = content[:60] + '...'
    return f'[{i.category}] {content}'


def _category_color(c: str) -> str:
    """Return a color for a category."""
    colors = {
        'decision': '#e74c3c', 'fact': '#3498db',
        'insight': '#9b59b6', 'preference': '#2ecc71',
        'context': '#f39c12',
        }
    return colors.get(c, '#95a5a6')


def _edge_color(t: str) -> str:
    """Return a color for an edge type."""
    colors = {
        'temporal': '#aaaaaa', 'semantic': '#3498db',
        'causal': '#e74c3c', 'entity': '#2ecc71',
        }
    return colors.get(t, '#cccccc')


def _render_dot(insights: list[Insight],
                edges: list[Edge]) -> str:
    """Render a DOT graph."""
    lines = [
        'digraph mnemon {',
        '  rankdir=LR;',
        ('  node [shape=box, style="filled,rounded",'
         ' fontsize=10, fontname="Helvetica"];'),
        '  edge [fontsize=8, fontname="Helvetica"];',
        '',
        ]

    active = {i.id for i in insights}

    for i in insights:
        label = _node_label(i).replace('"', '\\"')
        short_id = _trunc_id(i.id)
        color = _category_color(i.category)
        lines.append(
            f'  "{i.id}" [label="{short_id}: {label}",'
            f' fillcolor="{color}", fontcolor="white"];')

    lines.append('')
    for e in edges:
        if e.source_id not in active or e.target_id not in active:
            continue
        color = _edge_color(e.edge_type)
        sub_type = e.metadata.get('sub_type', '')
        edge_label = sub_type or e.edge_type
        lines.append(
            f'  "{e.source_id}" -> "{e.target_id}"'
            f' [label="{edge_label}", color="{color}",'
            f' fontcolor="{color}"];')

    lines.extend(('}', ''))
    return '\n'.join(lines)


def _js_str(s: str) -> str:
    """Return a JSON-encoded string for JS embedding."""
    return json.dumps(s)


def _render_html(insights: list[Insight], edges: list[Edge]) -> str:
    """Render an HTML vis.js interactive page."""
    active = {i.id for i in insights}

    node_parts = []
    for i in insights:
        short_id = _trunc_id(i.id)
        label = _node_label(i).replace('\n', ' ')
        title = i.content.replace('\n', '\\n')
        color = _category_color(i.category)
        node_parts.append(
            f'{{id:{_js_str(i.id)},label:{_js_str(short_id + ": " + label)},'
            f'title:{_js_str(title)},color:{_js_str(color)},'
            f'font:{{color:"white"}}}}')
    nodes_js = ',\n'.join(node_parts)

    edge_parts = []
    for e in edges:
        if e.source_id not in active or e.target_id not in active:
            continue
        color = _edge_color(e.edge_type)
        sub_type = e.metadata.get('sub_type', '')
        edge_label = sub_type or e.edge_type
        edge_parts.append(
            f'{{from:{_js_str(e.source_id)},to:{_js_str(e.target_id)},'
            f'label:{_js_str(edge_label)},'
            f'color:{{color:{_js_str(color)}}},'
            f'arrows:"to",font:{{color:{_js_str(color)},size:10}}}}')
    edges_js = ',\n'.join(edge_parts)

    return _HTML_TEMPLATE.replace('%NODES%', nodes_js).replace(
        '%EDGES%', edges_js)


_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Mnemon Knowledge Graph</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
  body { margin: 0; padding: 0; background: #1a1a2e; font-family: sans-serif; }
  #graph { width: 100vw; height: 100vh; }
  #legend { position: fixed; top: 10px; right: 10px; background: rgba(0,0,0,0.7);
    color: white; padding: 12px; border-radius: 8px; font-size: 12px; }
  .leg-item { display: flex; align-items: center; margin: 4px 0; }
  .leg-dot { width: 12px; height: 12px; border-radius: 50%; margin-right: 8px; }
  .leg-line { width: 20px; height: 3px; margin-right: 8px; }
</style>
</head>
<body>
<div id="graph"></div>
<div id="legend">
  <b>Nodes</b>
  <div class="leg-item"><div class="leg-dot" style="background:#e74c3c"></div>decision</div>
  <div class="leg-item"><div class="leg-dot" style="background:#3498db"></div>fact</div>
  <div class="leg-item"><div class="leg-dot" style="background:#9b59b6"></div>insight</div>
  <div class="leg-item"><div class="leg-dot" style="background:#2ecc71"></div>preference</div>
  <div class="leg-item"><div class="leg-dot" style="background:#f39c12"></div>context</div>
  <div class="leg-item"><div class="leg-dot" style="background:#95a5a6"></div>general</div>
  <br><b>Edges</b>
  <div class="leg-item"><div class="leg-line" style="background:#aaaaaa"></div>temporal</div>
  <div class="leg-item"><div class="leg-line" style="background:#3498db"></div>semantic</div>
  <div class="leg-item"><div class="leg-line" style="background:#e74c3c"></div>causal</div>
  <div class="leg-item"><div class="leg-line" style="background:#2ecc71"></div>entity</div>
</div>
<script>
var nodes = new vis.DataSet([%NODES%]);
var edges = new vis.DataSet([%EDGES%]);
var container = document.getElementById("graph");
var data = { nodes: nodes, edges: edges };
var options = {
  physics: { solver: "forceAtlas2Based", forceAtlas2Based: { gravitationalConstant: -30 } },
  interaction: { hover: true, tooltipDelay: 100 },
  nodes: { shape: "box", margin: 8, borderWidth: 0, font: { size: 11 } },
  edges: { smooth: { type: "continuous" }, font: { size: 9 } }
};
new vis.Network(container, data, options);
</script>
</body>
</html>"""
