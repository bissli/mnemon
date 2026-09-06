---
name: memman
description: Persistent memory CLI for LLM agents. Store facts, recall past knowledge, link related memories, manage lifecycle.
---

# memman

`memman` is a CLI on PATH — invoke commands directly via Bash. Memory is
organized into typed insights and a graph of edges between them. Writes
are queued and enriched in the background; reads are intent-aware.

## Storing what you learn

Store one self-contained fact per call. Pick the most accurate `--cat`.
Writes link into one temporal chain by session, which is what WHEN
recall walks. Omit `--session`: it reads `$CLAUDE_CODE_SESSION_ID` by
itself. Pass it only to pin a different id.

```bash
memman remember "<fact>" --cat <category> --imp <1-5> --entities "e1,e2" --source agent
```

Categories: `preference` · `decision` · `fact` · `insight` · `context`.
`--imp` is a sort key for listings and tie-breaks (1-5, default 3),
stored as passed. Pass 5 for a fact the whole system rests on.

A write is not guaranteed to land. The worker drops content its
extractor judges trivial, folds a fact that merely restates a stored
insight into that insight, and supersedes a stored insight the new text
contradicts (the old row keeps its content behind `superseded_by` and
leaves recall). A fact that contradicts several stored insights
supersedes each of them. All three complete as `done`, so the queue
reports success either way. When nothing at all was stored, the write is
filed in the skipped ledger: read it back with `memman scheduler queue
skipped`, which keeps the full content and the reason. A write that
stored even one fact is not filed, so a single folded fact in a
multi-fact write leaves no ledger row. To store text verbatim and bypass
all three, pass `--no-reconcile`.

To correct a stored insight by ID without losing its `access_count` and
edges (`corroboration_count` — the count of restatements, whether
byte-identical or reworded, shown in recall/get JSON but not under
`--brief` — resets, since the successor is a new row identity):

```bash
memman replace <id> "<new content>"
```

`replace` inherits the original's category, importance, entities,
and source unless you override per-flag. `--session` does not inherit:
the successor is written into today's chain. It also keeps the
replaced row's edges, so it stays linked to the original's chain as
well, bridging the two.

The original is superseded, not deleted: it keeps its content behind
`superseded_by`, leaves every recall and listing, and `memman insights
show <id> --history` reads the chain back. When the correction was
stored as its own insight before the link was noticed, link the two
existing rows instead of writing a third:

```bash
memman supersede <old_id> <new_id>
```

## Recalling what you know

Recall: vector + graph traversal + cross-encoder reranker. Reranker
runs by default on multi-token queries and auto-skips on 1-2 token
queries.

```bash
memman recall "<query>" --brief --limit 20 --session <id>
```

Add `--intent WHY|WHEN|ENTITY` to bias the ranking when intent is
unambiguous (cause/effect, timeline, entity-centric). Add `--cat` or
`--source` to filter.

Recall returns rows even when nothing matches: a recency channel
seeds the newest insights as anchors regardless. An empty `results`
therefore means the store itself is empty, not that the query failed. A full page is
therefore not evidence that anything on it is relevant -- and a page
that looks thin usually is not, because the store nearly always holds
something bearing on a query drawn from the same work. Judge each row
on its merits against the query. Each row carries its own `score` and
per-channel `signals`; compare them WITHIN the page and never against
a fixed number, because the scale belongs to whichever reranker is
configured. Report that nothing relevant is stored only when no row
bears on the query.

`--basic` returns before ranking, so it carries no `score` and no
`signals` to judge with at all. If a paraphrase returns nothing that
bears on the query, re-ask in the store's own words before concluding
it is empty.

Rows come back in relevance order at every `--limit`, so the first `n`
of a page of `m` are exactly a page of `n`. On `WHY`,
`meta.causal_edges` carries the `[cause, effect]` pairs among the
returned rows.

`--min-score` drops rows whose keyword plus similarity sum is under
the floor (0.0 to 2.0, `0.0` = off, rejected with `--basic` -- a
filter that quietly did nothing would certify rows it never checked,
unlike `--intent` and `--expand`, which `--basic` names in
`meta.ignored` instead). Leave it off by default: the deep tail of a
recall is often where the useful row sits. There is no value worth
copying -- the usable band depends on the embedder and the store, so
find it by running the query with and without a floor.

