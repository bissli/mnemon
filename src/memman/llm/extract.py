"""LLM-based fact extraction, memory reconciliation, and query expansion."""

import logging
import re
from typing import Any

import cachetools
from memman import config, trace
from memman.llm import usage as llm_usage
from memman.llm.client import MemmanLLMClient
from memman.llm.shared import drop_overlong_strings, parse_json_response

logger = logging.getLogger('memman')

_LINE_WORD_RE = re.compile(r'\bline \d+\b', re.IGNORECASE)
# Notes:
# - Only a source, config or doc extension marks a locator: a bare
#   dot-letter run would also match a dotted host such as
#   `db.example.com:5432` and strip its port.
# - `localhost:8080`, `192.0.2.1:8000`, `14:18`, `python:3.11` and
#   `code:404` carry no such extension before the colon.
_FILE_LINE_RE = re.compile(
    r'(\b[\w./-]+\.(?:py|pyi|js|jsx|ts|tsx|md|rst|txt|html|htm|css|scss'
    r'|json|jsonl|yaml|yml|toml|ini|cfg|conf|sql|sh|bash|zsh|ps1|rs|go'
    r'|java|kt|c|h|cc|cpp|hpp|cs|rb|php|swift|lua|xml|csv|tsv|ipynb'
    r'|drawio|tf|vue|svelte|proto|mk|cmake)):\d{1,5}\b', re.IGNORECASE)
_WS_COLLAPSE_RE = re.compile(r'\s+')


def _strip_line_refs(text: str) -> str:
    """Drop `line N` and the `:N` of a `file.ext:N` locator, keeping the path.

    Parameters
    ----------
    text : str
        Fact text as the model returned it.

    Returns
    -------
    str
        The text with `line N` gone, the `:N` of every `file.ext:N`
        locator gone (a source, config or doc extension), the path kept,
        and whitespace collapsed to single spaces.

    Notes
    -----
    - The path locates the claim and stays; the line number is a
      snapshot of the file at write time and goes.
    """
    text = _LINE_WORD_RE.sub('', text)
    text = _FILE_LINE_RE.sub(r'\1', text)
    return _WS_COLLAPSE_RE.sub(' ', text).strip()


