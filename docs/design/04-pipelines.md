# 4. Read & Write Pipelines

[< Back to Design Overview](../DESIGN.md)

---

## 4.1 Write pipeline: remember (deferred, two-tier)

`memman remember` appends one row to the queue in ~50 ms. A user-scope scheduler (systemd timer on Linux, launchd agent on macOS) invokes the hidden worker `memman scheduler drain --timeout 60` every 60 s; the drain runs extraction, reconciliation, and enrichment out of band.

![Remember Pipeline](../diagrams/02-remember-pipeline.drawio.png)

### Tier 1: synchronous queue-append (host session)

1. `memman remember [--cat X --imp Y --entities a,b --source S --session ID] "<text>"` validates input. `--session` (default `$MEMMAN_SESSION_ID`, then `$CLAUDE_CODE_SESSION_ID`) is the temporal chain key; the row also receives a `queue_uuid` minted at enqueue (the idempotency key).
2. Insert one row into the deferred-write queue with `status='pending'`, priority, queued_at, and the raw text + hints. The queue is always `~/.memman/queue.db` (SQLite WAL) regardless of any store's backend choice — it is a process-global write buffer, not per-store state.
3. Return `{action: queued, queue_id: N, queue_uuid: U, store: ...}` to the caller. `queue_id` addresses the queue row and is purged about a minute after the drain; `queue_uuid` is stamped on every insight the write produces, so it is the only handle that survives. `memman insights by-queue <U>` resolves it to those rows.

No LLM calls. No embeddings. No similarity scan. No edges. The host session never blocks.

Every write goes through the queue. When the scheduler is **stopped**, memman is recall-only and writes reject with a fixed error pointing at `memman scheduler start`.

### Tier 2: background worker (scheduler-driven)

`memman scheduler drain --timeout <seconds>` (hidden subcommand; only the trigger invokes it) runs under an environment-native trigger:

- **Linux host**: `systemctl --user` timer at `~/.config/systemd/user/memman-enrich.timer`, `Persistent=true` so sleep/off catch-up is automatic.
- **macOS host**: launchd agent at `~/Library/LaunchAgents/com.memman.enrich.plist` with `StartInterval=60`.
- **nanoclaw container** (no systemd / launchd): `memman scheduler serve --interval 60` runs as PID 1. Set `MEMMAN_SCHEDULER_KIND=serve`. The drain loop polls the state file every iteration so `scheduler stop` is observed within seconds; the loop then exits — in a PID-1 container that exits the container.

Per-blob processing inside `_process_queue_row`:

1. **Atomic claim** — `UPDATE queue SET claimed_at=..., attempts=attempts+1 WHERE id = (SELECT ... WHERE status='pending' ORDER BY priority DESC, queued_at ASC LIMIT 1) RETURNING ...`. The queue is SQLite WAL, so the claim is race-free under the WAL writer guarantee. Stale claims (>10 min) are reclaimable. Drains never overlap: an `fcntl.flock` on `~/.memman/drain.lock` gates `_drain_queue` regardless of which backend the store-under-drain routes to.
2. **Idempotency check** — if the target store already has any insight carrying the row's `queue_uuid` (a uuid4 minted at enqueue), skip and mark done (crash-recovery after partial commit). The uuid — not the integer row id — survives a `backup.restore` that rewinds the queue's AUTOINCREMENT counter; `source` is pure provenance and plays no part.
3. **Quality gate** — regex-based `check_content_quality()` rejects transient patterns.
4. **LLM fact extraction** — extracts exactly one canonical fact with category/entities. An empty extraction is a judgment, not a failure: the row returns `action='skipped'`, `skip_reason='trivial content'`, and no insight, edge, or oplog row is written. `--no-reconcile` bypasses this step entirely (verbatim-store contract, step 5).
5. **Per-fact**: embed via the store's bound provider, keyword + cosine similarity scan, then an **exact-match dedup rung**: when exactly one shortlist row matches the fact byte-for-byte (modulo case and whitespace), the fact skips with no LLM call, the stored row's `corroboration_count` is bumped, and a `reconcile-corroborate` oplog row is written; two identical stored rows escalate to the LLM (the store is already inconsistent, and which row to merge into is exactly the judgment worth a call). Otherwise `reconcile_memories` judges the one fact against every candidate and returns one action per affected memory: SUPERSEDE on each contradicted row, UPDATE on at most one refined row, NONE on the one row that restates the fact, else ADD; one merged text keeps every uncontradicted clause of every named row. The apply step supersedes each target, moves its edges, and inserts one successor; the shortlist itself is written first as a `reconcile-candidates` oplog row (`fact_id`, the fact, and each candidate with its rung and score) so the decision can be replayed against exactly the rows the model saw. UPDATE refines a memory with compatible detail; SUPERSEDE contradicts one, and the merged text keeps only the clauses of the predecessor that are still true. Both supersede the target the same way; SUPERSEDE alone withholds the `corroboration_count` carry, and a SUPERSEDE stored without merged text is marked `(unmerged)` in its oplog row so the rate is measurable. A `NONE` verdict is the second corroboration route: the model names the memory that already captures the fact, the plan carries that id, and the write is skipped with the named row bumped exactly as the rung does it. The prompt's `NONE <id>` line keeps its id demand: without it the model answers NONE with a null target, the parser reads a target-less NONE as ADD, and the restatement is stored as a new row instead of bumping the named one (see 02-concepts.md, Corroboration semantics). The rung does not run under `--no-reconcile` (verbatim-store contract; `replace` routes through that flag). Three refinements bound the bump: the restating row's `queue_uuid` is adopted only when the target carries none (the creating row's replay guard outranks the restating row's); a target no longer current between planning and apply (forgotten, or superseded by an earlier write) degrades the skip to a plain add carrying the already-computed vector (the fact is never dropped, the dead row is never bumped); and one queue row bumps a given target at most once, however many restatements its extraction emits. The skip result names the corroborated row via `target_id`.
6. **Parallel enrichment + causal inference** (ThreadPoolExecutor, 2 workers). LLM-proposed entities and keywords over 200 chars are dropped post-parse (never truncated — a truncated entity is still a valid exact-match edge key), before the count caps and before the merge with user-supplied `--entities`, which stay uncapped. The caps are pathological-input guardrails measured against the fleet (longest legitimate strings: 137 chars), not retrieval tunables, and they live post-parse so `prompt_version` is unaffected.
7. **Re-embed** with enriched keywords; rebuild auto edges.
8. `mark_done(queue_id)` on success, or `mark_failed` (retry up to 5 times across stale-claim windows before status='failed').
9. **Skipped-write ledger**: a row that stored nothing (step 4 returned empty, or every step-5 fact skipped) is filed in the `skipped_writes` sidecar table with its full content, the reason, the store, and the session id, then marked `done` like any other row. The sidecar exists because `skipped` is not a queue status and cannot become one: `_migrate` carries no `alter` migrations, so a widened CHECK constraint is a no-op on every existing database and writing the new status would raise inside the drain's try block, routing the row to `mark_failed`. The rule is all-or-nothing: a row that stored even one fact is not filed. `purge_done` deletes the queue row after 60 s and never reaches the ledger; `memman scheduler queue skipped` reads it back, `queue purge --skipped` empties it, `purge_store` drops one store's entries, and a `backup restore` replaces it wholesale. Nothing prunes it on a timer: a retention window would be a tunable, and memman does not ship estimated tunables.

Edge upserts and embed/LLM call sites no longer swallow exceptions; failures (constraint violation, network error, malformed payload) reach `mark_failed` and consume the retry budget. Best-effort cleanup (HTTP session resets, platform probes, pool teardown) keeps narrow typed catches at `logger.debug`.

### Metadata precedence on a reconcile merge

An UPDATE, SUPERSEDE or REPLACE is not an in-place edit. `_apply_plan` supersedes every target and inserts one **successor** row: each target keeps its content behind `superseded_by` and leaves every active read, so every field of the current view is decided by one of two rules, and a field governed by neither stays only on the predecessor. Every predecessor's pointer is written before the successor is inserted, and that order is load-bearing: the temporal builder reads the session's latest row, so the predecessors must already be out of the active set or the successor chains its backbone to a row it replaced. Two facts in one write that both target the same predecessor supersede it once; the later fact keeps its other targets and lands as a plain add only when every target is taken. The split:

| Field                                | Winner                                               | Why                                                                                           |
| ------------------------------------ | ---------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| `content`                            | incoming (LLM `merged_text`)                         | the merge is what the caller asked for                                                        |
| `category`                           | incoming                                             | the caller's `--cat` pin, else the extractor's per-fact guess                                 |
| `importance`                         | incoming                                             | the caller's `--imp`, stored as passed; the extractor does not set it                         |
| `source`, `session_id`, `queue_uuid` | incoming                                             | provenance names the write that produced this row                                             |
| `entities`                           | **union** of both                                    | the extractor sees only the incoming text, so overwriting would narrow the set on every merge |
| `access_count`                       | **max** of both                                      | earned recall history must survive a rewording                                                |
| `corroboration_count`                | **max** on UPDATE and REPLACE; incoming on SUPERSEDE | restatements of a claim do not corroborate the fact that falsified it                         |
| `superseded_by`                      | predecessor's, set to the successor                  | the link `insights show --history` and `unsupersede` read                                     |
| `created_at`                         | successor's own                                      | server-side default; the row is new                                                           |
| edges                                | **re-pointed** from target to successor              | the target's neighborhood is the graph's value; a bare delete throws it away                  |

Incoming-wins on the provenance triple is deliberate: `--source` is the only exact recall pre-filter, so a merged row belongs to the namespace of the write that last touched it. The consequence worth knowing: a later write that passes no `--source` carries the `user` default, so it moves a merged row out of a narrower namespace an earlier write had set. Scope an investigation with `--source` on **every** write that may merge into it, not only the first.

