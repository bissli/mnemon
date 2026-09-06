# memman — Usage & Reference

## Global flags

Available on every command:

| Flag                | Default     | Description                                                   |
| ------------------- | ----------- | ------------------------------------------------------------- |
| `--store <name>`    | (auto)      | Named memory store (overrides `MEMMAN_STORE` and active file) |
| `--data-dir <path>` | `~/.memman` | Base data directory                                           |
| `--verbose` / `-v`  | `false`     | INFO-level logging to stderr                                  |
| `--debug`           | `false`     | DEBUG-level logging to stderr (overrides `--verbose`)         |
| `--version`         |             | Print version and exit                                        |

---

## Install / Uninstall

Deploy memman into LLM CLI environments. Run after `pipx install memman` (or `pipx install -e .` for development).

```bash
# Interactive: detect environments and install
memman install

# Non-interactive: specific target only
memman install --target claude-code
memman install --target openclaw

# Remove memman integrations
memman uninstall
memman uninstall --target claude-code
```

| Command            | `--target <name>` | Effect                                                                                                                                                                                                                                                                                      |
| ------------------ | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memman install`   | (auto-detect)     | Deploy hook and skill symlinks, register in settings.json, install the scheduler unit, create `~/.memman/logs/` for scheduler output                                                                                                                                                        |
| `memman install`   | `claude-code`     | Install into `~/.claude/` only                                                                                                                                                                                                                                                              |
| `memman install`   | `openclaw`        | Install into `~/.openclaw/` only                                                                                                                                                                                                                                                            |
| `memman install`   | `nanoclaw`        | Install into `~/.nanoclaw/` only                                                                                                                                                                                                                                                            |
| `memman uninstall` | (auto-detect)     | Remove hooks, skill, settings.json entries, and scheduler unit. Strips secret keys (`MEMMAN_LLM_API_KEY`, `MEMMAN_OPENROUTER_API_KEY`, `MEMMAN_VOYAGE_API_KEY`, `MEMMAN_OPENAI_EMBED_API_KEY`) from `~/.memman/env` but keeps non-secret settings; memory store, queue, and logs untouched. |
| `memman uninstall` | `<name>`          | Remove memman from that environment only                                                                                                                                                                                                                                                    |

Two live-read commands (called by hooks, not by hand):

| Command        | What it prints                                                                                     |
| -------------- | -------------------------------------------------------------------------------------------------- |
| `memman guide` | Shipped `guide.md` (hidden, called by openclaw bootstrap; humans read `guide.md` from the package) |
| `memman prime` | Reads SessionStart JSON on stdin; emits status + compact-recall hint + guide (called by prime.sh)  |

---

## CLI Commands

### Core

```bash
# Remember — store a new insight (LLM reconciliation: duplicates skipped, conflicts resolved)
memman remember "Chose Qdrant over Milvus for vector search" \
  --cat decision --imp 5 --entities "Qdrant,Milvus" --source agent

# Skip LLM reconciliation (direct insert)
memman remember "Raw note" --no-reconcile

# Recall — intent-aware graph-enhanced retrieval (default)
memman recall "vector database" --limit 10

# Recall with explicit intent override
memman recall "why did we choose Qdrant" --intent WHY

# Recall with category/source filter (fills to --limit: the filter
# runs inside the anchor scans, not as a post-cut)
memman recall "auth" --cat decision --source agent

# Simple SQL LIKE matching (faster, no graph traversal, no LLM expansion)
memman recall "auth" --basic

# Replace — deterministic replacement by ID (inherits metadata from
# original); the replaced row is superseded, never deleted
memman replace <id> "Updated content" --cat decision --imp 5

# Link two insights that both already exist as predecessor and successor
memman supersede <old_id> <new_id>

# Reverse a supersession once the successor has been forgotten
memman unsupersede <old_id>