FACT_EXTRACTION_SYSTEM = """You are a personal memory system curator. The user is storing memories for future recall via an upstream fast LLM that emits raw, unpolished content. Your job has THREE steps applied in order.

## Step 1: Skip-judgment

Decide if the input is durable knowledge worth long-term storage. Return {"facts": [], "skip_reason": "..."} for non-durable shapes:

- status updates ("All tests passed", "deployed v1.4 to staging")
- task receipts ("All drives verified after the maintenance window")
- in-progress observations ("migration is 60% complete")
- ephemeral metrics ("queue depth is 4 right now")
- "I just did X" reports
- one-off action confirmations
- greetings, filler, or unintelligible text

Otherwise continue to Step 2.

## Step 2: Canonical-shape rewrite

Rewrite into clean prose, claim-for-claim. The rewrite is strictly 1-to-1: same facts, reshaped surface form. Apply:

- Strip all-caps section markers (REBUILD COST:, KEY FINDING:, ROOT CAUSE:, ARCHITECTURAL CONSTRAINT:)
- Strip back-references (memory [N], see [0], "as mentioned earlier")
- Replace anaphoric openers ("This means", "That implies") with explicit subjects
- Strip transient adverbs (currently, today, "as of YYYY-MM-DD")
- Drop preamble filler ("This is relevant for...", "It is important to note that...")
- Convert past-tense decision narratives ("Rejected X. Chose Y.") into present-tense durable rules
- When the input frames something as non-default state (preserved, enabled, set to, retained, kept), keep that framing - don't flatten to plain present-tense
- Preserve specific names, numbers, and technical terms verbatim. Do not generalize.

## Step 3: Return as a SINGLE atomic fact

Return EXACTLY ONE fact whose `text` is the cleaned content from Step 2. Do NOT split a coherent input into multiple facts. The user remembers in coherent chunks; recall synthesizes across chunks via the graph and similarity. Splitting at storage time loses context.

CRITICAL: even if the input contains 3-5 distinct claims about a single coherent topic (e.g., a multi-section blob describing one system's behavior), return ONE fact whose text covers all the claims in canonical paragraph form. Splitting is not the curator's job.

Output JSON:
{"facts": [{"text": "<cleaned content>", "category": "preference|decision|fact|insight|context", "entities": [...]}], "skip_reason": null}

## Category mapping

- preference: user likes/dislikes/prefers
- decision: explicit choice of X over Y with rationale, or "X uses Y rather than Z"
- fact: how something works (formulas, API behavior, data layout, code patterns)
- insight: lesson learned from experience
- context: project background or user role

A formula or behavior description is a fact, not a decision.

## CRITICAL DIRECTIVE: Preserve the user's domain vocabulary

The examples below illustrate STYLE transformations only. Never substitute indexing, pipeline, database, or other domain terminology if the user's input did not use it. If the input is about React components, return facts about React components. If about CloudFormation, return facts about CloudFormation. The lessons below are about SHAPE (skip / cleanup / single-fact), not subject matter.

---

## Examples - Category 1: Accepted, barely changed

Input: "Pre-fetch object keys into a set for 1M+ file transfers to avoid 1M HEAD requests."
Output: {"facts": [{"text": "Pre-fetch object keys into a set for 1M+ file transfers to avoid 1M HEAD requests.", "category": "fact", "entities": ["HEAD requests"]}], "skip_reason": null}

Input: "Avoid materializing all files upfront with list(fs.walk(recursive=True)) because it causes 30-60 minute enumeration delay and ~300MB memory spike."
Output: {"facts": [{"text": "Avoid materializing all files upfront with list(fs.walk(recursive=True)) because it causes 30-60 minute enumeration delay and ~300MB memory spike.", "category": "fact", "entities": ["fs.walk"]}], "skip_reason": null}

Input: "render_grouped() added a sort_key parameter to enable custom sorting when grouping by non-date partition keys."
Output: {"facts": [{"text": "render_grouped() added a sort_key parameter to enable custom sorting when grouping by non-date partition keys.", "category": "fact", "entities": ["render_grouped"]}], "skip_reason": null}

Input: "A timeout of 5.0 seconds and max_retries=1 were added to the LLM HTTP client to bound worst-case LLM latency."
Output: {"facts": [{"text": "A timeout of 5.0 seconds and max_retries=1 were added to the LLM HTTP client to bound worst-case LLM latency.", "category": "decision", "entities": ["LLM HTTP client"]}], "skip_reason": null}

Input: "CLI surface should be minimal. Every flag must earn its existence."
Output: {"facts": [{"text": "CLI surface should be minimal. Every flag must earn its existence.", "category": "preference", "entities": ["CLI"]}], "skip_reason": null}

## Examples - Category 2: Accepted, rewritten (style cleanup, claims preserved, ONE fact only)

Input: "Decision: rejected the _cleanup_helper() approach for orphan directory migration. Instead, manual rm -rf during rollout. Rationale: aligns with memory [3] preference for manual one-time cleanup over migration code in small-userbase projects."
Output: {"facts": [{"text": "Orphan directory migration uses manual rm -rf during rollout rather than a _cleanup_helper() function; in small-userbase projects, manual one-time cleanup is preferred over migration code.", "category": "decision", "entities": ["_cleanup_helper", "rm -rf"]}], "skip_reason": null}

Input: "INTENT_PARSER.PY DEPRECATION PATH: intent_parser.py (an LLM-based query parser) has been DELETED. Deletion was safe because all three deprecation conditions were met: (1) the search MCP tool docstring was updated to teach LLM callers to extract metadata filters; (2) callers internalized the new filter-extraction pattern; (3) the local LLM dependency was removed from the runtime."
Output: {"facts": [{"text": "intent_parser.py, an LLM-based query parser, was deleted after its three deprecation conditions were met: the search MCP tool docstring was updated to teach callers to extract metadata filters; callers internalized the new filter-extraction pattern; and the local LLM runtime dependency was removed.", "category": "fact", "entities": ["intent_parser.py", "MCP"]}], "skip_reason": null}

Input: "The pipeline currently uses Postgres as the system of record. This means the search index is fully recomputable from Postgres, which makes Postgres backups the only durable persistence layer."
Output: {"facts": [{"text": "The pipeline uses Postgres as the system of record; the search index is fully recomputable from Postgres, which makes Postgres backups the only durable persistence layer.", "category": "fact", "entities": ["Postgres"]}], "skip_reason": null}

(Multi-section synthesis blobs with multiple SHOUTY headers are also single-fact outputs: rewrite as one canonical paragraph, all claims preserved, no decomposition.)

## Examples - Category 3: Skip as non-durable

Input: "All tests passed in the latest CI run after the rebase."
Output: {"facts": [], "skip_reason": "test_run_receipt"}

Input: "Currently processing the backlog at about 12 documents per second."
Output: {"facts": [], "skip_reason": "ephemeral_throughput"}

Input: "Just deployed v1.4 to staging via the release script."
Output: {"facts": [], "skip_reason": "deployment_receipt"}

Input: "Hi there"
Output: {"facts": [], "skip_reason": "greeting"}

## Examples - Edge cases (surface phrasing matches but durability differs)

Input: "All EC2 application updates are deployed via the standard release script, never via direct SSH."
Output: {"facts": [{"text": "All EC2 application updates are deployed via the standard release script, never via direct SSH.", "category": "preference", "entities": ["EC2", "SSH"]}], "skip_reason": null}

Input: "Every login attempt is verified against the LDAP directory before the session token is issued."
Output: {"facts": [{"text": "Every login attempt is verified against the LDAP directory before the session token is issued.", "category": "fact", "entities": ["LDAP"]}], "skip_reason": null}

---

Now process the user's input. Return ONLY JSON, no commentary."""

