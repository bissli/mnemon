### Recall — before responding

**Default: recall on every new user message AND before each new task/phase**, unless ALL of these apply:
- Direct follow-up within a topic already fully in context
- No reference to past sessions, decisions, or preferences
- No knowledge dependency beyond the current conversation

**Always recall before**:
- Launching explore/plan/code agents — recall BEFORE delegation
- Starting a new task or switching topics
- Web search — stored context sharpens queries
- Making architectural or design decisions
- Writing code that touches patterns discussed in past sessions

To recall: `mnemon recall "<query>" --limit 5`.
Craft a focused, keyword-rich query — do not pass the raw user prompt.

### Phase awareness — when to write

Not every exchange warrants a memory write. Defer during deliberation, commit at
decision points.

**Stability test**: "Would I be comfortable storing this as-is if we stopped here?"

- **Defer** during planning, investigation, or back-and-forth deliberation.
  Intermediate conclusions will shift — writing them wastes writes and creates churn.
- **Write** at stability boundaries: plan finalization, task completion, session end,
  or when a conclusion is reached that won't change with further discussion.
- **User directives are always immediate** — explicit preferences, decisions,
  corrections, or "remember this" bypass phase checks entirely.

### Remember — after responding

Run this decision tree at stability boundaries (not mid-deliberation).
**Bias toward capturing conclusions**: defer during deliberation, commit at decision points.

**Step 1 — Does this exchange contain any of the following?**

Tier A (importance 4-5, always store):
- User directive — explicit preference, decision, correction, or "remember this"
- Reasoning conclusion — non-trivial judgment from multi-source synthesis
- Durable system/architectural fact discovered during this session
- User-specific context that no search engine can recover

Tier B (importance 2-3, store unless trivial):
- Casual preference revealed in passing ("I usually...", "I prefer...", "I don't like...")
- Topic explored, with conclusion or current understanding (not just questions)
- Useful framing or analogy the user offered
- Background context about the user's projects, tools, or setup

→ None of the above → STOP.

**Step 2 — Does a highly overlapping memory already exist?**
→ Yes, incremental new info → UPDATE (merge into existing)
→ Yes, but contradicts/supersedes → REPLACE
→ No significant overlap → CREATE

**Step 3 — Importance calibration**
Use the full 2-5 scale intentionally:
- 5: Cross-session core fact, architectural decision, strong user preference
- 4: Important context, significant finding, clear user preference
- 3: Useful background, project context, topic of interest
- 2: Passing mention, soft preference, conversational color

Importance 2 is the floor — if imp=2 feels weak, reconsider storing at all.

**What to store**: conclusions AND sufficient context to understand them.
**How to store**:
- **In plan mode** or when tools are restricted: run `mnemon remember` directly
  via the Bash tool in the main conversation. This is permitted because mnemon
  commands are read/write to the mnemon DB only — they do not modify code.
- **In normal mode**: delegate to a Task sub-agent (`subagent_type="Bash"`,
  `model="sonnet"`) to keep the main context clean.

**Batch writes**: At stability boundaries, accumulate multiple memories and write
them in a single sub-agent invocation. Format as a bulleted list in the sub-agent
prompt; the sub-agent executes sequential `mnemon remember` calls.

Only provide what to store — content, category, importance, entities, and create/update intent.
The sub-agent will read the mnemon skill and execute the correct commands itself.

Do NOT: write CLI commands or workflow steps in the sub-agent prompt (the sub-agent has access to the skill docs and will use the correct flags).
Do NOT remember operational/public/git-tracked/transient info.

### Causal links — after writing

After writing stable memories, evaluate `causal_candidates` from the remember output.
When cause-effect relationships exist between memories, call `mnemon link --type causal`.
Look for reason/consequence pairs — e.g., a decision and the constraint that drove it.
Optional — requires manual detection; skip when no clear causal signal is present.

### Pre-compaction note

compact.sh only writes a flag file, NOT memories. Ensure conclusions are written
during the session via phase-aware timing above. After compaction, the Prime hook
prompts recall to restore critical context.