# Forget — soft-delete an insight
memman forget <id>
```

**Remember flags:**

| Flag             | Default   | Description                                                                                                                       |
| ---------------- | --------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `--cat`          | `fact`    | Category: `preference`, `decision`, `fact`, `insight`, `context`                                                                  |
| `--imp`          | `3`       | Importance 1-5, a sort key stored as passed                                                                                       |
| `--entities`     |           | Comma-separated entities (merged with LLM-extracted)                                                                              |
| `--source`       | `user`    | Source: `user` (default), `agent`, or a locator for imported material; stored verbatim; recall filters by exact match             |
| `--session`      | (env)     | Session id for the temporal chain; defaults to `$MEMMAN_SESSION_ID`, then `$CLAUDE_CODE_SESSION_ID`. No session, no backbone edge |
| `--no-reconcile` | `false`   | Store the text verbatim: skip extraction and reconciliation, so the write cannot be dropped or folded away                        |

**Recall flags:**

| Flag          | Default       | Description                                                                                                                              |
| ------------- | ------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `--limit`     | `10`          | Max results                                                                                                                              |
| `--intent`    | (auto-detect) | Override intent: `WHY`, `WHEN`, `ENTITY`, `GENERAL`; validated always, but inert under `--basic` and named in `meta.ignored`             |
| `--cat`       |               | Filter by category                                                                                                                       |
| `--source`    |               | Filter by source                                                                                                                         |
| `--basic`     | `false`       | Use simple SQL LIKE matching instead of smart recall; returns before ranking so it carries no `score` or `signals`, and names `--intent` / `--expand` in `meta.ignored` when passed |
| `--brief`     | `false`       | Cut each result to id, category, importance, created_at, summary                                                                         |
| `--session`   |               | Calling session id, recorded on the `recall-detail` oplog row so a return is attributable to a session                                    |
| `--expand`    | `false`       | Opt-in LLM query expansion (synonyms + intent hint); inert under `--basic` and named in `meta.ignored`                                   |
| `--min-score` | `0.0`         | Relevance floor on keyword+similarity, 0.0 to 2.0 (`0.0` = off); rejected with `--basic`                                                 |

The cross-encoder rerank stage is on by default and auto-skips on 1-2 token
queries. Provider is selected via `MEMMAN_RERANK_PROVIDER` (any registered
rerank provider; ships defaulted to `voyage` / `rerank-3-lite`). Toggle
per-store with `memman config set MEMMAN_RERANK_ENABLED_<store> false` or
globally with `memman config set MEMMAN_RERANK_ENABLED false`.

**Telling a weak result set from a strong one.** Smart recall returns
rows even when nothing matches: a recency channel seeds the newest
insights as traversal anchors regardless, so a query that matches
nothing still comes back full. A full page is therefore not evidence
that anything on it is relevant, and a page that looks thin usually is
not: a store nearly always holds something bearing on a query drawn
from the same work. An empty `results` means the store itself is
empty, not that the query failed.

There is no flag for this, deliberately. Every returned row carries
its own `score` and its per-channel `signals` (keyword, similarity,
graph), and those are what a caller judges on - compared WITHIN one
response, never against a fixed number, because the scale belongs to
whichever reranker is configured and changes when the model does. A
boolean computed from a threshold would freeze one model's scale into
the response.

Rows come back in relevance order at every `--limit`, so the first `n`
rows of a page of `m` are exactly what a page of `n` returns. Nothing
re-sorts after the limit cut. On `WHY`, `meta.causal_edges` carries
the `[cause, effect]` pairs among the returned rows, cause first; an
empty list means those rows carry no causal relation to each other.
On `WHEN`, sort on each row's `created_at`, which `--brief` also
carries.

If a query returns nothing that bears on it, the likeliest cause is
vocabulary: re-ask in the store's own words before concluding the
store does not hold it.

`--min-score` is the filter. It drops rows whose keyword plus
similarity sum falls under the floor, so the range is 0.0 to 2.0 and
`0.0` means off. It reads those two signals rather than the ranked
`score` on purpose: the graph signal is min-max normalized, so the top
row of any query scores 1.0 there and a floor on the blended score
would shift with the intent's graph weight. Keep it off unless you
would rather have nothing than something unrelated, because the deep
tail of a recall is often where the useful row sits. There is no
canonical value to copy: the usable band is a property of the embedder
and the store, so find it by running the same query with and without a
floor and watching what leaves.

### Graph operations

```bash
# Link — create a typed edge
memman graph link <source_id> <target_id> --type semantic --weight 0.85
memman graph link <source_id> <target_id> --type causal --weight 0.8 \
  --meta '{"sub_type":"causes","reason":"..."}'