Edge re-pointing skips any edge whose far endpoint is the target itself or the successor, so a self-edge on the target does not become one on the successor. `upsert` keeps the higher weight, so re-pointing onto an edge the successor already minted is safe.

### Per-stage token accounting

Every `MemmanLLMClient.complete` call names its originating stage from a closed set (`extraction`, `reconciliation`, `query_expansion`, `enrichment`, `causal`, `probe`, plus `harness` for off-pipeline measurement tooling — an unknown stage raises). The client reads the provider's `usage` block **per attempt inside the retry loop** — an empty HTTP-200 body retried twice is three billed completions, so success-only accounting undercounts — and accumulates into a process-wide ledger behind a `threading.Lock` (enrichment and causal run concurrently, so event-order attribution is unrecoverable). The drain worker snapshots the ledger per row (each `queue_done`/`queue_failed` trace event carries the row's per-stage delta) and emits an `llm_usage_summary` trace event plus an `llm_usage` key in the drain's JSON output with the drain-level totals. An HTTP-200 response with no `usage` block counts the call under `missing_usage` without inventing zero tokens; non-2xx attempts land in `http_errors` rather than `calls`, so a retried rate-limit storm cannot inflate the billed-call signal, and an HTTP-200 whose body is not JSON is booked like an empty body and retried.

### LLM routing

Both the session path (`memman recall` query expansion) and the scheduler path go through a single `MemmanLLMClient` that posts to the OpenAI-compatible `/chat/completions` endpoint configured via `MEMMAN_LLM_ENDPOINT` (default `https://openrouter.ai/api/v1`). Switching providers is one env edit — any vendor exposing an OpenAI-compat shim (OpenRouter, OpenAI, Anthropic, Google, Ollama, vLLM, LiteLLM, ...) is reachable without code changes. The client speaks one wire protocol; there are no per-vendor subclasses.

Three role slots key off the configured endpoint:

- `MEMMAN_LLM_MODEL_FAST` — recall hot path and `doctor`'s connectivity probe.
- `MEMMAN_LLM_MODEL_SLOW_CANONICAL` — worker's canonical-content path (fact extraction, reconciliation).
- `MEMMAN_LLM_MODEL_SLOW_METADATA` — worker's derived-metadata path (enrichment summaries/keywords, causal-edge inference).

For OpenRouter endpoints, `memman install` queries `/v1/models` once per role and writes the resolved id to `~/.memman/env`. For any non-OpenRouter endpoint the install wizard prompts for each slug interactively (vendor-native model ids like `gpt-4o-mini` or `qwen2.5:7b` don't share OpenRouter's `provider/model` slug shape and cannot be auto-resolved). Runtime never queries the model inventory; it reads the persisted id and sends it through unchanged. Re-run `memman install` to bump to a current version when a new model family ships. Splitting the slow worker into `_CANONICAL` and `_METADATA` lets enrichment cost be tuned (e.g., a cheaper metadata model) independently of the load-bearing extraction prompt.

### Operational controls

| Command                                   | Effect                                                                                     |
| ----------------------------------------- | ------------------------------------------------------------------------------------------ |
| `memman scheduler queue list [--limit N]` | inspect pending/done/failed rows                                                           |
| `memman scheduler queue retry <id>`       | re-queue a failed row                                                                      |
| `memman scheduler queue purge --done`     | delete completed rows                                                                      |
| `memman scheduler status`                 | install state, interval, next run, log paths                                               |
| `memman scheduler start`                  | activate the trigger (idempotent)                                                          |
| `memman scheduler stop`                   | deactivate the trigger; trigger files stay                                                 |
| `memman scheduler interval --seconds N`   | change cadence; min 60 s for systemd/launchd; serve mode accepts `>= 0` (`0` = continuous) |
| `memman scheduler trigger`                | dispatch a drain and return at once, without waiting for it (rejects when stopped)         |

`memman graph rebuild` re-enriches all already-stored insights through the full LLM pipeline (useful after model/prompt changes; rejects when the scheduler is stopped). Auto-created edges (semantic, entity, temporal) are recomputed on DB open when edge constants change — no operator command for that.

---

## 4.2 Read pipeline: smart recall

`memman recall` combines optional LLM query expansion, intent detection, multi-signal anchor selection, beam search graph traversal, and multi-factor re-ranking. Use `--basic` for SQL LIKE fallback.

`--basic` returns before Step 0 and runs none of the steps below, so every flag that only feeds a step is inert there. `--intent` is still validated -- `--basic --intent bogus` fails rather than reporting a malformed intent as merely ignored -- and both it and `--expand` are then named in `meta.ignored`. `--min-score` is rejected instead, whenever it is actually set: it is a filter, and dropping a floor silently would leave every returned row looking like it had cleared one. (`--min-score 0` is the off value and passes.) `--cat`, `--source` and `--brief` stay fully active. So does `--limit`, with one trap: `--limit 0` means unbounded on the scored path, where the slice runs only when `limit > 0`, but the basic path passes the number straight into a SQL `limit ?`, so `--basic --limit 0` returns nothing at all.