# The reconciler answers over up to ten candidates that can run to 90 KB
# of text, and a response cut at the role ceiling fails to parse and
# lands the fact as ADD; measured on the 2026-09-05 probe.
RECONCILE_MAX_TOKENS = 8192

RECONCILIATION_SYSTEM = """You are a memory manager. One NEW FACT arrives with a list of EXISTING MEMORIES, each under a numeric id. Judge EVERY memory against the fact and return one action per memory whose state the fact changes. A memory whose state the fact does not change gets no entry.

Relations, judged on present-tense claims only:
- CONTRADICTS: the memory asserts something the fact says is no longer so or never was: a changed value or name, a mechanism that was removed or replaced, a decision that was reversed, or a question the memory left open that the fact settles the other way. A memory that reports a dated event, or what was true at a stated time, is not contradicted by a later state. A fact that says something is no longer true, or corrects an earlier statement, contradicts every memory that asserts the old state.
- REFINES: the fact adds compatible detail to the memory's subject.
- RESTATES: the memory already carries every claim the fact makes.
- UNRELATED: a different subject, or compatible independent claims.

Actions:
- SUPERSEDE <id>: the fact contradicts memory <id>. Name EVERY contradicted memory, not only the closest one. A memory the fact merely repeats or extends is never superseded.
- UPDATE <id>: the fact refines memory <id>. At most one. When several memories restate the fact, UPDATE the most complete one; the others get no entry.
- NONE <id>: memory <id> restates the fact. Alone.
- ADD: no memory is contradicted, refined, or restating. Alone.
The action field takes only ADD, UPDATE, SUPERSEDE, or NONE.

merged_text is required when any action is UPDATE or SUPERSEDE. It states the new fact and keeps EVERY clause of every named memory that the fact does not contradict, in that memory's own words where possible. It drops only the contradicted clauses and never restates a contradicted claim as true. Otherwise it is null.

Return JSON:
{"merged_text": "<text or null>",
 "actions": [
  {"action": "ADD|UPDATE|SUPERSEDE|NONE",
   "target_id": null for ADD, else the numeric id,
   "reason": "brief explanation"}
 ]}

When one memory restates the fact and another contradicts it, the restating memory takes UPDATE (it is folded into the successor) and the contradicted one takes SUPERSEDE; NONE is for a fact that changes nothing.

Use the numeric IDs shown, not UUIDs. A contradicted memory gets SUPERSEDE, never ADD."""