# Related — BFS traversal from an insight
memman graph related <id> --edge causal --depth 2

# Rebuild — full LLM re-enrichment + re-embed + edge rebuild
memman graph rebuild              # process all insights
memman graph rebuild --dry-run    # preview count without modifying DB
memman graph rebuild --stale-only # re-enrich only rows whose prompt_version
                                  # no longer matches the active enrichment key
```

Auto-reindex of computed edges (semantic, entity, temporal) fires on `open_db()` when graph constants have changed; no operator command for it.

`graph rebuild` re-enriches all insights through the full LLM pipeline (enrichment, re-embedding, causal inference, edge recreation). Processes in batches of 20. Returns `{"processed": N, "remaining": 0}`. Rejected when the scheduler is stopped.

`--stale-only` is the targeted variant: it only touches rows whose persisted `prompt_version` no longer matches `compute_prompt_version()` -- the enrichment prompt, the causal prompt, and the `slow_metadata` model, which is exactly the set this command replays. A `model_id` difference is deliberately NOT staleness: that column records the model behind the row's content, which no rebuild rewrites, so reporting it would nag forever with no remedy. Cross-backend (works on Postgres, unlike wholesale `graph rebuild` which remains SQLite-only). Shares the `'rebuild'` advisory lock so it cannot race a wholesale rebuild. NULL-provenance rows are not swept; they need a separate backfill.

### Insights lifecycle

```bash
# Read a single insight by ID (full content + metadata; a superseded
# row shows its successor under `superseded_by`)
memman insights show <id>

# Walk the supersession chain through an id, oldest first
memman insights show <id> --history

# Resolve a write to the insights it produced (key from remember/replace)
memman insights by-queue <queue_uuid>

# Scan stored insights for content quality issues
memman insights review
```

To delete an insight, use `memman forget <id>`. A `replace`, a
reconcile merge, or `memman supersede` never deletes: the corrected
row is superseded, keeps its content, and leaves recall and every
listing. Nothing deletes on its own: the store is uncapped and carries
no retention score, so a stored insight persists until an operator
removes it.

### Embedding operations

```bash
# Show this store's bound fingerprint and whether its provider's
# credentials are available in this process
memman embed status

# Online provider/model swap (resumable shadow-column backfill, atomic cutover)
memman embed swap --to voyage-3-large
memman embed swap --to text-embedding-3-small --provider openai
memman embed swap --resume                     # continue an in-flight swap
memman embed swap --abort                      # discard an in-flight swap

# Offline full re-embed under the current provider (rejected when scheduler is running)
memman embed reembed
memman embed reembed --dry-run                 # preview count without modifying DB
```

Two switching paths:

- **`embed swap`** is the online path. It populates `embedding_pending` (shadow column on SQLite, side column on Postgres) under the active provider while the existing column keeps serving recall, then commits an atomic cutover transaction. State machine: `backfilling → cutover → done`. Resumable via `--resume`; abortable via `--abort`. Per-store; the in-flight target is recorded in `meta.embed_swap_*` keys (deleted on completion).
- **`embed reembed`** is the offline path: every store is rewritten in place with the current `MEMMAN_EMBED_PROVIDER`. Requires the scheduler to be **stopped** (`memman scheduler stop`).

**Per-store embedder sovereignty.** Each store's `meta.embed_fingerprint` is the runtime authority over its embedder. Recall, drain, and graph rebuild all bind the embedder from the store's fingerprint, not from `MEMMAN_EMBED_PROVIDER`. One process can sequentially open two stores fingerprinted to different providers without env mutation — e.g., `MEMMAN_EMBED_PROVIDER=voyage memman --store openai_store recall ...` succeeds against an OpenAI-fingerprinted store. Switching a store's embedder is explicit (`embed swap` or `embed reembed`); there is no silent migration. Implementation details: [05-lifecycle.md § 5.3](design/05-lifecycle.md#53-embedding-support).

#### Using an embedding model not on the calibrated list

The shipped `_thresholds_generated.py` covers 20 `(provider, model)` pairs across `voyage`, `openrouter`, and `ollama` (see [05-lifecycle.md § 5.3.1a](design/05-lifecycle.md#531a-calibrated-embedding-models)). A store bound to a model outside that list falls back to the surface-wide median (`code` = 0.6495, `claw` = 0.6840) and `memman doctor` reports `embed_threshold: warn` with `source: surface_median`. Semantic edges still get created — the fallback is bounded (mean nDCG@5 loss ~0.014 vs calibrated on the shipped triples).

Operators with a quality-critical store running an uncalibrated model can set a per-store override:

```bash
# Set an explicit cosine cutoff (float in (0.0, 1.0))
memman config set MEMMAN_AUTO_SEMANTIC_THRESHOLD_<store> 0.72

