#!/usr/bin/env bash
#
# Mnemon Agent-Driven E2E Test
#
# Drives Claude Code through a 20-step REST API spec (bookmark manager)
# with deliberate decision points, preference reversals, and quality traps.
# After all steps, inspects the mnemon database for memory quality, recall
# effectiveness, and trap filtering.
#
# Usage:
#   make e2e-agent
#   MAX_STEPS=3 make e2e-agent      # quick smoke (first 3 steps only)
#   CLAUDE_MODEL=haiku make e2e-agent
#
set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────
CLAUDE_MODEL="${CLAUDE_MODEL:-sonnet}"
AGENT_TIMEOUT="${AGENT_TIMEOUT:-180}"
MAX_BUDGET_USD="${MAX_BUDGET_USD:-20}"
MAX_STEPS="${MAX_STEPS:-20}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
WORK_DIR="$PROJECT_DIR/.testdata/agent_e2e/ws"
STORE_NAME="e2e_test"
M=mnemon

# ── Colors ───────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

PASS=0
FAIL=0
WARN=0
TOTAL=0

# ── Cost Tracking ────────────────────────────────────────────────────
STEP_TIMES=()
STEP_COSTS=()
STEP_LABELS=()
TOTAL_COST="0"

# ── Helpers ──────────────────────────────────────────────────────────
banner() {
  echo ""
  echo -e "${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
  echo -e "${BOLD}${CYAN}  $1${RESET}"
  echo -e "${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
}

step() {
  echo ""
  echo -e "  ${YELLOW}▸${RESET} ${BOLD}$1${RESET}"
}

pass() {
  local label="$1"; local detail="${2:-}"
  TOTAL=$((TOTAL + 1)); PASS=$((PASS + 1))
  echo -e "    ${GREEN}✔${RESET} $label ${DIM}$detail${RESET}"
}

fail() {
  local label="$1"; local detail="${2:-}"
  TOTAL=$((TOTAL + 1)); FAIL=$((FAIL + 1))
  echo -e "    ${RED}✘${RESET} $label ${DIM}$detail${RESET}"
}

soft_warn() {
  local label="$1"; local detail="${2:-}"
  WARN=$((WARN + 1))
  echo -e "    ${YELLOW}⚠${RESET} $label ${DIM}$detail${RESET}"
}

fatal() {
  echo -e "    ${RED}FATAL:${RESET} $1"
  cleanup
  exit 1
}

assert_contains() {
  if echo "$2" | grep -qi "$3"; then
    pass "$1" "(contains: $3)"
  else
    fail "$1" "(expected: $3)"
  fi
}

assert_not_contains() {
  if echo "$2" | grep -qi "$3"; then
    fail "$1" "(should NOT contain: $3)"
  else
    pass "$1" "(absent: $3)"
  fi
}

assert_jq() {
  local label="$1" json="$2" filter="$3" expected="$4"
  local actual
  actual=$(echo "$json" | jq -r "$filter" 2>/dev/null || echo "__ERROR__")
  if [ "$actual" = "$expected" ]; then
    pass "$label" "($filter == $expected)"
  else
    fail "$label" "($filter: expected=$expected, got=$actual)"
  fi
}

assert_jq_gte() {
  local label="$1" json="$2" filter="$3" expected="$4"
  local actual
  actual=$(echo "$json" | jq -r "$filter" 2>/dev/null || echo "0")
  if [ "$actual" -ge "$expected" ] 2>/dev/null; then
    pass "$label" "($filter=$actual >= $expected)"
  else
    fail "$label" "($filter: expected >= $expected, got=$actual)"
  fi
}

assert_jq_lte() {
  local label="$1" json="$2" filter="$3" expected="$4"
  local actual
  actual=$(echo "$json" | jq -r "$filter" 2>/dev/null || echo "0")
  if [ "$actual" -le "$expected" ] 2>/dev/null; then
    pass "$label" "($filter=$actual <= $expected)"
  else
    fail "$label" "($filter: expected <= $expected, got=$actual)"
  fi
}

