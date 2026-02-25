#!/bin/bash
# mnemon Stop hook — prompt agent to evaluate remembering.
# Returns JSON decision:block so the agent sees the reason and gets
# one more turn. Checks stop_hook_active to prevent infinite loops.

INPUT=$(cat)

if echo "$INPUT" | grep -q '"stop_hook_active"[[:space:]]*:[[:space:]]*true'; then
  exit 0
fi

cat <<'EOF'
{"decision": "block", "reason": "[mnemon] Memory check: if a conclusion, decision, or preference emerged, evaluate memory via Task(Bash, model=sonnet) sub-agent. If still mid-planning or mid-investigation, stop without storing."}
EOF