# Or disable semantic-edge creation entirely for this store
memman config set MEMMAN_AUTO_SEMANTIC_THRESHOLD_<store> skip

# Inspect the active source for a store
memman doctor               # embed_threshold detail shows source
```

The override takes precedence over both the calibrated table and the median fallback. Doctor validates the value: numeric must be in `(0.0, 1.0)`; sentinels `skip` and `none` disable edges; anything else fails the check.

**Upgrading from a version with a single shared `AUTO_SEMANTIC_THRESHOLD`.** Existing stores keep working after the upgrade (no schema migration). Newly remembered insights are linked at the per-fingerprint calibrated threshold, but pre-existing semantic edges keep whatever weights they were created with — so two insights inserted before the upgrade may show a different edge population than two near-identical insights inserted after. To rebalance the whole store at the current threshold, run `memman graph rebuild <store>`. This is optional; existing edges remain valid.



### Store management

memman supports named stores for data isolation. Each store has its own database.

```bash
# List all stores (* marks the active one)
memman store list

# Create a new store
memman store create work

# Switch the default active store
memman store use work

# Remove a store (cannot remove the active store)
memman store remove old-project
```

**Store resolution priority** (highest to lowest):

1. `--store <name>` CLI flag
2. `MEMMAN_STORE` environment variable
3. `~/.memman/active` file
4. Falls back to `"default"`

**Per-directory automatic switching.** `MEMMAN_STORE` is read from `os.environ`, so any tool that scopes env vars to a working directory will flip the active store on `cd`. Four mechanisms:

| Mechanism                | Setup                                               | Scope                                                       |
| ------------------------ | --------------------------------------------------- | ----------------------------------------------------------- |
| `direnv` (recommended)   | `.envrc` in the project: `export MEMMAN_STORE=work` | Every shell, agent, and subprocess started in the directory |
| `--store <name>` flag    | Pass `--store work` on every invocation             | One command; explicit, survives a missing env               |
| Project `CLAUDE.md` rule | Instruct the agent to pass `--store work`           | Claude Code sessions only; not honored by terminal callers  |
| `memman store use work`  | Set the global `~/.memman/active` file              | Persistent and global; last `use` wins everywhere           |

Do not set `MEMMAN_DATA_DIR` per directory. The scheduler unit is installed once against `~/.memman/queue.db`; a per-directory data dir creates an isolated queue that the host scheduler never drains. Use a named store instead and let the worker dispatch per row.

#### Migrating between SQLite and Postgres

`memman migrate` is symmetric: `--to postgres` (default) copies a store from SQLite into Postgres; `--to sqlite` copies it back. Both directions hold the shared `drain.lock` so a scheduler-fired drain cannot race.

| Direction       | Source                                                       | Destination                 | Backend flag flipped to           |
| --------------- | ------------------------------------------------------------ | --------------------------- | --------------------------------- |
| `--to postgres` | SQLite store (preserved)                                     | `store_<name>` schema in PG | `MEMMAN_BACKEND_<store>=postgres` |
| `--to sqlite`   | Postgres `store_<name>` (dumped to `archive/`, then dropped) | Fresh SQLite store          | `MEMMAN_BACKEND_<store>=sqlite`   |

The command echoes a plan (source paths, redacted destination DSN, per-store target schema state — `ABSENT` / `EMPTY` / `POPULATED`) and prompts for confirmation. Stores already on the target backend emit a warning and are skipped (idempotent). `--dry-run` is supported only with `--to postgres`.

```bash
# Forward (default): SQLite -> Postgres, dry-run plan only
memman migrate --store work --dry-run

# Forward (default): SQLite -> Postgres, interactive
memman migrate --store work