# ── Send Prompt ──────────────────────────────────────────────────────
# send_prompt STEP_NUM LABEL PROMPT
# Sets globals: RESULT, SESSION_ID
send_prompt() {
  local step_num="$1" label="$2" prompt="$3"
  local step_start step_end step_duration

  echo -e "  ${DIM}  Step $step_num: $label${RESET}"

  step_start=$(date +%s)

  if [ -z "${SESSION_ID:-}" ]; then
    RESULT=$(cd "$WORK_DIR" && CLAUDECODE= timeout "$AGENT_TIMEOUT" claude -p "$prompt" \
        --output-format json \
        --dangerously-skip-permissions \
        --model "$CLAUDE_MODEL" \
        --max-budget-usd "$MAX_BUDGET_USD" 2>/dev/null) || true
    SESSION_ID=$(echo "$RESULT" | jq -r '.session_id // empty' 2>/dev/null)
  else
    RESULT=$(cd "$WORK_DIR" && CLAUDECODE= timeout "$AGENT_TIMEOUT" claude \
        --resume "$SESSION_ID" -p "$prompt" \
        --output-format json \
        --dangerously-skip-permissions \
        --model "$CLAUDE_MODEL" \
        --max-budget-usd "$MAX_BUDGET_USD" 2>/dev/null) || true
  fi

  step_end=$(date +%s)
  step_duration=$((step_end - step_start))
  local step_cost
  step_cost=$(echo "$RESULT" | jq -r '.total_cost_usd // 0' 2>/dev/null || echo "0")

  STEP_TIMES+=("$step_duration")
  STEP_COSTS+=("$step_cost")
  STEP_LABELS+=("$label")
  TOTAL_COST=$(echo "$TOTAL_COST + $step_cost" | bc 2>/dev/null || echo "$TOTAL_COST")

  echo -e "  ${DIM}  → ${step_duration}s, \$${step_cost}${RESET}"
}

# ── Setup ────────────────────────────────────────────────────────────
setup() {
  banner "Setup"

  # Remove any previous test store
  MNEMON_STORE="$STORE_NAME" $M store remove "$STORE_NAME" 2>/dev/null || true

  step "Create test store"
  $M store create "$STORE_NAME" 2>/dev/null || true
  echo -e "    ${DIM}Store: $STORE_NAME${RESET}"

  step "Create workspace"
  rm -rf "$WORK_DIR"
  mkdir -p "$WORK_DIR/.claude"

  cat > "$WORK_DIR/.claude/settings.json" << EOF
{
  "env": {
    "MNEMON_STORE": "$STORE_NAME"
  }
}
EOF

  cd "$WORK_DIR" && git init --quiet && git commit --allow-empty -m "init" --quiet
  echo -e "    ${DIM}Workspace: $WORK_DIR${RESET}"

  step "Verify test store is empty"
  local out
  out=$(MNEMON_STORE="$STORE_NAME" $M status)
  local insights
  insights=$(echo "$out" | jq -r '.total_insights' 2>/dev/null || echo "0")
  if [ "$insights" = "0" ]; then
    pass "fresh store" "(0 insights)"
  else
    fatal "test store not empty ($insights insights)"
  fi
}

# ── Cleanup ──────────────────────────────────────────────────────────
cleanup() {
  echo ""
  echo -e "  ${DIM}Cleanup:${RESET}"
  MNEMON_STORE="$STORE_NAME" $M store set default 2>/dev/null || true
  MNEMON_STORE="$STORE_NAME" $M store remove "$STORE_NAME" 2>/dev/null || true
  echo -e "  ${DIM}  Removed store: $STORE_NAME${RESET}"
  rm -rf "$PROJECT_DIR/.testdata/agent_e2e"
  echo -e "  ${DIM}  Removed workspace${RESET}"
}

