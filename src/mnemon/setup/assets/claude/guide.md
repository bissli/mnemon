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

**Store immediately** when the user states a preference, makes a decision, gives a
correction, or says "remember this." These are **user directives** — never defer them,
even mid-conversation.

**Defer** only when the exchange is pure back-and-forth deliberation with no conclusion
yet (e.g., "what are the pros and cons of X?" without a decision following). Intermediate
conclusions that will shift with further discussion waste writes.

**Stability test** for non-directive content: "Would I be comfortable storing this as-is
if we stopped here?" If yes, store it. If the conclusion might change in the next
exchange, defer.

### Remember — after responding

Run this decision tree after each response.
**Bias toward capturing**: when in doubt, store rather than defer.

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

**Category mapping** (pass via `--cat`):
- User stated preference → `preference`
- Architectural/design decision with rationale → `decision`
- Discovered fact about a system, tool, or domain → `fact`
- Reasoning conclusion synthesized from multiple sources → `insight`
- Background context (project setup, user role, environment) → `context`

**Excluded — never store regardless of tier:**

Recoverability test: *Can this fact be recovered from the project's code,
config, IaC state, or cloud account?* If yes, do not store it.

- Bug/issue discoveries — store the *resolution*, not the problem
- State snapshots (line counts, file sizes, resource counts, instance IDs)
- Deployment/verification receipts ("all verified", "deployed via", "state clean")
- Temporal observations ("currently", "not yet", "TODO", "should be changed to")
- Intermediate findings that will shift once the task completes

**Mixed content**: strip recoverable details (code paths, boot sequences),
keep only reasoning and conclusions.

**Step 2 — Does a highly overlapping memory already exist?**
→ Yes, incremental new info → UPDATE (merge into existing)
→ Yes, but contradicts/supersedes → REPLACE
→ No significant overlap → CREATE

**Step 3 — Importance calibration**
Use the full 2-5 scale intentionally:
- 5: Cross-session core fact, architectural decision, strong user preference
  NOT: deployment details, resource inventories, task completion receipts
- 4: Important context, significant finding, clear user preference
  NOT: facts recoverable from code/config, routine operational outcomes
- 3: Useful background, project context, topic of interest
- 2: Passing mention, soft preference, conversational color

Importance 2 is the floor — if imp=2 feels weak, reconsider storing at all.

**What to store**: conclusions AND sufficient context to understand them.
**How to store**: in plan mode, run `mnemon remember` directly via Bash.
In normal mode, delegate to a Task sub-agent (`subagent_type="Bash"`,
`model="sonnet"`). See skill doc for execution details.

### Causal links — after writing

After writing stable memories, evaluate `causal_candidates` from the remember output.
When cause-effect relationships exist between memories, call `mnemon link --type causal`.
Look for reason/consequence pairs — e.g., a decision and the constraint that drove it,
a problem discovery and the fix it led to, or a tool limitation and the workaround adopted.
Always link when causal_candidates are returned and a relationship is plausible;
skip only when candidates are clearly unrelated.

### Pre-compaction note

compact.sh only writes a flag file, NOT memories. Ensure conclusions are written
during the session via phase-aware timing above. After compaction, the Prime hook
prompts recall to restore critical context.