# Reverse: Postgres -> SQLite (no --dry-run); preserves a dump under archive/
memman migrate --store work --to sqlite

# Non-interactive (CI / scripts): skip the prompt
memman migrate --all --yes
```

To revert a single store without re-migrating data, set the backend flag directly: `memman config set MEMMAN_BACKEND_<store> sqlite` (or unset the key to fall back to `MEMMAN_DEFAULT_BACKEND`). To verify the cutover, run `memman doctor`.

### Observability

```bash
memman status                                       # memory statistics; JSON includes stale_insights count
memman doctor                                       # health checks (integrity, schema, partial_index_predicates, enrichment, embeddings, fingerprint, orphan and dangling edges, supersession_integrity, queue, scheduler, drain heartbeat, env, no_stale_swap_meta, provenance_drift)
memman doctor --text                                # human-readable colored table
memman config show                                  # effective configuration (env + on-disk)

memman log list                                     # operation audit log (default JSON, last 20)
memman log list --limit 50                          # show more entries
memman log list --since 7d                          # entries from last 7 days
memman log list --since 7d --stats                  # grouped counts + never-accessed
memman log list --text                              # human-readable text table

memman log worker [--errors] [--lines N]            # tail worker stdout/stderr (~/.memman/logs/enrich.{log,err})
memman log worker --stack [--lines N]               # tail the rotated log + backups (<data-dir>/logs/memman.log); excludes --errors
```

### Scheduler

```bash
memman scheduler status [--text]         # platform, interval, state, next run, last heartbeat, log paths (default JSON)
memman scheduler start [--text]          # flip persistent state to STARTED (resume drains + writes)
memman scheduler stop [--text]           # flip persistent state to STOPPED (pause drains + reject writes)
memman scheduler trigger                 # dispatch a drain, do not wait for it (systemd/launchd; not applicable in serve mode)
memman scheduler interval --seconds N    # change cadence (60s minimum on systemd/launchd)
memman scheduler install                 # install the scheduler unit (idempotent)
memman scheduler uninstall               # remove the scheduler unit; preserves persistent state
memman scheduler serve --interval N      # long-running drain loop (used as PID 1 in containers)
memman scheduler debug on|off|status     # toggle the verbose worker trace log