For a fast token-only lookup that skips graph and reranking (cheap,
no network cost; rows come back ranked by importance, then recency):

```bash
memman recall "<keyword>" --basic
```

Add `--brief` to cut each insight to `id`, `category`, `importance`,
`created_at`, and `summary`. Use it when scanning for which insight to open rather
than reading the insights themselves. It works on both paths; on the
ranked path the `score`, `intent`, and `signals` keys around each
insight are kept. A row left without a summary falls back to its
content instead, so no row comes back blank. `truncated: true` means
the text you got is a raw content prefix cut at 200 characters. Its
ABSENCE does not mean you hold the whole row: a summarized row carries
no marker however much its summary left out, and a fallback row is
marked only when its content ran past the cut. `memman insights show
<id>` is how you read the rest of any row worth more than a scan.

A brief row carries `created_at`, so a WHEN query reconstructs a
timeline by sorting on that field rather than by reading row order,
which is relevance-ordered on every path.

Read a single insight by ID:

```bash
memman insights show <id>
```

`remember` and `replace` return a `queue_uuid`. It is stamped on every
insight that write produces, so it answers "where did my write land"
once the scheduler has drained:

```bash
memman insights by-queue <queue_uuid>
```

`count: 0` has three causes: the write is still queued, it stored
nothing (see `memman scheduler queue skipped`), or it went to a
different store -- the queue is global while this reads one store.

## Forgetting

```bash
memman forget <id>                    # soft-delete
memman insights review                # scan for content quality issues
```

`insights review` only surfaces rows — it deletes nothing. Use
`forget <id>` to actually remove. Nothing else deletes: the store is
uncapped and a stored insight persists until someone forgets it.
Supersession (`replace`, `supersede`, a reconcile merge) hides without
deleting; `memman unsupersede <id>` brings a superseded row back once
its successor has been forgotten.

## Working with relationships

The graph holds typed edges between insights. Auto-edges (semantic,
temporal, entity) are computed during enrichment; manual links express
relationships you've identified:

```bash
memman graph link <src> <tgt> --type semantic --weight 0.85
memman graph link <src> <tgt> --type causal --weight 0.8 \
    --meta '{"sub_type": "causes"}'
```

Causal `sub_type` values: `causes` · `enables` · `prevents`.

Traverse from any insight:

```bash
memman graph related <id> --depth 2
memman graph related <id> --edge causal
```

## Inspecting the system

```bash
memman status                         # insight count, store, scheduler state
memman doctor                         # health check (sqlite, queue, keys, scheduler, env_completeness)
```

## Operator commands the agent rarely runs

| Command                                              | Purpose                             |
| ---------------------------------------------------- | ----------------------------------- |
| `memman log list [--since 7d --stats --text]`        | Operation audit log                 |
| `memman scheduler status`                            | Worker state, next run, log paths   |
| `memman scheduler queue list`                        | Inspect deferred-write queue        |
| `memman store list` / `use <name>` / `create <name>` | Multi-store management              |
| `memman config show`                                 | Effective settings (env + on-disk)  |

## Guardrails

- Never store secrets, passwords, or tokens.
- Max 8,000 characters per insight; chunk longer content.
- One self-contained fact per `remember` call. The worker extracts one
  fact from each call and folds every claim in the text into it, so a
  second unrelated subject rides along and goes stale with the first;
  give it its own call.
- `--source agent` for the agent's own conclusion, a locator (URL,
  script, dataset pull) for imported material; `user`, the default, is
  for the user's words. Recall's `--source` filter is an exact match on
  that string.
- No session, no temporal chain. You do not have to pass one:
  `--session` reads `$MEMMAN_SESSION_ID`, then
  `$CLAUDE_CODE_SESSION_ID`. Claude Code exports that second one into
  every Bash call, a subagent's included, with the parent's id. An
  explicit `--session <id>` beats both.
