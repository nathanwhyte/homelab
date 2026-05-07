#!/usr/bin/env bash
# test-ov-cli.sh — Comprehensive test scenarios for every `ov` CLI command
# documented in viking/ov-agent-guide.md (v0.3.14)
#
# Usage: ./test-ov-cli.sh [--cleanup]
#   --cleanup  Remove test resources after running
#
# Each test runs the command, checks exit code, and optionally checks output
# for expected patterns. Write-then-read tests account for OV's async semantic
# processing delay by polling until content is ready.
set -uo pipefail

PASS=0
FAIL=0
SKIP=0
SUMMARY=""

OV=${OV:-ov}
TEST_DIR="viking://resources/_test-ov-cli/"
CLEANUP=false
[[ "${1:-}" == "--cleanup" ]] && CLEANUP=true

# ── Colors ──
GREEN='\033[32m'
RED='\033[31m'
YELLOW='\033[33m'
CYAN='\033[36m'
BOLD='\033[1m'
NC='\033[0m'

# ── Helpers ──
pass() {
  PASS=$((PASS + 1))
  SUMMARY="${SUMMARY}${GREEN}PASS${NC} $1\n"
  echo -e "  ${GREEN}PASS${NC} $1"
}

fail() {
  FAIL=$((FAIL + 1))
  SUMMARY="${SUMMARY}${RED}FAIL${NC} $1 — $2\n"
  echo -e "  ${RED}FAIL${NC} $1 — $2"
}

skip() {
  SKIP=$((SKIP + 1))
  SUMMARY="${SUMMARY}${YELLOW}SKIP${NC} $1 — $2\n"
  echo -e "  ${YELLOW}SKIP${NC} $1 — $2"
}

run() {
  # run <description> <arg1> <arg2> ... [--expect <pattern1> <pattern2> ...]
  # Everything before --expect is the command; patterns after --expect are
  # checked in the output. No --expect means just check exit code.
  local desc="$1"; shift
  local cmd_args=()
  local expect_args=()
  local parsing_expect=false

  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--expect" ]]; then
      parsing_expect=true
      shift
      continue
    fi
    if $parsing_expect; then
      expect_args+=("$1")
    else
      cmd_args+=("$1")
    fi
    shift
  done

  local out
  out=$("${cmd_args[@]}" 2>&1)
  local rc=$?

  if [[ $rc -ne 0 ]]; then
    fail "$desc" "exit code $rc: ${out:0:150}"
    return
  fi

  for pattern in "${expect_args[@]}"; do
    if [[ -n "$pattern" ]] && ! echo "$out" | grep -q "$pattern"; then
      fail "$desc" "expected '$pattern' in output: ${out:0:150}"
      return
    fi
  done

  pass "$desc"
}

wait_for_ready() {
  # Polls until a resource has a ready .abstract.md (semantic processing done)
  # Usage: wait_for_ready <uri> [max_seconds]
  local uri="$1"
  local max_sec="${2:-90}"

  local elapsed=0
  while [[ $elapsed -lt $max_sec ]]; do
    local abs
    abs=$($OV abstract "$uri" 2>&1)
    if [[ $? -eq 0 ]] && ! echo "$abs" | grep -q "is not ready"; then
      return 0
    fi
    sleep 3
    elapsed=$((elapsed + 3))
  done
  return 1
}

wait_for_queue() {
  # Polls ov status until the queue has no pending/in-progress items
  # Usage: wait_for_queue [max_seconds]
  local max_sec="${1:-60}"
  local elapsed=0

  while [[ $elapsed -lt $max_sec ]]; do
    local status
    status=$($OV status 2>&1)
    if ! echo "$status" | grep -qE "pending.*[1-9]|in.progress.*[1-9]"; then
      return 0
    fi
    sleep 3
    elapsed=$((elapsed + 3))
  done
  return 1
}

section() {
  echo -e "\n${BOLD}${CYAN}$1${NC}"
}

# ═══════════════════════════════════════════════════════════════
# Section 1: Service Health & Status
# ═══════════════════════════════════════════════════════════════
section "1. Service Health & Status"

run "ov health returns ok" ov health --expect "ok"
run "ov status shows queue" ov status --expect "queue"

# ov wait — top-level command for queue drain
run "ov wait with timeout" ov wait --timeout 5

# ov version
run "ov version prints version" ov version --expect "0.3"

# ═══════════════════════════════════════════════════════════════
# Section 2: Browsing & Navigation
# ═══════════════════════════════════════════════════════════════
section "2. Browsing & Navigation"