memman scheduler queue list [--limit N]  # peek pending rows
memman scheduler queue failed [--limit N]# rows in 'failed' state
memman scheduler queue skipped [--limit N]# drained writes that stored no insight
memman scheduler queue show <row_id>     # full payload + trace events for one row
memman scheduler queue retry <row_id>    # requeue a single failed row
memman scheduler queue retry --all-stale # requeue every row currently in status='stale'
memman scheduler queue purge --done      # delete rows where status='done'
memman scheduler queue purge --stale     # delete rows where status='stale'
memman scheduler queue purge --skipped   # empty the skipped-write ledger
```

A skipped write is a row the pipeline completed without storing an insight: its extraction came back empty, or every extracted fact restated an existing insight and was folded into it as a corroboration. All of these mark the row `done`, and `purge_done` deletes it a minute later, so the `skipped_writes` ledger is what survives. It keeps the full content, the reason, the store, and the session id, and `stats` reports its size under `skipped` (alongside `stale`, which it also reports). The rule is all-or-nothing: a write that stored even one fact is not filed.

Nothing prunes the ledger on a timer: `purge_done` never reaches it. `queue purge --skipped` empties it, `store remove` drops one store's entries, and a `backup restore` replaces it wholesale with the archive's copy. The listing spans every store, and it holds raw content, so treat it as sensitive. Pass `--no-reconcile` on `remember` to bypass every drop and store the text verbatim.

A stale row is a pending entry claimed more than `STALE_CLAIM_SECONDS` ago (default 600 s), usually from a mid-drain worker crash. The post-drain maintenance pass auto-recovers via `queue.retry_stale` alongside `purge_done` and `purge_worker_runs`; the explicit verbs exist for incident response.

When the scheduler is stopped, memman is recall-only: every write exits 1 with `Scheduler is stopped; cannot <verb>`. The `serve` loop polls the state file every iteration, so pause is observed within seconds even mid-drain.

### Backup

`memman backup` snapshots every store to an **external, durable directory** (e.g. a Dropbox path) on a cron schedule, rotates old bundles, and can rebuild a working store after total loss of `~/.memman/`. Bundles are written **only** to the target directory, never into `~/.memman/` (which is per-host and disposable, so an in-place archive dies with it). Snapshots are online and non-disruptive — the enrichment worker keeps draining (SQLite via the `sqlite3` online-backup API, Postgres via `pg_dump -Fc`), so no scheduler stop is needed.

```bash
memman backup run [TARGET]                        # build one bundle now (TARGET or MEMMAN_BACKUP_TARGET)
memman backup schedule '<cron>' TARGET [--keep N] # install a scheduled backup (cron -> native scheduler)
memman backup unschedule                          # remove the scheduled backup trigger (keeps env config)
memman backup list [TARGET]                        # list bundles at TARGET (read from sidecar manifests)
memman backup status                               # cron, target, keep, last fire, next run, latest bundle
memman backup restore BUNDLE [--yes]               # rebuild stores + non-secret config from a bundle
```

The cron string is a 5-field expression (`min hour dom month dow`, interpreted in local time) and is translated to the host's native scheduler at install time: systemd `OnCalendar=` (+`Persistent=true` for sleep/power-off catch-up), launchd `StartCalendarInterval`, or an in-process matcher in `serve` mode. The target directory is created if it does not exist. Retention keeps the newest `MEMMAN_BACKUP_KEEP` bundles (default 7).

Each bundle is one atomic `.tar.gz` plus an uncompressed sidecar `<bundle>.manifest.json` (for cheap `list`). A failing store is recorded with `status="failed"` in the manifest and never aborts the whole bundle. The global write queue (`queue.db`) is snapshotted too — and before the store DBs — so a `remember` that has not yet drained into its store is never lost: it rides in the bundle and drains on the restored host (the manifest records `queue_pending`). In `serve` mode the loop also drains the queue to empty before snapshotting, so the bundle is settled when possible.

**Secrets are excluded.** API keys, the default Postgres DSN, and every per-store `MEMMAN_POSTGRES_DSN_<store>` are stripped from the bundle's `env.nonsecret` member; per-store backend selection and model/provider/threshold knobs are kept. On `restore`, the non-secret config is merged first (so per-store backend routing is in place), each store is written by its manifest `backend` (SQLite file copy, or `pg_restore` resolving the DSN on the target host), and the active-store pointer is restored. `restore` holds the shared `drain.lock` and reports `secret_keys_needed` (re-enter these on the host), `pg_restore_skipped` (postgres stores with no DSN configured here), `embed_mismatch` (stores whose fingerprint differs from the restored embedding config), and any `failed` stores.

```bash
# A daily 03:00 backup to a Dropbox archive
memman backup schedule '0 3 * * *' ~/Dropbox/code/archive/
memman backup status                               # confirm next run + installed timer