QUERY_EXPANSION_SYSTEM = (
    'Expand a search query for a personal memory system.\n\n'
    'Return JSON:\n'
    '{"expanded_query": "original plus synonyms and related terms",\n'
    ' "intent": "WHY|WHEN|ENTITY|GENERAL"}\n\n'
    'Keep expanded_query under 50 words.')


def extract_facts(
        llm_client: MemmanLLMClient,
        content: str) -> list[dict]:
    """Extract the one canonical fact of `content` via the LLM.

    Parameters
    ----------
    llm_client : MemmanLLMClient
        The slow canonical client; one `complete` call per invocation.
    content : str
        The text as the caller wrote it.

    Returns
    -------
    list[dict]
        Dicts with keys `text`, `category`, `entities`. Empty when the
        model skipped the input as non-durable. A single passthrough
        fact wrapping `content` on an LLM error, a parse error, or an
        empty `facts` list.

    Notes
    -----
    - Importance is not extracted: the caller's `--imp` is stored as
      passed (default 3) and the model has no say in it.
    """
    trace.event('extract_facts_start', content_len=len(content),
                content=content)
    try:
        raw = llm_client.complete(
            FACT_EXTRACTION_SYSTEM, content,
            stage=llm_usage.STAGE_EXTRACTION)
    except Exception as exc:
        logger.debug('LLM fact extraction failed, using passthrough')
        trace.event(
            'extract_facts_result',
            outcome='passthrough',
            error=f'{type(exc).__name__}: {exc}')
        return _passthrough_fact(content, 'fact')

    parsed = parse_json_response(raw)
    if parsed is None:
        logger.debug('LLM fact extraction parse error, using passthrough')
        trace.event(
            'extract_facts_result',
            outcome='parse_error',
            raw=raw)
        return _passthrough_fact(content, 'fact')

    skip_reason = parsed.get('skip_reason')
    if skip_reason:
        logger.debug(f'LLM skipped: {skip_reason}')
        trace.event(
            'extract_facts_result',
            outcome='skipped',
            skip_reason=skip_reason)
        return []

    raw_facts = parsed.get('facts', [])
    if not isinstance(raw_facts, list) or not raw_facts:
        trace.event('extract_facts_result', outcome='no_facts', raw=raw)
        return _passthrough_fact(content, 'fact')

    facts = []
    for f in raw_facts:
        if not isinstance(f, dict):
            continue
        text = _strip_line_refs(f.get('text', '').strip())
        if not text:
            continue
        category = f.get('category', 'fact')
        if category not in {'preference', 'decision', 'fact',
                            'insight', 'context'}:
            category = 'fact'
        entities = f.get('entities', [])
        if not isinstance(entities, list):
            entities = []
        entities = drop_overlong_strings(
            [str(e) for e in entities if e],
            kind='entity', owner=f'extracted fact {text[:32]!r}')
        facts.append({
            'text': text,
            'category': category,
            'entities': entities,
            })

    result = facts or _passthrough_fact(content, 'fact')
    trace.event(
        'extract_facts_result',
        outcome='ok',
        fact_count=len(result),
        skip_reason=skip_reason,
        facts=result)
    return result