![Smart Recall Pipeline](../diagrams/03-smart-recall-pipeline.drawio.png)

### Step 0: LLM query expansion (opt-in, off by default)

`expand_query(llm_client, query)` sends the raw query to the LLM and returns:

- **expanded_query**: original + synonyms and related terms
- **intent**: WHY / WHEN / ENTITY / GENERAL (can override regex detection)

Expansion runs only when the user passes `--expand`, and never under `--basic`. By default the raw query is embedded directly. Expansion is gated because the LLM has no domain scope and can pull the candidate pool toward general-knowledge synonyms that recency-aware rerank (Step 4) then amplifies. Modern embedding models already capture most synonym intent; recency does the rest. See § 4.3.

### Step 1: Intent detection

Query intent is identified via regex (or LLM override from Step 0):

| Intent  | Trigger Patterns                                                                       |
| ------- | -------------------------------------------------------------------------------------- |
| WHY     | `why`, `reason`, `because`, `cause`, `motivation`, `rationale`                         |
| WHEN    | `when`, `time`, `date`, `before`, `after`, `during`, `timeline`, `history`, `sequence` |
| ENTITY  | `what is`, `who is`, `tell me about`, `describe`, `about`                              |
| GENERAL | None of the above match                                                                |

`--intent` manually overrides automatic detection. Under `--basic` no intent is detected at all, so the flag is validated and then reported in `meta.ignored`.

### Step 2: Multi-signal anchor selection (RRF fusion)

Three signals run in parallel and fuse via Reciprocal Rank Fusion:

```
Signal 1: Keyword     → KeywordCounts(query_tokens) → top-30
Signal 2: Vector      → CosineSimilarity(query_vec, all_embeddings, top-30)
Signal 3: Recency     → sort by created_at DESC, top-30

RRF Score = Σ  1 / (k + r)    (k = 60, r = 1-based rank)
                 for each signal
```

Each insight may rank differently across signals; RRF fusion produces a composite ranking that does not collapse when any one signal is noisy.

**Rationale.**

- **`ANCHOR_TOP_K = 30`**: per-signal anchor pool size. MAGMA Table 5 specifies 20; memman uses 30 to give beam search a richer starting frontier given the flat insight hierarchy (no episode/narrative super-nodes). The 30 is not flat in every case: with `--cat` or `--source` set and `limit > 0`, the budget widens to `max(ANCHOR_TOP_K, limit)` so a filtered recall can still fill a large limit. Unfiltered recall keeps `ANCHOR_TOP_K` untouched, which is what stops a bare `max()` from silently overriding the ablation harness's `anchor_top_k` sweep.
- **`RRF_K = 60`**: standard value from the original RRF paper (Cormack, Clarke & Büttcher, SIGIR 2009). MAP scores nearly flat from k=50–90, with k=60 validated across four TREC collections.
- **No absolute cosine floor on the vector channel.** `VECTOR_SEARCH_MIN_SIM = 0.10` was deleted, along with the `min_sim` parameter it fed: a fixed cosine means different things under different embedding models, so the floor bound silently on a store whose cosines center low and could not be re-derived when the provider changed. Measured inert where it shipped - over 120 queries and 166,156 (query, row) cosines under `voyage-3-lite` it removed ZERO rows from any top-30 anchor set, though 5.85% of pairs fell below it. `vector_anchors` now returns positives only, which is the one floor that is model-invariant: an orthogonal row is orthogonal under every model. A store with fewer than `k` positive-cosine rows therefore returns fewer than `k` anchors, by design.
- **The keyword channel counts in the store, not in Python, and no longer tokenizes a row at recall time.** `RecallSession.keyword_counts` returns how many distinct query tokens each active insight holds, counted where the text lives. On SQLite that is an index probe per query token against an FTS5 table. On Postgres each row stores its own distinct token set in `insights.kw_tokens`, written by `keyword.insight_tokens` at insert and recomputed when entities change, so the count is one GIN-indexed array intersection. The count is identical to the Python route by construction: stopword filtering on the row side cannot change it, because query tokens are stopword-filtered too and only tokens present in both sides enter the intersection. The first version of this channel counted in the store but still re-expressed the tokenizer in SQL and scanned sequentially, which measured at 75% of recall latency on the largest Postgres store; the stored column is 97% faster and returns the same rows. Each step replaced a slower route and moved nothing else - the score formula, its `[0, 1]` range and every returned row are unchanged. FTS5 `match` takes a query language, so the probe is built from `tokenize` output and never from query text; 8 of 11 realistic queries handed to `match` raw raise a syntax error. `search/keyword.py` keeps the per-row route for the drain's reconciliation pass, which ranks in-memory facts that are not in any index yet.

### Step 3: Beam search graph traversal

From each anchor, beam search traverses the four graphs:

```
for each anchor:
    priority_queue = [(anchor, initial_score)]
    visited = {}

    while budget_remaining:
        node = pop(priority_queue)
        for edge in GetEdgesFrom(node):
            neighbor = edge.target
            structural_score = edge.weight × intent_weight[edge.type]
            semantic_score = cosine(vec_neighbor, vec_query)
            total = score_node + λ₁·structural + λ₂·semantic
            //  λ₁ = 1.0 (structural weight), λ₂ = 0.4 (semantic weight)

            if total > best_score[neighbor]:
                update(neighbor, total)
                push(priority_queue, neighbor)
```

Beam width, max depth, and max-visited budgets are intent-adaptive — see the per-intent tuning table in Step 4.

### Step 4: Multi-factor re-ranking

For all collected candidates, a three-dimensional score is computed and combined via weighted sum:

```
keyword_score  = token_intersection / query_token_count
                 // the candidate's token set is content tokens UNION
                 // its entity-name tokens, so a stored entity name
                 // reaches the blend through this term
                 // the intersection is counted by the store, not by
                 // tokenizing every row per request -- one FTS5 probe
                 // per query token on SQLite, one query on Postgres
similarity     = cosine(vec_candidate, vec_query)
graph_score    = (traversal_score - min) / (max - min)   // min-max normalization

final = w_kw·keyword + w_sim·similarity + w_gr·graph
```

Each row sums to 1.0, so `final` is a weighted average carrying one range at every intent. A fourth term, `entity`, was removed in 0.23.0 (see the note below), which left the rows summing to WHY 0.90, WHEN 0.90, ENTITY 0.65 and GENERAL 0.85; 0.23.1 divided each row by its own sum. Both claims hold to within a float ulp: WHEN sums to 0.9999999999999999, and `similarity` is an unclamped cosine that can return 1 + 1 ulp, so `final` is bounded by 1 + 4.5e-16 rather than by 1. One range is not one meaning - the mix behind a 0.7 still differs per intent - and `graph_score` is min-max normalized over the query's own candidate pool, so no score compares across queries at any intent.

The rows inherit their DIRECTION from the pre-0.23.0 four-weight table; only the scale is new. They are not a measured optimum. `experiments/quality_matrix/results/sweep_rerank/` holds WHEN and WHY swept against three corpora, and the shipped arm is flagged beaten by the grid peak in all six runs; ENTITY and GENERAL have no grid at all. Read that record before treating this table as settled.

The division is computed at import, not written out, because no quotient here has an exact float literal - 0.45/0.90 is 1/2, yet computes to 0.5000000000000001, because the raw row sums to 0.8999999999999999. A rounded literal turns the row as well as scaling it: `experiments/recall_ablation/verify_weight_rounding.py` prices that turn in returned positions, and its record in that directory's README shows four decimals moving 68 of 8000 slots at limit 100 where the computed form moves none. Computing it also keeps the sum from drifting when someone edits a raw row.

Rescaling one intent's row by a positive constant leaves the weighted-sum order untouched, because every candidate in a call shares one intent. Two things break that end to end. Above the shortlist (next paragraph) the list holds two score scales at once, and lifting only one of them can reorder. And MMR scores `lam * relevance - (1 - lam) * max_pool_similarity`, mixing the blended score with a raw cosine, so it is not scale-invariant either - inert at the shipped `MMR_LAMBDA = 1.0`, but a swept lambda means something different after a rescale.

Note the interaction with the cross-encoder (Step 4b): when rerank fires it overwrites `final` for the top `RERANK_SHORTLIST = 100` rows, so on a pool of 100 or fewer these weights decide nothing about the order the caller sees. Above 100 they decide which rows reach the reranker at all.

When the pool exceeds the shortlist, that splice leaves cross-encoder scores on the head and blended scores on the tail; a smaller pool is overwritten whole and has no tail. The limit slice normally drops the tail, but it runs only when `limit > 0`, so `--limit 0` (unbounded) or `--limit > 100` returns both scales in one list, ordered on one key. The order within the head and within the tail is each internally consistent; only a comparison ACROSS the boundary is meaningless. Nothing re-sorts after the slice, so the splice can no longer be compounded by a second ordering pass - which is what previously let a weight change perturb the returned order on that path.

**Per-intent tuning.** The Step 3 traversal budget and the Step 4 reranker weights both vary by intent. Left columns tune beam search; right columns tune the reranker:

Reranker columns are the RAW rows as written in `_RERANK_WEIGHTS_RAW`; the shipped values are each divided by its row sum.

| Intent  | Beam | Depth | MaxVis | KW   | Sim      | Graph    |
| ------- | ---- | ----- | ------ | ---- | -------- | -------- |
| WHY     | 15   | 5     | 500    | 0.15 | **0.45** | **0.30** |
| WHEN    | 10   | 5     | 400    | 0.20 | **0.40** | **0.30** |
| ENTITY  | 10   | 4     | 400    | 0.20 | **0.35** | 0.10     |
| GENERAL | 10   | 4     | 500    | 0.25 | **0.45** | 0.15     |

**Rationale.**