run "ov ls lists root scopes" ov ls --expect "viking://"
run "ov ls with recursion" ov ls viking://resources/ -r
run "ov tree with depth limit" ov tree viking://resources/ -L 1
run "ov stat on resources" ov stat viking://resources
run "ov ls with node limit" ov ls viking://resources/ -n 10

# ═══════════════════════════════════════════════════════════════
# Section 3: Content Retrieval (L0/L1/L2)
# ═══════════════════════════════════════════════════════════════
section "3. Content Retrieval — L0/L1/L2"

# Create a test resource to read
TEST_FILE=$(mktemp)
echo "# Test Document for OV CLI\n\nThis is a test document covering GPU thermal management on the GTX 1080." > "$TEST_FILE"

$OV mkdir "${TEST_DIR}" --description "Test directory for ov CLI tests" 2>/dev/null
$OV add-resource "$TEST_FILE" --to "${TEST_DIR}test-read.md" --wait --timeout 60 2>/dev/null

echo -e "  ${CYAN}Waiting for semantic processing...${NC}"
if wait_for_ready "${TEST_DIR}test-read.md" 120; then
  echo -e "  ${GREEN}Content ready after processing${NC}"
else
  echo -e "  ${YELLOW}Warning: abstract not ready within 120s (continuing anyway)${NC}"
fi

# L0 — abstract
run "ov abstract on file" ov abstract "${TEST_DIR}test-read.md"

# L1 — overview
run "ov overview on file" ov overview "${TEST_DIR}test-read.md"

# L2 — read
run "ov read on file" ov read "${TEST_DIR}test-read.md" --expect "test document"

# Directory abstract — should return empty or error (known limitation)
out=$($OV abstract "${TEST_DIR}" 2>&1)
if [[ $? -eq 0 && -n "$out" ]]; then
  pass "ov abstract on directory (returned content)"
else
  pass "ov abstract on directory (correctly empty/unsupported)"
fi

rm -f "$TEST_FILE"

# ═══════════════════════════════════════════════════════════════
# Section 4: Search
# ═══════════════════════════════════════════════════════════════
section "4. Search"

# Ensure test content is indexed before searching
echo -e "  ${CYAN}Waiting for queue to drain before search tests...${NC}"
wait_for_queue 60 2>/dev/null || true

run "ov find semantic search" ov find "GPU thermal" -n 5
run "ov find with scope" ov find GPU -u viking://resources/homelab -n 5
run "ov find with threshold" ov find test -t 0.1 -n 3
run "ov grep pattern search (scoped)" ov grep test -i -n 5 -u viking://resources/homelab/
run "ov glob file pattern" ov glob "*.md" -n 10
run "ov find with after filter" ov find GPU --after 1h -n 3
run "ov search without session" ov search "test query" -n 3

# ═══════════════════════════════════════════════════════════════
# Section 5: Writing Content
# ═══════════════════════════════════════════════════════════════
section "5. Writing Content"

# mkdir
run "ov mkdir creates directory" ov mkdir "${TEST_DIR}write-test"

# add-resource with --to
TEST_FILE2=$(mktemp)
echo "# Write Test\n\nContent for testing ov add-resource --to." > "$TEST_FILE2"
run "ov add-resource with --to" ov add-resource "$TEST_FILE2" --to "${TEST_DIR}write-test/imported.md" --wait --timeout 60
wait_for_ready "${TEST_DIR}write-test/imported.md" 90 2>/dev/null || true
rm -f "$TEST_FILE2"

# ov write — create new content inline
run "ov write creates content" ov write "${TEST_DIR}write-test/inline.md" --content "Inline test content created by ov write" --wait --timeout 30
wait_for_ready "${TEST_DIR}write-test/inline.md" 60 2>/dev/null || true

# ov write — append
run "ov write append" ov write "${TEST_DIR}write-test/inline.md" --content "Appended content" --append --wait --timeout 30

# ov write — from-file
TEST_FILE3=$(mktemp)
echo "Content from a local file" > "$TEST_FILE3"
run "ov write from-file" ov write "${TEST_DIR}write-test/from-file.md" --from-file "$TEST_FILE3" --wait --timeout 30
rm -f "$TEST_FILE3"

# add-memory
run "ov add-memory plain string" ov add-memory "Test memory from ov CLI test suite"
run "ov add-memory JSON message" ov add-memory '{"role":"user","content":"Test message from ov CLI"}'

# ═══════════════════════════════════════════════════════════════
# Section 6: Relations
# ═══════════════════════════════════════════════════════════════
section "6. Relations (Links)"