# Restore after losing ~/.memman (re-enter secrets afterward, per secret_keys_needed)
memman backup restore ~/Dropbox/code/archive/memman-backup-<host>-<stamp>.tar.gz --yes
```

`memman uninstall` tears down the backup timer/agent alongside the enrichment scheduler; the `MEMMAN_BACKUP_*` env keys are kept so a later `memman backup schedule` resurrects the configuration.

---

## Configuration

memman reads config at runtime from one source: `<MEMMAN_DATA_DIR>/env`, a `KEY=VALUE` file at mode 0600 (default `~/.memman/env`). Shell environment variables are not consulted at runtime for installable settings, so a stale shell export cannot override a committed value.

`memman install` performs a one-time pull from the current shell into the env file. Precedence per key: existing file value > wizard prompt (TTY only) > `os.environ` > OpenRouter `/models` resolver (FAST/SLOW only) > `INSTALL_DEFAULTS`. Existing file values are sticky; reinstall never lets a shell export override them.

`memman config set KEY VALUE` is the override path. Use it after install to change a backend, rotate an API key, or update a DSN. Conflicts between an `INSTALLABLE_KEYS` flag and an existing env-file value are rejected with the exact `memman config set ...` command to run.

Process-control variables (`MEMMAN_DATA_DIR`, `MEMMAN_STORE`, `MEMMAN_WORKER`, `MEMMAN_DEBUG`, `MEMMAN_SCHEDULER_KIND`, `MEMMAN_SESSION_ID`) are not persisted to the file; they are read directly from `os.environ` by the components that own them. `MEMMAN_SESSION_ID` in particular is never written to the env file on purpose — a stale persisted session id would fuse every later write into one false temporal chain. `--session` then falls back to `CLAUDE_CODE_SESSION_ID`. Claude Code owns and exports it; memman only reads it, and never persists it either.

The full variable list lives in [CONTRIBUTING.md § Variable reference](../CONTRIBUTING.md#variable-reference).

### Install wizard

Run `memman install` in a TTY to get the interactive wizard. It prompts for the LLM endpoint URL (any OpenAI-compatible endpoint; ships defaulted to `https://openrouter.ai/api/v1`); for OpenRouter endpoints it auto-resolves the three role model slugs (`MEMMAN_LLM_MODEL_FAST` / `_SLOW_CANONICAL` / `_SLOW_METADATA`) against `/v1/models`, for any other endpoint it prompts for each slug interactively. It then prompts (masked input) for `MEMMAN_LLM_API_KEY` (required for non-loopback endpoints; loopback endpoints like Ollama may leave it blank), then for the embedding provider (any registered provider; ships defaulted to `voyage`) and the matching key for that provider (e.g. `MEMMAN_VOYAGE_API_KEY` for voyage, `MEMMAN_OPENAI_EMBED_API_KEY` for openai; openrouter reuses the LLM key). It also offers a backend selector (sqlite/postgres) when the `memman[postgres]` extra is installed; the wizard probes the DSN, verifies the `pgvector` extension, and (for non-localhost DSNs) emits a hint about PgBouncer transaction pooling. Headless installs bypass the wizard:

- `--backend [sqlite|postgres]` — explicit backend choice; required in non-interactive mode if you want anything other than sqlite.
- `--pg-dsn URL` — Postgres DSN; required with `--backend postgres` in non-interactive mode. The DSN may omit the password to use `~/.pgpass`, `PGSERVICE`, or `PGPASSWORD`.
- `--no-wizard` — disables prompts even in a TTY; flags + defaults only.

### Backend selection

memman routes each store through a backend chosen by env-file lookup:

1. `MEMMAN_BACKEND_<store>` — explicit per-store override (e.g., `MEMMAN_BACKEND_work=postgres`).
2. `MEMMAN_DEFAULT_BACKEND` — fallback when no per-store key is set (default `sqlite`).

`memman migrate <store>` writes `MEMMAN_BACKEND_<store>=postgres` so a single store can move to Postgres while others stay on SQLite. Use `memman config set MEMMAN_DEFAULT_BACKEND postgres` only when you want every newly-created store to default to Postgres.

The deferred-write queue is always SQLite at `<data_dir>/queue.db`. The Postgres backend stores per-store data in `store_<name>` schemas, each with its own `worker_runs` heartbeat table.

### Postgres DSN

Standard PostgreSQL libpq URI per psycopg3: `postgresql://[user[:password]@][host][:port]/[dbname][?param=value&...]`.

`memman config set-pg-dsn` walks you through host / port / user / password (masked) / dbname and writes the URI for you (URL-encoding special characters). Pass `--default` for `MEMMAN_DEFAULT_POSTGRES_DSN` or `--store NAME` for `MEMMAN_POSTGRES_DSN_<store>`:

```bash
memman config set-pg-dsn --default       # writes MEMMAN_DEFAULT_POSTGRES_DSN
memman config set-pg-dsn --store work    # writes MEMMAN_POSTGRES_DSN_work
```

Leave the password prompt empty to produce a passwordless DSN that defers to `~/.pgpass` (recommended on shared hosts). The command does not probe connectivity — verify with `memman doctor` or `memman migrate --dry-run`.

| Scenario     | DSN                                                      | Notes                                                  |
| ------------ | -------------------------------------------------------- | ------------------------------------------------------ |
| local dev    | `postgresql://memman@localhost/memman`                   | no password                                            |
| inline creds | `postgresql://memman:s3cret@db.internal:5432/memman`     | URL-encode `: @ /` in the password                     |
| `~/.pgpass`  | `postgresql://memman@db.internal:5432/memman`            | passwordless URL, recommended                          |
| TLS-required | `postgresql://memman@db.internal/memman?sslmode=require` | + any libpq parameter (e.g. `application_name=memman`) |

