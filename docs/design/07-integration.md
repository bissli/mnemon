# 7. LLM CLI Integration

[< Back to Design Overview](../DESIGN.md)

---

![Integration Architecture](../diagrams/08-three-layer-integration.drawio.png)

Mnemon integrates with LLM CLIs through lifecycle hooks, a skill file, and a behavioral guide. Claude Code's [hook system](https://docs.anthropic.com/en/docs/claude-code/hooks) is the reference implementation — all components are deployed automatically via `mnemon setup`.

## 7.1 Integration Architecture

Five hooks drive the memory lifecycle:

```
Session starts
    │
    ▼
  Prime (SessionStart) ─── prime.sh ──→ load guide.md (memory execution manual)
    │
    ▼
  User sends message
    │
    ▼
  Remind (UserPromptSubmit) ─── user_prompt.sh ──→ remind agent to recall & remember
    │
    ▼
  Skill (SKILL.md) ── command syntax reference (auto-discovered)
    │
    ▼
  LLM generates response (following guide.md behavioral rules)
    │
    ▼
  Nudge (Stop) ─── stop.sh ──→ remind agent to remember
    │
    ▼
  (when context compacts)
  Compact (PreCompact) ─── compact.sh ──→ flag file for post-compact recall
    │
    ▼
  (before delegating to sub-agents)
  Recall (PreToolUse) ─── task_recall.sh ──→ remind agent to recall before delegation
```

Three layers work together:

| Layer     | What                                                                        | Where                    | Role                                                                                                               |
| --------- | --------------------------------------------------------------------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------------ |
| **Hooks** | Shell scripts triggered by Claude Code lifecycle events                     | `.claude/hooks/mnemon/`  | Prime (guide), Remind (recall & remember), Nudge (remember), Compact (pre-compact bridge), Recall (pre-delegation) |
| **Skill** | `SKILL.md` — command reference in Claude Code skill format                  | `.claude/skills/mnemon/` | Teaches the LLM *how* to use mnemon commands                                                                       |
| **Guide** | `guide.md` — detailed execution manual for recall, remember, and delegation | `~/.mnemon/prompt/`      | Teaches the LLM *when* to recall, *what* to remember, and *how* to delegate                                        |

## 7.2 Hook Details

Claude Code fires hooks at specific lifecycle events. Mnemon registers up to five, each with a distinct role in the memory lifecycle:

**Prime (SessionStart) — `prime.sh`**

Runs once when a session starts. Loads the behavioral guide — a detailed execution manual that teaches the agent when to recall, what to remember, and how to delegate memory writes:

```bash
STATS=$(mnemon status 2>/dev/null)
if [ -n "$STATS" ]; then
  # extract counts from JSON and show in status line
  echo "[mnemon] Memory active (<insights> insights, <edges> edges)."
else
  echo "[mnemon] Memory active."
fi
[ -f ~/.mnemon/prompt/guide.md ] && cat ~/.mnemon/prompt/guide.md
```

The guide content appears in the LLM's system context, establishing recall/remember/delegation behavior for the entire session.

**Remind (UserPromptSubmit) — `user_prompt.sh`**

Runs on every user message. A lightweight prompt that reminds the agent to evaluate whether recall and remember are needed before starting work:

```bash
echo "[mnemon] Evaluate: recall needed? After responding, evaluate: remember needed?"
```

The agent decides whether to act on this reminder based on the guide.md rules — it is a suggestion, not forced execution.

**Nudge (Stop) — `stop.sh`**

Runs after each LLM response. Returns a `decision: block` JSON so the agent gets one more turn to evaluate memory. Directive-aware: prompts the agent to store if a user preference, decision, or conclusion emerged. Stays silent when `stop_hook_active` is true (preventing infinite loops):

```bash
INPUT=$(cat)
if echo "$INPUT" | grep -q '"stop_hook_active".*true'; then
  exit 0
fi
cat <<'EOF'
{"decision": "block", "reason": "[mnemon] Memory check: did the user state a preference, make a decision, give a correction, or reach a conclusion? If yes, store via Task(Bash, model=sonnet) sub-agent. Only skip if the exchange was purely open-ended questions with no resolution."}
EOF
```

**Compact (PreCompact + SessionStart) — `compact.sh` + `prime.sh` (optional)**

A two-part bridge that preserves memory context across context compaction. PreCompact cannot inject context into the agent's conversation (stdout is verbose-mode only), so the solution uses a flag file relay:

1. `compact.sh` fires at PreCompact — writes a flag file to `~/.mnemon/compact/<session_id>.json` with the trigger type and timestamp
2. After compaction, Claude Code fires SessionStart with `source=compact`
3. `prime.sh` detects `source=compact`, reads the flag file for enrichment, and injects a recall instruction the agent can see

