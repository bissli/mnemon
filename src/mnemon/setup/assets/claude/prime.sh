#!/bin/bash
# mnemon SessionStart hook — load memory context.
# Reads SessionStart JSON input to detect post-compact restarts.

PROMPT_DIR="${HOME}/.mnemon/prompt"

if [ -t 0 ]; then
  INPUT='{}'
else
  INPUT=$(cat)
fi
SOURCE=$(echo "$INPUT" | sed -n 's/.*"source": *"\([^"]*\)".*/\1/p' | head -1)

if ! command -v mnemon >/dev/null 2>&1; then
  echo "[mnemon] Warning: mnemon not found in PATH."
  [ -f "${PROMPT_DIR}/guide.md" ] && cat "${PROMPT_DIR}/guide.md"
  exit 0
fi

STATS=$(mnemon status 2>/dev/null)
if [ -n "$STATS" ]; then
  INSIGHTS=$(echo "$STATS" | sed -n 's/.*"total_insights": *\([0-9]*\).*/\1/p' | head -1)
  EDGES=$(echo "$STATS" | sed -n 's/.*"edge_count": *\([0-9]*\).*/\1/p' | head -1)
  echo "[mnemon] Memory active (${INSIGHTS:-0} insights, ${EDGES:-0} edges)."
else
  echo "[mnemon] Memory active."
fi

if [ "$SOURCE" = "compact" ]; then
  SESSION_ID=$(echo "$INPUT" | sed -n 's/.*"session_id": *"\([^"]*\)".*/\1/p' | head -1)
  FLAG="${HOME}/.mnemon/compact/${SESSION_ID}.json"
  TRIGGER=""
  if [ -n "$SESSION_ID" ] && [ -f "$FLAG" ]; then
    TRIGGER=$(sed -n 's/.*"trigger":"\([^"]*\)".*/\1/p' "$FLAG" | head -1)
    rm -f "$FLAG"
  fi
  echo "[mnemon] Context was just compacted (${TRIGGER:-auto}). Recall critical context now: mnemon recall \"<topic>\" --limit 5"
fi

[ -f "${PROMPT_DIR}/guide.md" ] && cat "${PROMPT_DIR}/guide.md"

exit 0