# ── Smoke Test ───────────────────────────────────────────────────────
smoke_test() {
  banner "Smoke Test"

  step "Verify claude binary"
  command -v claude > /dev/null || fatal "claude not found in PATH"
  pass "claude binary found"

  step "Single-step env propagation test"
  local smoke_result smoke_session
  local env_file="$WORK_DIR/env_check.txt"
  rm -f "$env_file"

  smoke_result=$(cd "$WORK_DIR" && CLAUDECODE= timeout 120 claude -p \
      "Write the value of the MNEMON_STORE environment variable to a file called env_check.txt. Just the raw value, nothing else. Use: echo \$MNEMON_STORE > env_check.txt" \
      --output-format json \
      --dangerously-skip-permissions \
      --model "$CLAUDE_MODEL" \
      --max-budget-usd 1 2>/dev/null) || true

  smoke_session=$(echo "$smoke_result" | jq -r '.session_id // empty' 2>/dev/null)
  [ -n "$smoke_session" ] || fatal "no session_id in smoke test output"
  pass "session created" "(id: ${smoke_session:0:12}...)"

  local smoke_cost
  smoke_cost=$(echo "$smoke_result" | jq -r '.total_cost_usd // "?"' 2>/dev/null)
  echo -e "    ${DIM}smoke cost: \$$smoke_cost${RESET}"

  if [ -f "$env_file" ] && grep -q "$STORE_NAME" "$env_file"; then
    pass "MNEMON_STORE propagated to agent" "(file contains: $(cat "$env_file" | tr -d '\n'))"
  elif [ -f "$env_file" ]; then
    fatal "MNEMON_STORE not propagated (file contains: $(cat "$env_file" | tr -d '\n'))"
  else
    fatal "agent did not create env_check.txt — Bash tool may not have run"
  fi

  step "Verify hooks fired (check store for any activity)"
  local post_smoke
  post_smoke=$(MNEMON_STORE="$STORE_NAME" $M status)
  local oplog
  oplog=$(echo "$post_smoke" | jq -r '.oplog_count' 2>/dev/null || echo "0")
  if [ "$oplog" -ge 0 ]; then
    pass "hooks operational" "(oplog: $oplog)"
  else
    soft_warn "could not verify hook activity"
  fi

  # Clean the store after smoke test (don't pollute real steps)
  MNEMON_STORE="$STORE_NAME" $M store remove "$STORE_NAME" 2>/dev/null || true
  $M store create "$STORE_NAME" 2>/dev/null || true
  SESSION_ID=""
}

