# 5. Lifecycle & Embedding

[< Back to Design Overview](../DESIGN.md)

---

memman is not append-only, but nothing expires on its own: a stored memory persists until an operator removes it.

## 5.1 Retention

A store is **uncapped** and nothing deletes automatically. Deletion is always an operator action: `memman forget <id>`.

**Rationale.**

- **No count cap.** A memory store's value grows with what it holds, so a capacity limit is the wrong shape for it. The former `MAX_INSIGHTS = 1000` also drove an `auto_prune` that soft-deleted real memories with only an oplog trace, and its predicate (`importance < 4 and access_count < 3`) made the deletions unpredictable rather than gentle: on a store of mostly high-importance rows it pruned nothing and the cap silently failed to bound anything, while on a store of low-importance rows it deleted freely.
- **No retention score either.** A stored `effective_importance` once combined base importance, access frequency, a 30-day half-life and edge count into one number, and an `importance >= 4 or access_count >= 3` predicate exempted rows from the report it fed. Nothing read the column: both backends recomputed the score on every call and wrote the result back, and no query filtered or ordered by it. The report ranked ascending, and its edge term capped at five edges, so rows carrying dozens of edges scored identically to a row carrying five and the report nominated the best-connected rows in the store for deletion. The column, the predicate, the `insights candidates` report and the `insights protect` command that existed to keep a row off it are all gone.
- **Supersession keeps content.** `replace` and the reconciler's UPDATE and SUPERSEDE never delete: the corrected row keeps its content behind `superseded_by`, leaves recall and every listing, and its edges move to the successor. A fact that contradicts several rows supersedes each of them. `memman supersede <old> <new>` links two rows that both already exist; `memman unsupersede <id>` reverses a link once the successor is forgotten; `memman insights show <id> --history` walks the chain. Deletion stays operator-only.
- **Scan cost is not a reason to cap.** Bounded recall latency is the storage layer's problem, not the operator's; see [04-pipelines.md § Smart recall](04-pipelines.md).
- **`MAX_OPLOG_ENTRIES = 5000`**: the oplog is an audit trail, not memory, and a bounded trail is the point. Roughly five operations per insight at the scale where the value was chosen; retained without unbounded growth.
- **`access_count` records retrievals and nothing else.** Recall increments it once per returned row. No write path and no operator command touches it, so it stays a faithful count of what recall returned -- which is also the limit of what it can answer, since a returned row is not a used one. Its reader is `never_accessed` in `memman log list --stats`. `last_accessed_at` moves with the same increment and has no runtime reader; the stale-serve measurement reads it (a superseded row whose last access falls after its successor's `created_at` was served after its correction existed), and it goes at the next schema-touching release unless that measurement becomes a standing check.

## 5.2 Insights group

Manual inspection lives under the `memman insights` group:

```bash
# Read a single insight by ID (a superseded row shows its successor)
memman insights show <id>

# Walk the supersession chain through an id, oldest first
memman insights show <id> --history

# Resolve a write to the insights it produced
memman insights by-queue <queue_uuid>

# Review stored insights for content quality issues
memman insights review
```

`insights review` scans all active insights against transient content patterns (AWS instance IDs, resource counts, verification receipts, deployment receipts, state observations, line number references) and returns flagged entries sorted by warning count. Since the remember pipeline rejects content with 2+ quality warnings at write time, `insights review` primarily catches insights stored before the hard gate was introduced, or single-warning content that accumulated additional transient characteristics over time.

---

## 5.3 Embedding support

Embeddings power semantic search and graph connectivity. Vector dimensionality is provider-defined and recorded in a per-store `meta.embed_fingerprint` (provider, model, dim). Switching a store's embedder is explicit — online via `memman embed swap` (resumable shadow-column backfill) or offline via `memman embed reembed`.

**Per-store embedder sovereignty.** Each store's stored fingerprint is the runtime authority over which embedder client serves that store. Every consumer — drain worker (`_StoreContext`), recall (`bound_embedder(backend)` → the query embedding), graph rebuild, `run_remember(ec=...)` — binds via `embed.fingerprint.bound_embedder(backend)`, which resolves `meta.embed_fingerprint` and dispatches to `embed.registry.get_for(provider, model)`. One process can sequentially open two stores fingerprinted to different providers without env mutation. The operator-facing worked example lives in [USAGE.md § Embedding Operations](../USAGE.md#embedding-operations).

`MEMMAN_EMBED_PROVIDER`'s runtime role narrows to two cases:

1. **Seeding a fresh store** — when a store has no stored fingerprint yet, `seed_if_fresh(backend, get_client())` writes the env-active client's fingerprint into `meta.embed_fingerprint`. After that write, the env var no longer drives runtime selection for that store.
2. **Carrying credentials** — providers read `MEMMAN_VOYAGE_API_KEY`, `MEMMAN_OPENAI_EMBED_API_KEY`, etc. from the env file. A store fingerprinted to a provider whose credentials are absent fails at the embed call site (recall warns and degrades to keyword-only; drain marks the row failed via `EmbedCredentialError`).

`memman embed status` reports the store's stored fingerprint and whether credentials for that fingerprint's provider are available. `memman doctor` (`check_embed_fingerprint`) follows the same shape: pass on stored + creds-available, fail on stored-but-missing-creds, fail on populated-store-without-fingerprint (corruption).

### 5.3.1 Supported providers

| Provider     | Default model             | Notes                                                                                                                                    |
| ------------ | ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `voyage`     | `voyage-3-lite` (512-dim) | Requires `MEMMAN_VOYAGE_API_KEY`. `AUTO_SEMANTIC_THRESHOLD` auto-resolved per `(provider, model, surface)` -- see `embed/thresholds.py`. |
| `openai`     | `text-embedding-3-small`  | Requires `MEMMAN_OPENAI_EMBED_API_KEY` + `MEMMAN_OPENAI_EMBED_ENDPOINT`. Any OpenAI-compatible endpoint (vLLM, LiteLLM, ...).            |
| `openrouter` | `baai/bge-m3`             | Reuses `MEMMAN_OPENROUTER_API_KEY` + `MEMMAN_OPENROUTER_ENDPOINT`; no separate secret needed.                                            |
| `ollama`     | `nomic-embed-text`        | Local Ollama at `MEMMAN_OLLAMA_HOST` (default `http://localhost:11434`).                                                                 |

The wizard ships defaulted to `voyage`; any of the four is selectable.

### 5.3.1a Calibrated embedding models

The shipped `_thresholds_generated.py` covers these `(provider, model)` pairs with surface-specific `AUTO_SEMANTIC_THRESHOLD` values. A store using one of these pairs gets the calibrated threshold automatically based on its `MEMMAN_SURFACE_<store>` setting (default `code`).

| Provider     | Model                                     | code  | claw  |
| ------------ | ----------------------------------------- | -----: | -----: |
| `voyage`     | `voyage-3-lite`                           | 0.645 | 0.497 |
| `voyage`     | `voyage-3`                                | 0.591 | 0.524 |
| `voyage`     | `voyage-3-large`                          | 0.777 | 0.681 |
| `voyage`     | `voyage-code-3`                           | 0.790 | 0.795 |
| `voyage`     | `voyage-finance-2`                        | 0.654 | 0.688 |
| `voyage`     | `voyage-law-2`                            | 0.625 | 0.611 |
| `voyage`     | `voyage-multilingual-2`                   | 0.602 | 0.680 |
| `openrouter` | `openai/text-embedding-3-small`           | 0.630 | 0.613 |
| `openrouter` | `openai/text-embedding-3-large`           | 0.625 | 0.636 |
| `openrouter` | `baai/bge-m3`                             | 0.662 | 0.687 |
| `openrouter` | `baai/bge-large-en-v1.5`                  | 0.738 | 0.712 |
| `openrouter` | `intfloat/e5-large-v2`                    | 0.856 | 0.845 |
| `openrouter` | `intfloat/multilingual-e5-large`          | 0.967 | 0.962 |
| `openrouter` | `qwen/qwen3-embedding-8b`                 | 0.639 | 0.783 |
| `openrouter` | `sentence-transformers/all-MiniLM-L6-v2`  | 0.569 | 0.591 |
| `openrouter` | `sentence-transformers/all-mpnet-base-v2` | 0.610 | 0.669 |
| `ollama`     | `nomic-embed-text`                        | 0.738 | 0.744 |
| `ollama`     | `mxbai-embed-large`                       | 0.748 | 0.700 |
| `ollama`     | `all-minilm`                              | 0.558 | 0.568 |
| `ollama`     | `snowflake-arctic-embed`                  | 0.704 | 0.752 |

### 5.3.1b Models not on the calibrated list

A store fingerprinted to a `(provider, model)` triple outside the table above falls back to the **surface-wide median** of the calibrated values for that surface — empirically the lowest-error single-constant rule (mean nDCG@5 loss ~0.014 vs the calibrated optimum on the shipped triples, max ~0.08).

| Surface | Fallback threshold |
| ------- | ------------------: |
| `code`  | 0.6495             |
| `claw`  | 0.6840             |

`memman doctor`'s `embed_threshold` check warns when an uncalibrated triple is in use and reports `source: 'surface_median'` plus the fallback value in the detail payload.

Operators with a quality-critical store can override the fallback by setting `MEMMAN_AUTO_SEMANTIC_THRESHOLD_<store>` in `~/.memman/env`. Accepted values:

- A float in `(0.0, 1.0)`: an explicit cosine cutoff for this store.
- The sentinel `skip` (also accepted: `none`): disable semantic-edge creation for this store regardless of the model.

`memman config set MEMMAN_AUTO_SEMANTIC_THRESHOLD_<store> 0.72` (or `skip`) is the supported way to write it. The override takes precedence over both the calibrated table and the median fallback.

### 5.3.2 Vector storage

Vector serialization depends on the active storage backend for the store (`MEMMAN_BACKEND_<store>`, falling back to `MEMMAN_DEFAULT_BACKEND`):

- **SQLite** — little-endian float64 BLOB stored in `insights.embedding`; bytes per row = 8 × provider dim (e.g., a 512-dim model writes 4096 bytes).
- **Postgres** — `pgvector` `vector(N)` typed column, persisted as float32 (HNSW-indexed). The migrate path (`PostgresMigrator` in `src/memman/store/postgres.py`) casts SQLite float64 BLOBs to `numpy.float32` before binding to avoid silent rounding by psycopg.

> **Threshold resolution.** `AUTO_SEMANTIC_THRESHOLD` is resolved at runtime in this precedence order: (1) per-store env override `MEMMAN_AUTO_SEMANTIC_THRESHOLD_<store>`; (2) calibrated table lookup `(provider, model, surface)` via `memman.embed.thresholds.resolve`; (3) surface-wide median fallback via `thresholds.resolve_with_fallback`. Surface is a closed set `{'code', 'claw'}` resolved per store via `MEMMAN_SURFACE_<store>` (default `'code'`). The fallback path always returns a usable float, so uncalibrated triples still produce semantic edges -- just at a bounded-but-not-optimal threshold; `memman doctor`'s `embed_threshold` check reports the `source` (`calibrated`, `surface_median`, `override`, or `override_skip`). **After upgrading memman, run `memman graph rebuild` for each store** to recompute semantic edges at the active per-surface threshold for its `meta.embed_fingerprint`. Without rebuild, only new insights flowing through `link_pending` get the corrected threshold; existing edges stay at their built-time value.

### 5.3.3 Embedding in the pipeline

- **Initial (remember — sequential)**: each fact is embedded immediately after extraction.
- **Merged (remember — sequential)**: if reconciliation merges facts, the merged text is re-embedded.
- **Enriched (remember — parallel)**: after LLM enrichment extracts keywords, the insight is re-embedded with enriched text (content + keywords).
- **Recovery (`graph rebuild`)**: re-enriches all insights through the full LLM pipeline and updates embeddings.
- **Recall**: expanded query is embedded for vector search anchors and reranking.

### 5.3.4 Recovery

`memman graph rebuild` re-enriches all insights through the full LLM pipeline and updates embeddings. The worker owns the embedding lifecycle (initial, merged, enriched, rebuild).

### 5.3.5 Online embedding swap

`memman embed swap` performs a per-store provider/model change without going recall-only. The orchestrator (`src/memman/embed/swap.py`) drives a state machine recorded in per-store meta keys:

```
(idle)  ──swap──▶  backfilling  ──last batch──▶  cutover  ──commit──▶  (idle)
                       ▲   │                          │
                       │   └── --resume               │
                       │                              ▼
                       └────── (continues)         (--abort)
```

| State         | Meaning                                                                                                                                                                                                                 |
| ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `backfilling` | Each batch embeds the next `MEMMAN_EMBED_SWAP_BATCH_SIZE` rows under the new provider into a shadow column. Recall keeps using the live column. The cursor advances per-batch so a crash resumes from where it stopped. |
| `cutover`     | Set immediately before the atomic cutover transaction. See "Cutover details" below.                                                                                                                                     |
| (cleared)     | All `embed_swap_*` meta keys absent; `embed status` shows the new fingerprint.                                                                                                                                          |

**Cutover details.** Postgres uses `CREATE INDEX CONCURRENTLY` (timeout `MEMMAN_EMBED_SWAP_INDEX_TIMEOUT`, default unlimited). SQLite copies the shadow column over the live column. The new fingerprint is written and the swap meta keys are **deleted** — absence is the canonical "no swap in flight" signal, not zeroed sentinel values.

`--abort` drops `embedding_pending` (and any uncommitted side column) and clears the swap meta. `memman doctor`'s `no_stale_swap_meta` check warns if any `embed_swap_*` key remains on a store that is not actively swapping.

`embed reembed` is the offline alternative: it rewrites every store in place with the active provider, requires `memman scheduler stop` first, and is intended for one-shot rewrites (not provider migrations).