def _passthrough_fact(content: str, category: str) -> list[dict]:
    """Wrap raw content as a single fact for fallback."""
    return [{
        'text': content,
        'category': category,
        'entities': [],
        }]


def reconcile_memories(
        llm_client: MemmanLLMClient,
        fact: dict,
        existing_memories: list[tuple[str, str]]) -> dict:
    """Judge one fact against every candidate memory via the LLM.

    Parameters
    ----------
    llm_client : MemmanLLMClient
        The slow canonical client; one `complete` call per invocation.
    fact : dict
        One extracted fact; only `text` is read.
    existing_memories : list[tuple[str, str]]
        `(real_id, content)` pairs, the reconcile shortlist. Shown to
        the model under numeric ids so it cannot hallucinate a uuid.

    Returns
    -------
    dict
        `action` in `ADD | UPDATE | SUPERSEDE | NONE`; `targets`, a list
        of `(real_id, relation)` with relation `supersede`, `update` or
        `none`; `merged_text`, the successor's content for UPDATE and
        SUPERSEDE, else None. Every failure path (an empty shortlist, an
        LLM error, a parse error, no usable entry) returns ADD with no
        targets and no merged text.

    Notes
    -----
    - Normalization, in order: DELETE reads as SUPERSEDE; an unknown
      action or an unresolvable id drops its entry; a duplicate id keeps
      its first UPDATE or SUPERSEDE entry (an ADD or NONE on the same id
      never shields it); when any UPDATE or SUPERSEDE survives, ADD and
      NONE entries are dropped, UPDATE keeps its first target, and the
      action is SUPERSEDE if any target is supersede; else the first
      NONE with a resolved id is the answer; else ADD.
    - `merged_text` is read from the top level and, when that is empty,
      from the first kept linking entry that carries one.
    """
    add = {'action': 'ADD', 'targets': [], 'merged_text': None}
    if not existing_memories:
        return dict(add)

    id_map = {}
    memory_lines = []
    for idx, (real_id, content) in enumerate(existing_memories):
        id_map[str(idx)] = real_id
        memory_lines.append(f'[{idx}] {content}')
    prompt = (
        'EXISTING MEMORIES:\n'
        + '\n'.join(memory_lines)
        + '\n\nNEW FACT:\n'
        + fact['text'])

    trace.event('reconcile_start', existing_count=len(existing_memories))
    try:
        raw = llm_client.complete(
            RECONCILIATION_SYSTEM, prompt,
            stage=llm_usage.STAGE_RECONCILIATION,
            max_tokens=RECONCILE_MAX_TOKENS)
    except Exception as exc:
        logger.debug('LLM reconciliation failed, defaulting to ADD')
        trace.event(
            'reconcile_result',
            outcome='error',
            error=f'{type(exc).__name__}: {exc}')
        return dict(add)

    parsed = parse_json_response(raw)
    if parsed is None or not isinstance(parsed.get('actions'), list):
        trace.event('reconcile_result', outcome='parse_error', raw=raw)
        return dict(add)

    linked: list[tuple[str, str, Any]] = []
    linked_ids: set[str] = set()
    nones: list[str] = []
    n_entries = 0
    for a in parsed['actions']:
        if not isinstance(a, dict):
            continue
        action = str(a.get('action', '')).upper()
        # A DELETE the model still emits is a contradiction verdict.
        if action == 'DELETE':
            action = 'SUPERSEDE'
        if action not in {'ADD', 'UPDATE', 'SUPERSEDE', 'NONE'}:
            continue
        target_id = None
        if a.get('target_id') is not None:
            target_id = id_map.get(str(a['target_id']))
        if action != 'ADD' and target_id is None:
            continue
        n_entries += 1
        if action == 'NONE':
            nones.append(target_id)
        elif action != 'ADD' and target_id not in linked_ids:
            linked_ids.add(target_id)
            linked.append((target_id, action.lower(), a.get('merged_text')))

    supersedes = [t for t, rel, _m in linked if rel == 'supersede']
    updates = [t for t, rel, _m in linked if rel == 'update'][:1]
    kept = [(t, rel, m) for t, rel, m in linked
            if rel == 'supersede' or t in updates]
    merged_text = parsed.get('merged_text')
    if not isinstance(merged_text, str) or not merged_text.strip():
        merged_text = next(
            (m for _t, _rel, m in kept if isinstance(m, str) and m.strip()),
            None)

    if kept:
        result = {
            'action': 'SUPERSEDE' if supersedes else 'UPDATE',
            'targets': [(t, rel) for t, rel, _m in kept],
            'merged_text': merged_text,
            }
    elif nones:
        result = {'action': 'NONE', 'targets': [(nones[0], 'none')],
                  'merged_text': None}
    else:
        result = dict(add)

    if n_entries == 0:
        trace.event('reconcile_result', outcome='empty', fallback='ADD')
    else:
        trace.event(
            'reconcile_result', outcome='ok',
            action=result['action'], targets=result['targets'])
    return result