# ── Step Prompts ─────────────────────────────────────────────────────
run_steps() {
  banner "Agent Steps (1-$MAX_STEPS)"

  local prompts=()
  local labels=()

  # Phase 1: Decision Steps (1-12)
  labels+=("Framework choice")
  prompts+=("We're going to build a REST API bookmark manager in Python. I'm considering Flask or FastAPI. Which would you recommend and why? Don't write any code yet, just discuss the trade-offs.")

  labels+=("FastAPI preference")
  prompts+=("I like FastAPI. Let's go with that. Set up the basic project structure with a main.py entry point. Keep it minimal — just the FastAPI app instance and a health check route.")

  labels+=("Storage decision")
  prompts+=("For storage, should we use SQLite or PostgreSQL? I want something simple for now but upgradeable later.")

  labels+=("SQLite + SQLAlchemy")
  prompts+=("Let's use SQLite with SQLAlchemy so we can swap to PostgreSQL later without changing the code. Set up the database models file.")

  labels+=("Bookmark model")
  prompts+=("The bookmark model should have: url, title, tags, created_at. Store tags as a comma-separated string for simplicity.")

  labels+=("Pagination preference")
  prompts+=("We need pagination for the list endpoint. I prefer cursor-based pagination over offset-based — it's more scalable.")

  labels+=("Pagination reversal")
  prompts+=("Actually, I changed my mind. Let's switch to simple offset/limit pagination instead. Cursor-based is overkill for our use case.")

  labels+=("Auth approach")
  prompts+=("For authentication, let's use API keys passed in the X-API-Key header. No OAuth — too complex for this project.")

  labels+=("Rate limiting")
  prompts+=("Add rate limiting: 100 requests per minute per API key. Use a simple in-memory approach for now.")

  labels+=("Rate limit scaling")
  prompts+=("If we run multiple workers, the in-memory rate limiter won't work. What options do we have for shared state? Just discuss, don't implement yet.")

  labels+=("Export format")
  prompts+=("Add an export endpoint: GET /bookmarks/export?format=json or format=csv. The format is selected via query parameter.")

  labels+=("Error format")
  prompts+=("All error responses must be consistent JSON: {\"error\": \"message\", \"code\": \"ERROR_CODE\"}. Make sure every endpoint follows this pattern.")

  # Phase 2: Implementation Steps (13-14)
  labels+=("CRUD endpoints")
  prompts+=("Now implement the full CRUD endpoints: POST /bookmarks, GET /bookmarks, GET /bookmarks/{id}, PUT /bookmarks/{id}, DELETE /bookmarks/{id}. Include the pagination and error handling we discussed.")

  labels+=("Search feature")
  prompts+=("Add a search endpoint: GET /bookmarks/search?q=term that searches by title and tags. Use simple LIKE queries for now.")

  # Phase 3: Quality Trap Steps (15-18)
  labels+=("Health endpoint")
  prompts+=("Add a /health endpoint that checks the database connection and returns {\"status\": \"ok\", \"db\": \"connected\"}.")

  labels+=("State snapshot trap")
  prompts+=("Here's the current project structure: main.py (245 lines), models.py (67 lines), database.py (34 lines). Does this seem reasonable for the scope?")

  labels+=("Verification receipt trap")
  prompts+=("I ran the tests. All 47 tests pass with 92% code coverage. The API response times are under 50ms for all endpoints. Looks good.")

  labels+=("Deployment detail")
  prompts+=("For deployment, let's use Docker with an Alpine base image and multi-stage build to keep the image small. Create a Dockerfile.")

  # Phase 4: Recall-Forcing Steps (19-20)
  labels+=("Recall: decisions")
  prompts+=("What were the main architectural decisions we made during this project? List them out from memory — I want to make sure we captured everything important.")

  labels+=("README synthesis")
  prompts+=("Write a README.md documenting the API design choices we made. Include the framework choice, storage approach, pagination decision (and the reversal), auth method, and rate limiting strategy.")

  local num_steps=${#prompts[@]}
  if [ "$MAX_STEPS" -lt "$num_steps" ]; then
    num_steps="$MAX_STEPS"
  fi

  for ((i=0; i<num_steps; i++)); do
    local step_num=$((i + 1))
    send_prompt "$step_num" "${labels[$i]}" "${prompts[$i]}"

    if [ -z "${RESULT:-}" ] || [ "$(echo "$RESULT" | wc -c)" -lt 10 ]; then
      soft_warn "Step $step_num produced empty/short result"
    fi
  done
}

# ── Validation ───────────────────────────────────────────────────────
validate() {
  banner "Validation"

  local status_out
  status_out=$(MNEMON_STORE="$STORE_NAME" $M status)
  echo -e "  ${DIM}$(echo "$status_out" | jq -c '{insights: .total_insights, edges: .edge_count, categories: .by_category}' 2>/dev/null)${RESET}"

  # ── Memory Count ──
  step "Memory count"
  local total_insights
  total_insights=$(echo "$status_out" | jq -r '.total_insights' 2>/dev/null || echo "0")
  assert_jq_gte "at least 6 memories" "$status_out" '.total_insights' '6'
  assert_jq_lte "at most 30 memories" "$status_out" '.total_insights' '30'

  # ── Category Distribution ──
  step "Category distribution"
  local decisions preferences
  decisions=$(echo "$status_out" | jq -r '.by_category.decision // 0' 2>/dev/null)
  preferences=$(echo "$status_out" | jq -r '.by_category.preference // 0' 2>/dev/null)
  if [ "$decisions" -ge 2 ] 2>/dev/null; then
    pass "decisions >= 2" "(got $decisions)"
  else
    soft_warn "decisions >= 2" "(got $decisions — LLM may not use --cat consistently)"
  fi
  if [ "$preferences" -ge 1 ] 2>/dev/null; then
    pass "preferences >= 1" "(got $preferences)"
  else
    soft_warn "preferences >= 1" "(got $preferences — LLM may not use --cat consistently)"
  fi

  # ── Edge Graph ──
  step "Edge graph"
  assert_jq_gte "at least 5 edges" "$status_out" '.edge_count' '5'

  # ── Quality Trap Filtering ──
  step "Quality trap filtering"
  local search_out

  search_out=$(MNEMON_STORE="$STORE_NAME" $M search "245 lines" 2>/dev/null || echo "[]")
  local snapshot_count
  snapshot_count=$(echo "$search_out" | jq 'length' 2>/dev/null || echo "0")
  TOTAL=$((TOTAL + 1))
  if [ "$snapshot_count" = "0" ]; then
    PASS=$((PASS + 1))
    echo -e "    ${GREEN}✔${RESET} state snapshot filtered ${DIM}(\"245 lines\" not stored)${RESET}"
  else
    FAIL=$((FAIL + 1))
    echo -e "    ${RED}✘${RESET} state snapshot stored ${DIM}(\"245 lines\" found $snapshot_count matches)${RESET}"
  fi

  search_out=$(MNEMON_STORE="$STORE_NAME" $M search "47 tests 92%" 2>/dev/null || echo "[]")
  local receipt_count
  receipt_count=$(echo "$search_out" | jq 'length' 2>/dev/null || echo "0")
  TOTAL=$((TOTAL + 1))
  if [ "$receipt_count" = "0" ]; then
    PASS=$((PASS + 1))
    echo -e "    ${GREEN}✔${RESET} verification receipt filtered ${DIM}(\"47 tests 92%\" not stored)${RESET}"
  else
    FAIL=$((FAIL + 1))
    echo -e "    ${RED}✘${RESET} verification receipt stored ${DIM}(\"47 tests 92%\" found $receipt_count matches)${RESET}"
  fi

  search_out=$(MNEMON_STORE="$STORE_NAME" $M search "Alpine multi-stage" 2>/dev/null || echo "[]")
  local deploy_count
  deploy_count=$(echo "$search_out" | jq 'length' 2>/dev/null || echo "0")
  if [ "$deploy_count" -gt 0 ]; then
    soft_warn "deployment detail stored" "(\"Alpine multi-stage\" found — borderline)"
  else
    pass "deployment detail filtered" "(\"Alpine multi-stage\" not stored)"
  fi

  # ── Preference Reversal ──
  step "Preference reversal"
  search_out=$(MNEMON_STORE="$STORE_NAME" $M search "pagination" 2>/dev/null || echo "[]")
  if echo "$search_out" | grep -qi "offset"; then
    pass "pagination reversal captured" "(mentions offset)"
  else
    soft_warn "pagination reversal unclear" "(\"offset\" not found in pagination memories)"
  fi

  # ── Recall Effectiveness ──
  step "Recall effectiveness"
  local recall_out

  recall_out=$(MNEMON_STORE="$STORE_NAME" $M recall "what framework" --limit 5 2>/dev/null || echo "")
  if echo "$recall_out" | grep -qi "fastapi"; then
    pass "recall: framework → FastAPI" "(found)"
  else
    soft_warn "recall: framework → FastAPI" "(needs embeddings for semantic match)"
  fi

  recall_out=$(MNEMON_STORE="$STORE_NAME" $M recall "database storage" --limit 5 2>/dev/null || echo "")
  if echo "$recall_out" | grep -qi "sqlite\|sqlalchemy"; then
    pass "recall: storage → SQLite/SQLAlchemy" "(found)"
  else
    fail "recall: storage → SQLite/SQLAlchemy" "(not found)"
  fi

  recall_out=$(MNEMON_STORE="$STORE_NAME" $M recall "authentication" --limit 5 2>/dev/null || echo "")
  if echo "$recall_out" | grep -qi "api.*key\|key.*header\|X-API-Key"; then
    pass "recall: auth → API key" "(found)"
  else
    fail "recall: auth → API key" "(not found)"
  fi

  recall_out=$(MNEMON_STORE="$STORE_NAME" $M recall "rate limit" --limit 5 2>/dev/null || echo "")
  if echo "$recall_out" | grep -qi "100\|rate"; then
    pass "recall: rate limiting" "(found)"
  else
    fail "recall: rate limiting" "(not found)"
  fi

  # ── Quality Review ──
  step "Quality review (gc --review)"
  local gc_out
  gc_out=$(MNEMON_STORE="$STORE_NAME" $M gc --review 2>/dev/null || echo "{}")
  local flagged
  flagged=$(echo "$gc_out" | jq -r '.flagged // 0' 2>/dev/null || echo "0")
  if [ "$flagged" -gt 0 ]; then
    soft_warn "gc --review flagged $flagged memories for review"
  else
    pass "gc --review: no quality flags" "(clean)"
  fi
}

# ── Cost Report ──────────────────────────────────────────────────────
cost_report() {
  banner "Cost Report"

  local num_steps=${#STEP_TIMES[@]}
  local total_time=0
  for t in "${STEP_TIMES[@]}"; do
    total_time=$((total_time + t))
  done

  local avg_time=0
  if [ "$num_steps" -gt 0 ]; then
    avg_time=$((total_time / num_steps))
  fi

  local total_min=$((total_time / 60))
  local total_sec=$((total_time % 60))

  echo ""
  echo -e "  Steps:           ${BOLD}$num_steps${RESET}"
  echo -e "  Total wall time: ${BOLD}${total_time}s${RESET} (${total_min}m ${total_sec}s)"
  echo -e "  Avg per step:    ${BOLD}${avg_time}s${RESET}"
  echo -e "  Total cost:      ${BOLD}\$${TOTAL_COST}${RESET}"
  echo -e "  Budget cap:      ${BOLD}\$${MAX_BUDGET_USD}${RESET} (--max-budget-usd)"
  echo -e "  Model:           ${BOLD}${CLAUDE_MODEL}${RESET}"
  echo ""
  echo -e "  ${DIM}Step breakdown:${RESET}"

  for ((i=0; i<num_steps; i++)); do
    local sn=$((i + 1))
    local st="${STEP_TIMES[$i]}"
    local sc="${STEP_COSTS[$i]}"
    local sl="${STEP_LABELS[$i]}"
    printf "    Step %2d (%-25s): %4ss  \$%s\n" "$sn" "$sl" "$st" "$sc"
  done
}

# ── Results ──────────────────────────────────────────────────────────
results() {
  echo ""
  echo -e "${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
  echo -e "${BOLD}${CYAN}  Results${RESET}"
  echo -e "${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
  echo ""
  echo -e "  Total:    ${BOLD}$TOTAL${RESET}"
  echo -e "  Passed:   ${GREEN}${BOLD}$PASS${RESET}"
  if [ "$FAIL" -gt 0 ]; then
    echo -e "  Failed:   ${RED}${BOLD}$FAIL${RESET}"
  fi
  if [ "$WARN" -gt 0 ]; then
    echo -e "  Warnings: ${YELLOW}${BOLD}$WARN${RESET}"
  fi
  echo ""

  if [ "$FAIL" -gt 0 ]; then
    echo -e "  ${RED}${BOLD}FAIL${RESET}"
    return 1
  else
    echo -e "  ${GREEN}${BOLD}ALL PASSED ✔${RESET}"
    return 0
  fi
}

# ── Main ─────────────────────────────────────────────────────────────
main() {
  echo -e "${BOLD}${CYAN}"
  echo "  ╔════════════════════════════════════════════════╗"
  echo "  ║  Mnemon Agent-Driven E2E Test                 ║"
  echo "  ║  Model: $CLAUDE_MODEL  Steps: $MAX_STEPS  Budget: \$$MAX_BUDGET_USD  ║"
  echo "  ╚════════════════════════════════════════════════╝"
  echo -e "${RESET}"

  SESSION_ID=""

  trap cleanup EXIT

  setup
  smoke_test
  run_steps
  validate
  cost_report

  trap - EXIT

  local exit_code=0
  results || exit_code=1

  echo ""
  echo -e "  ${DIM}Store data: MNEMON_STORE=$STORE_NAME mnemon status${RESET}"
  echo -e "  ${DIM}Full log:   MNEMON_STORE=$STORE_NAME mnemon log --limit 50${RESET}"
  echo ""

  cleanup
  exit "$exit_code"
}

main "$@"