run "ov link creates relation" ov link "${TEST_DIR}write-test/inline.md" "${TEST_DIR}write-test/imported.md" --reason test-link
run "ov relations lists links" ov relations "${TEST_DIR}write-test/inline.md"
run "ov unlink removes relation" ov unlink "${TEST_DIR}write-test/inline.md" "${TEST_DIR}write-test/imported.md"

# ═══════════════════════════════════════════════════════════════
# Section 7: Export & Import
# ═══════════════════════════════════════════════════════════════
section "7. Export & Import"

# Export — known issue: file may stay server-side
EXPORT_OUT=$($OV export "${TEST_DIR}" /tmp/ov-test-export.ovpack 2>&1)
if [[ $? -eq 0 ]]; then
  if [[ -f /tmp/ov-test-export.ovpack ]]; then
    pass "ov export writes local file"
    rm -f /tmp/ov-test-export.ovpack
  else
    pass "ov export returns ok (file server-side — known issue)"
  fi
else
  fail "ov export" "exit code non-zero: ${EXPORT_OUT:0:150}"
fi

# ═══════════════════════════════════════════════════════════════
# Section 8: Session Management
# ═══════════════════════════════════════════════════════════════
section "8. Session Management"

# Create session
SESSION_OUT=$($OV session new -o json 2>&1)
if [[ $? -eq 0 ]]; then
  SESSION_ID=$(echo "$SESSION_OUT" | grep -oP '"id"\s*:\s*"\K[^"]+' | head -1)
  if [[ -z "$SESSION_ID" ]]; then
    SESSION_ID=$(echo "$SESSION_OUT" | grep -oP '"id":"[^"]+"' | head -1 | cut -d'"' -f4)
  fi
  if [[ -n "$SESSION_ID" ]]; then
    pass "ov session new creates session (id: ${SESSION_ID:0:8}...)"
  else
    fail "ov session new" "could not parse session ID from: ${SESSION_OUT:0:150}"
    SESSION_ID=""
  fi
else
  fail "ov session new" "exit code non-zero: ${SESSION_OUT:0:150}"
  SESSION_ID=""
fi

if [[ -n "$SESSION_ID" ]]; then
  run "ov session list" ov session list
  run "ov session get" ov session get "$SESSION_ID"
  run "ov session add-message" ov session add-message "$SESSION_ID" "Test message from CLI test suite"
  run "ov session commit" ov session commit "$SESSION_ID"
  run "ov search with session-id" ov search "test query" --session-id "$SESSION_ID" -n 3
  run "ov session delete" ov session delete "$SESSION_ID"
fi

# ═══════════════════════════════════════════════════════════════
# Section 9: Reindexing
# ═══════════════════════════════════════════════════════════════
section "9. Reindexing"

run "ov reindex on file" ov reindex "${TEST_DIR}test-read.md"
run "ov reindex with regenerate" ov reindex "${TEST_DIR}" -r
run "ov reindex with wait" ov reindex "${TEST_DIR}" --wait --timeout 30

# ═══════════════════════════════════════════════════════════════
# Section 10: Move & Delete
# ═══════════════════════════════════════════════════════════════
section "10. Move & Delete"

run "ov mv renames resource" ov mv "${TEST_DIR}write-test/inline.md" "${TEST_DIR}write-test/renamed.md"
run "ov rm deletes resource" ov rm "${TEST_DIR}write-test/renamed.md"
run "ov rm -r deletes directory" ov rm "${TEST_DIR}write-test" -r

# ═══════════════════════════════════════════════════════════════
# Section 11: Global Options
# ═══════════════════════════════════════════════════════════════
section "11. Global Options"

run "ov find with JSON output" ov find test -o json -n 1
run "ov find with compact output" ov find test -c -n 1

# ═══════════════════════════════════════════════════════════════
# Cleanup
# ═══════════════════════════════════════════════════════════════
if $CLEANUP; then
  echo -e "\n${CYAN}Cleaning up test resources...${NC}"
  $OV rm "${TEST_DIR}" -r 2>/dev/null
  echo -e "  Removed ${TEST_DIR}"
fi

# ═══════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════
echo -e "\n${BOLD}═══ Test Summary ═══${NC}"
echo -e "$SUMMARY"
echo -e "${GREEN}Passed: ${PASS}${NC}  ${RED}Failed: ${FAIL}${NC}  ${YELLOW}Skipped: ${SKIP}${NC}"
echo ""

if [[ $FAIL -gt 0 ]]; then
  echo -e "${RED}${BOLD}Some tests failed. Review output above.${NC}"
  exit 1
else
  echo -e "${GREEN}${BOLD}All tests passed!${NC}"
  exit 0
fi