> **Security.** `MEMMAN_DEFAULT_POSTGRES_DSN` and any `MEMMAN_POSTGRES_DSN_<store>` are stored plaintext in `~/.memman/env` at mode 0600. Root and any process running as your user can read them. For shared hosts, prefer `~/.pgpass` (mode 0600) and a passwordless DSN — psycopg3 sources the password from `~/.pgpass`, `PGSERVICE`, or `PGPASSWORD` automatically.

### Runtime tunables

The variables below are not installable — they are read from the env file on demand by the components that own them, with no install-time seeding:

| Variable                          | Default         | Description                                                                                                           |
| --------------------------------- | --------------- | --------------------------------------------------------------------------------------------------------------------- |
| `MEMMAN_REINDEX_TIMEOUT`          | `180`           | Seconds Postgres reindex (HNSW) is allowed to run before `statement_timeout` aborts; reraised idempotently next call. |
| `MEMMAN_EMBED_SWAP_BATCH_SIZE`    | `200`           | Rows per backfill batch in `memman embed swap`.                                                                       |
| `MEMMAN_EMBED_SWAP_INDEX_TIMEOUT` | `0` (unlimited) | Seconds Postgres `CREATE INDEX CONCURRENTLY` may run during cutover; `0` disables `statement_timeout`.                |

---

## Architecture

### Write pipeline (deferred, two-tier)

`memman remember` appends one row to the queue in ~50 ms on the host session — no LLM calls, no embeddings, no edges. The full pipeline runs out of band:

1. **Tier 1 (host)** — append a row to `~/.memman/queue.db` with `status='pending'`, the raw text, and any `--cat`/`--imp`/`--entities` hints. Returns `{action: queued, queue_id, queue_uuid, store}`. The `queue_uuid` is the join key: it is stamped on every insight this write produces and outlives the queue row, which `purge_done` drops about a minute after the drain.
2. **Tier 2 (worker)** — systemd timer (Linux), launchd agent (macOS), or `memman scheduler serve` PID 1 (containers) invokes `memman scheduler drain --timeout 60` every 60 s under an `flock` on `~/.memman/drain.lock`. Per row: quality gate → LLM fact extraction → per-fact embed + similarity scan → exact-match dedup (a fact byte-identical to exactly one stored row skips the LLM and bumps that row's `corroboration_count`) or LLM reconciliation (ADD/UPDATE/SUPERSEDE/NONE, where `NONE <id>` names the memory that already covers a reworded fact and bumps it the same way, and UPDATE and SUPERSEDE supersede their target rather than delete it) → insert/supersede → fast edges (temporal + entity + semantic) → parallel enrichment + LLM causal inference → re-embed → rebuild auto edges → mark done.

The host session never blocks on the network. Newly stored memories become recallable on the next drain tick (default 60 s).

### Recall pipeline

1. **LLM query expansion** (opt-in via `--expand`) — synonyms and intent detection.
2. **RRF anchor selection** — keyword + vector + recency fused with K=60.
3. **Beam search** — intent-weighted graph traversal from anchors.
4. **3-signal rerank** — keyword, similarity, graph (intent-weighted). A stored entity name reaches the keyword signal because a candidate's token set unions its content tokens with its entity-name tokens.
   - **4a. MMR diversity re-sort** — one-shot re-sort of the top 200; shipped disabled (`MMR_LAMBDA = 1.0`, measured a no-op under the cross-encoder rerank at both placements — see `experiments/recall_ablation/README.md`).
5. **Cross-encoder rerank** (on by default; toggle per-store via `MEMMAN_RERANK_ENABLED_<store>`) — the configured reranker (default `voyage` / `rerank-3-lite`) re-scores the top 100 candidates; replaces the multi-signal score for the final ordering. Auto-skips on 1-2 token queries.
6. **Structure payload** — WHY carries `meta.causal_edges`, the `[cause, effect]` pairs among the returned rows. Nothing re-sorts after the limit cut: rows come back in relevance order on every intent.

Inspired by [MAGMA](https://arxiv.org/abs/2601.03236). See [Design & Architecture](DESIGN.md) for the full deep dive.
