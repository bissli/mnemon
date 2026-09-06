# memman 0.34.0 - the write contract

The reconciler judges one fact against every candidate memory and acts on
every row the fact contradicts. The F round measured the defect this
release fixes: of six verified contradictions whose target sat in the
reconciler's own candidate set, the shipped write path stored every one
as an ADD or linked the wrong row. No schema column moves; the store
format and the backup format are unchanged.

## What changes

- **One fact, every candidate, one action per affected memory.**
  `reconcile_memories` takes one fact and returns `action`, a list of
  `(id, relation)` targets and one `merged_text`. SUPERSEDE names every
  contradicted row, UPDATE at most one refined row, NONE the one row
  that restates the fact, else ADD. `_apply_plan` supersedes each target,
  moves each one's edges to the one successor, writes one oplog row per
  target, and reports `replaced_ids`; a target that is no longer current
  is dropped into `targets_gone` and the plan degrades to a plain add only
  when every target is gone. A later fact in the same write keeps its
  free targets when one is already taken.
- **The reconcile shortlist is logged.** Every plan that carried
  candidates writes a `reconcile-candidates` oplog row first: `fact_id`,
  the fact, and each candidate with its rung (`keyword` or `cosine`) and
  score, keyed on the new row, or on the NONE or exact-match target for a
  skip. The re-measurement replays decisions against exactly the rows the
  model saw.
- **The JSON reader takes the model's final answer.** `parse_json_response`
  now reads the LAST top-level object in a response that carries prose
  before its JSON, or a first block followed by a correction and a second
  block, and repairs a lone backslash such as `NT AUTHORITY\SYSTEM`. At
  HEAD every such response was a parse failure and the fact landed as ADD.
- **The reconciler asks for 8192 output tokens.** `complete()` takes a
  per-call `max_tokens`; the reconciler passes `RECONCILE_MAX_TOKENS`.
  A response over ten large candidates was cut at the 4096 role ceiling,
  failed to parse, and landed as ADD.
- **Importance leaves the extractor.** The extraction prompt has no
  importance ladder and no `importance` key; the parsed fact carries
  `text`, `category`, `entities`. `--imp` (1-5, default 3) is stored as
  passed and is a sort key for listings and tie-breaks. The exit is
  recorded in `docs/design/02-concepts.md`.
- **The locator rule keeps the path.** `See cli.py:182 for the fix`
  becomes `See cli.py for the fix`; `line 42` is dropped; `host:port`,
  `HH:MM`, `image:tag` and `code:404` are untouched.
- **`general` leaves the category set.** The five categories are
  `preference`, `decision`, `fact`, `insight`, `context`; `--cat` defaults
  to `fact` and `--cat general` exits non-zero. `Insight.category`
  defaults to `fact`; new stores get `default 'fact'` on the column.
- **`--source` is free-form provenance.** `user` (the default) for the
  user's words, `agent` for the agent's own conclusion, a locator (URL,
  script, dataset pull) for imported material; stored verbatim; recall's
  `--source` filter is an exact match.
- **One write verb for the agent.** The guide's Step 2 says a correction
  goes in with `memman remember` like any other fact, its text naming
  what is no longer true and what is; the worker finds the stored row and
  supersedes it with a merge that keeps its still-true clauses. The hooks
  are unchanged. The guide also says what a row is: an assertion at its
  `created_at`, not a directive, with a `git log --since` check before a
  path-naming row is acted on.

## Measured before shipping

The reconciler prompt is a tunable and shipped only measured. The probe
replayed 174 real cases from the live stores (the fact, the shortlist the
write path built at the time, and a verified expectation) plus 4
constructed duplicate writes, three times each, against the shipped
prompt and the candidate, on the `slow_canonical` model.

| line | shipped write path | 0.34.0 contract |
| --- | --- | --- |
| verified contradictions superseded (64 cases) | 9 | 30 (McNemar b=22 c=1, p=6e-06) |
| multi-target cases with every row superseded (3) | 0 | 2 |
| restatements answered NONE (11 synthetic, 4 real duplicate writes) | 15 of 15 | 15 of 15 |
| labeled-safe rows superseded (101) | 0 | 10 |
| merged texts keeping every still-true clause | 1 of 8 | 2 of 17 |

Of the 10 labeled-safe rows the contract superseded, an independent
judge found the label wrong in 3 (the fact overturns a claim the row
makes), a status or next-step row overtaken by events in 4, an exact
duplicate of a row the fact updates in 2, and one genuine miss. The
shipped path's zero is the rate of a path that supersedes almost
nothing. Merge quality is poor in both and no different between them;
it is the baseline the field re-measurement reads against. Sixteen
cases were cut at the 4,096-token ceiling; at 8,192 the cut responses
fell from 15 to 6 of 48 and the parsed ones rose from 40 to 45.

The parser finding above came out of the same run: a third of the
shipped prompt's real responses failed the whole-text reader and
landed as ADD; the shipped contract's first entry was an UPDATE or ADD
of a different row even when its JSON parsed, so the gain is the
contract, not the reader.

## Migration

No column is added or dropped; `PAYLOAD_VERSION` and
`BACKUP_FORMAT_VERSION` do not move; the scheduler keeps running.

`general` rows, on each store that holds any (three fleet-wide on
2026-09-05: one current row in `default`, one superseded row each in
`memman` and `pfiles`):

    update insights set category = 'fact' where category = 'general';

Postgres stores, the column default, once per store schema `s`:

    alter table s.insights alter column category set default 'fact';

SQLite stores keep a dead `default 'general'` on the column, by decision:
SQLite changes a column default only through a table rebuild (create,
copy, drop, rename, every index and the FTS shadow), no insert path omits
the category (`_process_queue_row` and `Insight.category` both supply
one), and nothing reads the default. The rebuild is not worth its risk
for a default nothing reads.

Rows enqueued before 0.34.0 drain after it with `hint_imp` NULL; the
drain's null-to-3 fallback exists for those rows only. Every `remember`
and `replace` on 0.34.0 enqueues an importance.
