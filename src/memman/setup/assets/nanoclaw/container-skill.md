---
name: memman
description: Persistent graph-based memory. Recall context before responding, remember insights after. Each group has private memory; global memory is read-only.
---

# memman — Persistent Memory

`memman` is a CLI on PATH inside the container. Memory is organized into
typed insights and a graph of edges between them. PID 1 of the container
is `memman scheduler serve`, which drains the write queue every 60
seconds. From the agent's perspective `remember` returns immediately
with `{action: queued, queue_id, queue_uuid}`; the new insight becomes
recallable within the next drain interval. Keep the `queue_uuid` if you
need to find what the write stored -- `memman insights by-queue <uuid>`
resolves it once the drain has run.

If `memman scheduler stop` is run inside the container, memman becomes
recall-only and the serve loop exits at its next iteration. Because the
serve loop is PID 1, the container also exits. To resume, restart the
container — do not invoke `scheduler stop` inside a container where
serve is PID 1 unless you intend to terminate the container.

## Memory stores

- **Private** (default): per-group, read-write. All writes go here.
- **Global**: shared across all groups, read-only. Append `--store global` to read it.

Never write to the global store — the mount is read-only.

## Recall — before responding

**Default: recall on every new user message**, unless ALL of these apply:
- Direct follow-up within a topic already fully in context
- No reference to past sessions, decisions, or preferences
- No knowledge dependency beyond the current conversation

```bash
memman recall "<query>" --brief --limit 20
memman --store global recall "<query>" --brief --limit 20
```

The cross-encoder reranker runs by default on multi-token queries
(auto-skipped on 1-2 token queries).

Note: `--store` is a root-group flag and must come **before** the subcommand name (e.g. `recall`).

Craft a focused, keyword-rich query — do not pass the raw user prompt.

## Remember — after responding

Run this decision tree after every substantive response:

**Step 1 — Does this exchange contain any of these?**
  a) User directive — preference, decision, correction, explicit "remember this"
  b) Reasoning conclusion — non-trivial judgment from multi-source synthesis
  c) Durable observed state — system fact, environment detail, architectural finding
  → No to all → STOP.

**Step 2 — Does this correct something already stored?** Then the text
says so: it names what is no longer true and what is true now, in one
self-contained statement, and goes in with `memman remember` like any
other fact. The worker finds every stored row the fact contradicts and
supersedes each with one merge that keeps their still-true clauses; a
settled open question is a correction of the row that left it open.

**Step 3 — Is it worth storing?**
  Rebuilding from scratch costs more than storing + recalling?
  - Single-query public facts → No
  - Multi-source synthesis with non-obvious conclusions → Yes
  - User-specific context no search engine can recover → Yes
  → No → STOP.

**What to store**: conclusions and user-specific context, not raw facts.

## Storing what you learn

```bash
memman remember "<fact>" --cat <category> --imp <1-5> --entities "e1,e2" --source agent --session $SESSION_ID
```

Always pass `--session` with your session id — it links the
session's writes into one temporal chain; a write without it joins
no chain.

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

Correct an existing insight by ID:

```bash
memman replace <id> "<new content>"
```

`replace` inherits metadata from the original unless overridden. The
original is superseded, not deleted: `memman insights show <id>
--history` reads the chain back, and `memman supersede <old_id>
<new_id>` links two rows that both already exist.

## Recalling and inspecting

```bash
memman recall "<query>" --brief --limit 20             # smart recall + cross-encoder rerank
memman recall "<keyword>" --basic                      # fast token-only
memman recall "<query>" --limit 20                     # same page, full content
memman insights show <id>                              # read by ID
memman insights by-queue <queue_uuid>                  # what one write stored
```

`--brief` works on both paths. A row left without a summary falls back
to its content instead, so no row comes back blank. `truncated: true`
means the text you got is a raw content prefix cut at 200 characters.
Its ABSENCE does not mean you hold the whole row: a summarized row
carries no marker however much its summary left out, and a fallback
row is marked only when its content ran past the cut. `memman insights
show <id>` is how you read the rest of any row worth more than a scan.

A brief row carries `created_at`, so a WHEN query reconstructs a
timeline by sorting on that field rather than by reading row order,
which is relevance-ordered on every path.

Add `--intent WHY|WHEN|ENTITY` to bias ranking when intent is unambiguous.

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
the floor (0.0 to 2.0, `0.0` = off, rejected with `--basic`). No value
is worth copying; the usable band depends on the embedder and store.
`--basic` returns before ranking, so `--intent` and `--expand` do
nothing there; it lists them in `meta.ignored` rather than obeying
them.

## Forgetting

```bash
memman forget <id>                    # soft-delete
memman insights review                # scan content quality issues
```

Nothing else deletes: the store is uncapped and a stored insight
persists until someone forgets it. Supersession hides without
deleting; `memman unsupersede <id>` brings a superseded row back once
its successor has been forgotten.

## Working with relationships

```bash
memman graph link <src> <tgt> --type semantic --weight 0.85
memman graph link <src> <tgt> --type causal --weight 0.8 \
    --meta '{"sub_type": "causes"}'
memman graph related <id> --depth 2
```

Causal `sub_type` values: `causes` · `enables` · `prevents`.

## Inspecting the system

```bash
memman status                         # insight count, store
memman doctor                         # health check (sqlite, queue, keys, scheduler, env_completeness)
memman log list [--since 7d]          # operation audit log
```

## Guardrails

- Never store secrets, passwords, or tokens.
- Never write to the global store — it is mounted read-only.
- Max 8,000 characters per insight.
- One self-contained fact per `remember` call.
- `--source agent` for the agent's own conclusion, a locator (URL,
  script, dataset pull) for imported material; `user`, the default, is
  for the user's words. Recall's `--source` filter is an exact match on
  that string.