- **`LAMBDA1 = 1.0`, `LAMBDA2 = 0.4`** (Step 3 traversal-score blend): `LAMBDA1` is from MAGMA Table 5 ("λ1 (Structure Coef.): 1.0 (Base)"); `LAMBDA2` falls within MAGMA's empirically tuned range (0.3–0.7), at the conservative end so structural signal is weighted 2.5× semantic.
- **Beam / Depth / MaxVis**: max depth 5 (WHY/WHEN) is from MAGMA Table 5. WHY gets beam width 15 (50% wider than the base 10) because causal chains typically span more hops. GENERAL gets `MaxVis=500` (matching WHY) because unknown intent should not restrict exploration. WHEN/ENTITY get 400 as a moderate budget — their primary edges (temporal/entity) form shorter chains.
- **KW / Sim / Graph**: extends MAGMA's intent-adaptive philosophy (which steers beam search via edge type weights) into the final reranking stage. MAGMA does not define a separate reranking stage — this is memman's extension.
- **The retired entity term.** A fourth signal, `matched_entities / query_entities_count`, was removed in 0.23.0. Its only feeder was Step 0's expansion, so from the moment expansion became opt-in it was identically 0.0 on the default path and non-zero only under `--expand` — and no harness ever swept it in that live state, because every harness call site passed an empty entity list. Fed deliberately, it measured indistinguishable from a random channel of the same magnitude, and the reason is scale: the mean of `w_ent x` the largest `entity_score` a query actually produced was 0.0596 against a mean rank-5-to-6 score margin of 0.0118, and on individual queries a full-scale match was worth 24x to 108x the margin it had to clear. A term that large does not inform the blend, it overrides it. Entities still reach recall two ways — as keyword tokens through the union above, and as `entity` graph edges, which carry the highest edge weight of any intent under ENTITY (0.55).

Embeddings are Nd vectors from the store's bound provider (dim is provider-defined; current default is `voyage-3-lite`, 512-dim). The expanded query from Step 0 is embedded for vector search and reranking.

### The `--min-score` floor (off by default)

The floor runs between the `--cat` / `--source` result filter and the MMR pass of Step 4a. `--min-score` drops any row whose `keyword + similarity` falls below the floor, so its range is 0.0 to 2.0 and `0.0` means off. It thresholds that sum rather than the blended `score` because `graph_score` is min-max normalized: the top candidate of any query scores 1.0 there, so a blended floor would sit at `w_gr` and move with the intent. It ships opt-in because the deep tail of a recall is often where the useful row sits.

### Step 4a: MMR diversity re-sort (off by default)

Between the `--cat`/`--source` result filter and the cross-encoder shortlist, a one-shot MMR pass can re-sort the top `MMR_POOL` (200) candidates by `lam * relevance - (1 - lam) * max_pool_similarity`, computed with one gram-matrix BLAS call over L2-normalized stored vectors (diagonal zeroed so a row's self-similarity is excluded). It is the cheap one-shot variant — every candidate scored once against the whole pool, then one sort — not greedy iterative MMR. Candidates without a cached embedding are exempt from the re-sort and hold their relevance position (scoring them would hand the degraded rows a zero penalty — the maximum diversity bonus). `MMR_POOL > RERANK_SHORTLIST` by construction so the pass can change shortlist membership when rerank is on. `MMR_LAMBDA` ships at the value measured by the `experiments/recall_ablation` mmr sweep; `1.0` disables the term (see the sweep record in that directory's README for why).

### Step 4b: Cross-encoder rerank

Rerank is on by default. The decision to run is resolved at recall time per call from config: `MEMMAN_RERANK_ENABLED_<store>` (per-store override) falls back to `MEMMAN_RERANK_ENABLED` (global default, `true` post-install). When enabled and the query has more than `MIN_RERANK_TOKENS` (default 2) whitespace tokens, the top `RERANK_SHORTLIST` (default 100) candidates from Step 4 are re-scored by the configured cross-encoder reranker (`MEMMAN_RERANK_PROVIDER`; current default `voyage` with model `rerank-3-lite`), and the rerank score replaces the multi-signal score for the final ordering. Operators disable rerank for a noisy store with `memman config set MEMMAN_RERANK_ENABLED_<store> false`.

Bi-encoder retrieval (Steps 1–4) embeds the query and each insight independently and ranks by cosine plus the three signals. A cross-encoder reads `(query, content)` together with full attention and outputs a relevance score directly, so it resolves cases where bi-encoder cosine misses the right answer despite low token overlap.

Failures (timeouts, non-200 responses) are caught and logged; the baseline ordering is returned unchanged with `meta.reranked = false`. The 1-2 token query gate skips rerank when there is too little query signal for the cross-encoder to use.

### Why rerank is on by default

Rerank is enabled by default because a labeled-corpus evaluation showed it lifts retrieval quality where the bi-encoder is weakest, with no observed regression on the kinds of queries it was predicted to hurt. WHY and WHEN intents — initially predicted to regress under cross-encoder reranking — gained the most, because their bi-encoder baselines were the weakest. The per-store `MEMMAN_RERANK_ENABLED_<store>` knob exists for operators whose corpora prove to be exceptions.

