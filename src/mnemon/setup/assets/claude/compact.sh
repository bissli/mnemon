#!/bin/bash
# mnemon PreCompact hook — bridge context across compaction.
# Writes a flag file so prime.sh can enrich the recall reminder
# after compaction completes (SessionStart source=compact).
# The flag is supplementary — prime.sh detects compaction from
# SessionStart source=compact regardless of whether this hook ran.

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | sed -n 's/.*"session_id": *"\([^"]*\)".*/\1/p' | head -1)
TRIGGER=$(echo "$INPUT" | sed -n 's/.*"trigger": *"\([^"]*\)".*/\1/p' | head -1)

if [ -z "$SESSION_ID" ]; then
  exit 0
fi

COMPACT_DIR="${HOME}/.mnemon/compact"
mkdir -p "$COMPACT_DIR"

cat > "${COMPACT_DIR}/${SESSION_ID}.json" <<FLAGEOF
{"trigger":"${TRIGGER:-auto}","ts":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
FLAGEOF

exit 0