The design is defensively layered — `prime.sh` detects compaction from the SessionStart `source` field regardless of whether `compact.sh` ran. The flag file enriches the message with trigger type but is not required.

```bash
# compact.sh (PreCompact) — writes flag file
cat > "${COMPACT_DIR}/${SESSION_ID}.json" <<EOF
{"trigger":"${TRIGGER:-auto}","ts":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF

# prime.sh (SessionStart) — detects compact source, injects recall
if [ "$SOURCE" = "compact" ]; then
  echo "[mnemon] Context was just compacted (${TRIGGER:-auto}). Recall critical context now."
fi
```

**Recall (PreToolUse) — `task_recall.sh` (optional)**

Fires before the agent delegates to a sub-agent. Reminds the agent to recall relevant context before delegation, ensuring sub-agents receive informed prompts:

```bash
echo "[mnemon] Before delegating: recall relevant context first (mnemon recall \"<query>\" --limit 5) unless already done for this topic."
```

## 7.3 Automated Setup

`mnemon setup` handles all deployment automatically:

```
$ mnemon setup

Detecting LLM CLI environments...
  ✓ Claude Code (v1.x)    .claude/

Select environment: Claude Code
Install scope: Local — this project only (.claude/)

[1/3] Skill
  ✓ Skill     .claude/skills/mnemon/SKILL.md

[2/3] Prompts
  ✓ Prompts   ~/.mnemon/prompt/ (guide.md, skill.md)

[3/3] Optional hooks
  Select hooks to enable:
    [x] Remind  — remind agent to recall & remember (recommended)
    [x] Nudge   — remind about memory on session end
    [x] Compact — save context before compaction (recommended)
    [x] Recall  — remind agent to recall before delegating (recommended)

Setup complete!
  Hooks   prime, remind, nudge, compact, recall
  Prompts ~/.mnemon/prompt/ (guide.md, skill.md)

Start a new Claude Code session to activate.
Edit ~/.mnemon/prompt/guide.md to customize behavior.
Run 'mnemon setup --eject' to remove.
```

Key setup options:

| Flag                   | Effect                                                                       |
| ---------------------- | ---------------------------------------------------------------------------- |
| `--global`             | Install to `~/.claude/` (all projects) instead of `.claude/` (project-local) |
| `--target claude-code` | Non-interactive, Claude Code only                                            |
| `--eject`              | Remove all mnemon integrations                                               |
| `--yes`                | Auto-confirm all prompts (CI-friendly)                                       |

The Prime hook is always installed. Remind, Nudge, Compact, and Recall hooks are optional (all enabled by default).

## 7.4 Sub-Agent Delegation

Memory writes don't happen in the main conversation. Instead, the host LLM delegates to a lightweight sub-agent:

```
Main Agent (Opus)                     Sub-Agent (Sonnet)
┌──────────────────────┐              ┌──────────────────────┐
│ Full conversation     │  delegates   │ ~1000 tokens context │
│ context (~25k tokens)  │ ──────────→ │ Reads SKILL.md       │
│                       │              │ Executes commands    │
│ Decides WHAT to       │  result      │ Evaluates candidates │
│ remember               │ ←────────── │ with judgment        │
└──────────────────────┘              └──────────────────────┘
```

**Why sub-agent?**

| Dimension    | Main conversation        | Sub-agent                |
| ------------ | ------------------------ | ------------------------ |
| Context size | ~25,000 tokens           | ~1,000 tokens            |
| Model        | Opus (expensive)         | Sonnet (cheaper)         |
| Scope        | Full conversation        | Memory task only         |
| Execution    | Synchronous, blocks user | Background, non-blocking |

The main agent provides only WHAT to store — content, category, importance, entities. The sub-agent reads SKILL.md, executes the correct `mnemon remember` command, and evaluates `remember`'s link candidates with judgment — not mechanical rules.

This separation means:

- **Token economy**: ~7,000 total tokens per memory write vs ~25,000 if done in main conversation
- **Context isolation**: Memory processing doesn't pollute the main conversation context
- **Model efficiency**: Sonnet handles routine execution while Opus focuses on high-level decisions

## 7.5 Adapting to Other LLM CLIs

For CLIs with hook support, replicate the Claude Code pattern: register lifecycle hooks that call mnemon commands, deploy the skill file, and provide the behavioral guide.

For CLIs without hook support, merge the recall/remember guidance into the corresponding system prompt file:

- Cursor -> `.cursorrules`
- Windsurf -> `RULES.md`
- OpenClaw -> `mnemon setup --target openclaw` deploys skill + guide, but hooks require manual plugin configuration
- Others -> System prompt / rules file