### Step 5: WHY structure — causal edges in `meta`

If the intent is WHY, the response carries `meta.causal_edges`: the `[cause, effect]` pairs among the returned rows, cause first, restricted to ids the caller actually received. An empty list is emitted rather than omitted, because "these rows carry no causal relation to each other" is a fact the rows themselves cannot convey.

The list is built from the SOURCE-KEYED `directed` adjacency, never the symmetrized `bidir` map the beam traversal walks — the beam crosses a causal edge from either end on purpose, but a payload that did so would give every pair a spurious reverse. Pairs are emitted in returned-row order rather than by iterating an id set, because string hashing is salted per process and a set-ordered payload would differ between two runs of the same query.

**Rows are not re-ordered.** There is no post-limit sort on any intent: the returned order is relevance order at every `--limit`, so the first `n` rows of a page of `m` are exactly what a page of `n` returns. A chronological or topological re-sort of a page already cut by relevance asserts an ordering the result set does not contain — five rows dated across a year read as a timeline when they are the five most relevant, arranged to look like one. Relevance order asserts only what each row's visible `score` already shows. On `WHEN`, sort on `created_at`, which `--brief` carries.

### Signal breakdown

Each retrieval result includes signal details:

```json
{
  "insight": {
    "id": "...",
    "content": "...",
    "summary": "..."
  },
  "score": 0.72,
  "intent": "ENTITY",
  "via": "entity",
  "signals": {
    "keyword": 0.85,
    "similarity": 0.72,
    "graph": 0.45,
    "rerank": 0.81
  }
}
```

Provenance is tracked internally but NOT returned. The pipeline keeps
a `via` label per candidate -- either the anchor channel that selected
the row (`keyword`, `vector`, `hybrid`, `time`) or the edge type that
reached it (`entity`, `temporal`, `causal`, `semantic`) -- and the
traversal overwrites an anchor's channel label whenever it re-scores
that node. On a well-connected store that overwrite is near-total:
measured over 3,000 returned rows, the label took only edge-type
values and reported an anchor channel ZERO times, so a row the vector
channel surfaced came back labeled `entity`. It also predicted
nothing, spanning 0.05 in precision against judged relevance across
its four values while a typical page carried four distinct ones. A
field that is both wrong and uninformative was removed from the
caller payload rather than corrected in place; reinstating it means
fixing the overwrite first.

`signals.rerank` is present only on rows Step 4b actually re-scored: it is the cross-encoder score, and it is the same number that replaced `score`. Its absence means the row never reached the shortlist, or that rerank was off, gated by the token minimum, or failed.

The `summary` field is the LLM-authored one-line gloss produced during enrichment (slow_metadata role). It is present only when (a) enrichment has run for the row and (b) the summary actually compresses the content (write-time gate at `len(summary) < len(insight.content) * 0.85`); rows that fail the gate emit no `summary` key. Calling LLMs see ~3.6× token compression with ~90% ranking-decision agreement vs full content.

The host LLM sees these signals and can apply its own judgment with full conversation context.

### Response envelope

`intent_aware_recall` returns `{'results': [...], 'meta': {...}}`. The `meta` object is the pipeline's account of its own run, and is what lets a calling LLM weigh the rows it got:

| Field           | Computed at | Meaning                                                                                          |
| --------------- | ----------- | ------------------------------------------------------------------------------------------------ |
| `intent`        | Step 1      | The resolved intent, whatever supplied it                                                        |
| `intent_source` | Step 1      | `override` when `--intent` or a Step 0 expansion hint supplied the intent, else `auto`           |
| `hint`          | Step 1      | Per-intent reasoning guidance from `RECALL_HINTS`; always present                                |
| `anchor_count`  | Step 2      | Fused anchor pool size, after `--cat` / `--source` filtering                                     |
| `traversed`     | Step 3      | Candidates scored, deliberately unfiltered                                                       |
| `reranked`      | Step 4b     | `true` only when the cross-encoder re-scored the shortlist; a reranker failure leaves it `false` |
| `causal_edges`  | Step 5      | WHY only: `[cause, effect]` pairs among the returned rows; emitted even when empty               |

Two of these carry a trap worth stating. `intent_source` reads `override` for a Step 0 expansion hint exactly as it does for an explicit `--intent`, so it distinguishes automatic detection from everything else, not the user from the LLM. And `anchor_count` against `traversed` is the filter diagnostic, read against the budget rule in Step 2 rather than against a flat 30: while `limit` stays at or below `ANCHOR_TOP_K`, an anchor count that collapses under a selective `--cat` while `traversed` stays wide says the filter starved the anchor pools, not the graph. Above that, a filter widens the budget itself and the two move for reasons that are not the diagnosis.