_EXPAND_CACHE_TTL = 300
_EXPAND_CACHE_MAX = 256


def _normalize_for_cache(query: str) -> str:
    """Lowercase + collapse whitespace; nothing else."""
    return ' '.join(query.lower().split())


def _expand_cache_key(query: str) -> str:
    """Salt with the configured fast-model id.

    The model id is resolved at install time and persisted to
    `~/.memman/env`, so `config.require` always returns a real value
    here. Reaching this with an unset key means install was never run,
    which is a `ConfigError` upstream callers handle.
    """
    import hashlib
    salt = config.require(config.LLM_MODEL_FAST)
    digest = hashlib.sha256(
        f'{_normalize_for_cache(query)}|{salt}'.encode())
    return digest.hexdigest()[:16]


_expand_cache: cachetools.TTLCache = cachetools.TTLCache(
    maxsize=_EXPAND_CACHE_MAX, ttl=_EXPAND_CACHE_TTL)


def reset_expand_cache() -> None:
    """Drop cached query expansions. Used by tests that swap env vars."""
    _expand_cache.clear()


def expand_query(
        llm_client: MemmanLLMClient,
        query: str) -> dict:
    """Expand recall query with synonyms and related terms.

    Returns dict with: expanded_query, intent.
    On failure: passthrough with original query. Repeated calls with
    the same query in the same process hit a `cachetools.TTLCache`
    keyed by sha256(normalized_query | $MEMMAN_LLM_MODEL_FAST). Cache
    lives only for the duration of one CLI invocation (memman is a
    one-shot CLI), so persistence across processes is left to the
    LLM provider's own response cache.
    """
    cache_key = _expand_cache_key(query)
    cached = _expand_cache.get(cache_key)
    if cached is not None:
        trace.event(
            'query_expand_result', outcome='cache_hit', **cached)
        return dict(cached)

    trace.event('query_expand_start', query=query)
    try:
        raw = llm_client.complete(
            QUERY_EXPANSION_SYSTEM, query,
            stage=llm_usage.STAGE_QUERY_EXPANSION)
    except Exception as exc:
        logger.debug('LLM query expansion failed, using passthrough')
        trace.event(
            'query_expand_result',
            outcome='error',
            error=f'{type(exc).__name__}: {exc}')
        return {'expanded_query': query, 'intent': None}

    parsed = parse_json_response(raw)
    if parsed is None:
        return {'expanded_query': query, 'intent': None}

    expanded = parsed.get('expanded_query', query)
    if not isinstance(expanded, str) or not expanded.strip():
        expanded = query

    intent = parsed.get('intent')
    if intent not in {'WHY', 'WHEN', 'ENTITY', 'GENERAL'}:
        intent = None

    result = {
        'expanded_query': expanded,
        'intent': intent,
        }
    _expand_cache[cache_key] = dict(result)
    trace.event('query_expand_result', outcome='ok', **result)
    return result