**There is no confidence flag, and that is deliberate.** Recall returns rows even when nothing matches -- Step 2's Recency channel anchors the newest insights regardless -- so a full page is not evidence that anything on it is relevant. An empty `results` means the store itself is empty, not that the query failed. The response answers this per ROW instead: every returned row carries its own `score` and its per-channel `signals`, which a caller compares WITHIN one response. A boolean derived from a threshold would freeze one reranker's score scale into the envelope, and the scale changes when `MEMMAN_RERANK_PROVIDER` or the model does; a per-row score weighed against its siblings does not.

`ignored` is emitted only when non-empty. A calling LLM reads this envelope out of its own context window, so a key that always reported `false` would spend tokens to say nothing.

Under `--basic` none of those keys exist. That envelope is `{'basic': true}`, plus `ignored` when a flag was wasted -- a list of bare flag names (`intent`, `expand`), without the leading dashes. `--basic` returns before ranking, so it carries no `score` and no `signals` either: it can return nothing and says so no differently than a full page.

The rows change shape as well, which matters more to a consumer than the missing `meta` keys. A scored row wraps its insight -- `{'insight': ..., 'score': ..., 'intent': ..., 'signals': ...}` -- while a basic row IS the bare insight dict. Code reading `results[i]['insight']` or `results[i]['signals']` breaks under `--basic`.

### Recall trace events

With debug tracing enabled (`MEMMAN_DEBUG=1` or `memman scheduler debug on`), `intent_aware_recall` emits per-phase events: `recall_anchors` (per-signal hit counts, fused pool size, and `vector_hits` against `anchor_k` — the measurement for whether a selective filter starves the vector scan), `recall_traversal` (beam-search visited count and how many anchors hit the visit budget), and `recall_rerank` (how many shortlist positions actually moved, diffed by id — the reranker replaces every score, so a score diff would always read "all moved"). The `trace.is_enabled()` gate is read once per recall, not per event site, because it can fall through to a file read on the synchronous hot path.

## 4.3 Model resilience

memman calls LLMs at write time (extraction, reconciliation, enrichment, causal inference) and embedding models on every vector. Prompts get edited, models get upgraded, providers get swapped. The design goal is detection and re-run, not bit-identical output across versions.

Two principles:

1. **Keep slow work off the hot path.** The write path defers LLM work to the scheduler drain (Tier 2 in 4.1). The read path is embedding-only at the bare-CLI level. LLM query expansion is opt-in via `--expand`; the cross-encoder reranker is on by default but gated by the per-store config knob `MEMMAN_RERANK_ENABLED_<store>` (no CLI flag — the model never sees it). Where LLM judgment is unavoidable, the output is tagged with what produced it and re-runnable.
2. **Provenance + re-run beats deterministic-rule replacement.** Hard rules (length thresholds, importance clamps, similarity cutoffs) calcify with one model's behavior baked in. Provenance + re-run tracks what produced each row and re-derives when inputs change. Same precedent as the embed-fingerprint mechanism.

### Invalidation hooks

| Hook                                                             | Stored at | Detects                                                                                                                        | Operator action                                                                                                                                                                                                                                                                                                              |
| ---------------------------------------------------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `embed_fingerprint`                                              | `meta`    | per-store embedder binding                                                                                                     | Each store's stored fingerprint binds the embedder used by recall, drain, and graph rebuild. Change the binding via `memman embed swap` (online, resumable shadow-column backfill) or `memman embed reembed` (offline, scheduler-stopped).                                                                                   |
| `embed_swap_state` / `embed_swap_cursor` / `embed_swap_target_*` | `meta`    | in-flight swap progress                                                                                                        | Written by `embed swap`; **deleted** on cutover or `--abort`. `memman doctor`'s `no_stale_swap_meta` check warns if any key remains on an idle store.                                                                                                                                                                        |
| `insights.prompt_version`                                        | per row   | enrichment prompt, causal prompt, or `slow_metadata` model change                                                              | `memman doctor` warns; remediate via `memman graph rebuild --stale-only` or `UPDATE insights SET linked_at=NULL, enriched_at=NULL WHERE prompt_version='<old>';` then drain. `insights.model_id` is write provenance only and is NOT a drift signal: it names the model behind the row's content, which no rebuild rewrites. |
| `constants_hash`                                                 | `meta`    | edge-construction constants change, and a completed `embed swap` (which clears the key so stale semantic edge weights rebuild) | Auto-reindex on next open + warning.                                                                                                                                                                                                                                                                                         |
| `linked_at` / `enriched_at`                                      | per row   | per-row pipeline-stage completion                                                                                              | `link_pending` drains naturally.                                                                                                                                                                                                                                                                                             |

Per-row provenance columns are preferred over global meta-key fingerprints because they expose the actual rebuild scope: how many rows came from which prompt or model. That distribution is what the operator needs to write a targeted hand-update SQL rather than rebuilding the whole store.

### What is NOT used

memman does not run multi-LLM consensus, calibrate against a target judgment distribution, or hold deterministic rules that override LLM output. Each adds permanent complexity that conflicts with future model improvements. Provenance + re-run keeps the implementation simple and lets future model upgrades be a deliberate operator action rather than a silent shift.
