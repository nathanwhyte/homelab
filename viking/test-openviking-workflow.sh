#!/usr/bin/env bash
# test-openviking-workflow.sh
# Tests the OpenViking workflow through the 'ov' CLI.
# Covers: connectivity, browsing, content retrieval, search, export.
set -uo pipefail

PASS=0
FAIL=0
SUMMARY=""

OV=${OV:-ov}
BASE=${OV_BASE_URI:-viking://}

# ── Colors ──
GREEN='\033[32m'
RED='\033[31m'
YELLOW='\033[33m'
CYAN='\033[36m'
BOLD='\033[1m'
NC='\033[0m'

step()  { printf "\n${CYAN}━━━ ${BOLD}%s${NC}${CYAN} ━━━${NC}\n" "$1"; }
ok()    { PASS=$((PASS+1)); printf "  ${GREEN}✓${NC} %s\n" "$1"; }
fail()  { FAIL=$((FAIL+1)); printf "  ${RED}✗${NC} %s\n" "$1"; }
info()  { printf "  ${YELLOW}ℹ${NC} %s\n" "$1"; }

# ── 1. Connectivity ──
step "1. Connectivity & Health"

OUT=$($OV health 2>&1)
if echo "$OUT" | grep -q "status.*ok"; then
    ok "health check returns ok"
else
    fail "health check failed: $OUT"
fi

WORKER_COUNT=$(echo "$OUT" | awk '/healthy_count/ {print $NF}')
if [ -n "$WORKER_COUNT" ] && [ "$WORKER_COUNT" -ge 2 ]; then
    ok "workers healthy: $WORKER_COUNT/3"
else
    fail "worker count low: $WORKER_COUNT"
fi

if echo "$OUT" | grep -q '"active":\s*true'; then
    ok "merged read endpoint active"
else
    fail "merged read endpoint not active"
fi

OUT=$($OV status 2>&1)
if echo "$OUT" | grep -q "system.*(healthy)"; then
    ok "status: system healthy"
else
    fail "status unhealthy: $OUT"
fi

# ── 2. Browsing ──
step "2. Browsing & Directory Listing"

OUT=$($OV ls 2>&1)
for scope in agent resources session user temp; do
    if echo "$OUT" | grep -q "$scope"; then
        ok "ls shows scope: $scope"
    else
        fail "ls missing scope: $scope"
    fi
done

OUT=$($OV ls "$BASE"resources 2>&1)
if echo "$OUT" | grep -q "projects"; then
    ok "ls resources/ shows projects"
else
    fail "ls resources/ failed: $OUT"
fi

OUT=$($OV stat "$BASE"resources 2>&1)
if echo "$OUT" | grep -q "isDir.*true\|true.*isDir"; then
    ok "stat confirms resources/ is directory"
else
    fail "stat failed: $OUT"
fi

# ── 3. Content Retrieval ──
step "3. Content Retrieval (L0/L1/L2)"

# Try reading a known resource
PROJECTS=$($OV ls "$BASE"resources/projects 2>&1 | grep -oP 'viking://[^ ]+' | head -5 || true)
if [ -n "$PROJECTS" ]; then
    info "found projects: $(echo "$PROJECTS" | tr '\n' ' ')"
    # Skip the directory entry itself; use the first actual project
    FIRST_PROJECT=""
    for uri in $PROJECTS; do
        if [ "$uri" != "viking://resources/projects" ]; then
            FIRST_PROJECT="$uri"
            break
        fi
    done
    if [ -z "$FIRST_PROJECT" ]; then
        FIRST_PROJECT=$(echo "$PROJECTS" | tail -1)
    fi
    info "reading abstract for: $FIRST_PROJECT"

    if ABSTRACT=$($OV abstract "$FIRST_PROJECT" 2>&1); then
        ok "abstract (L0) read for $(basename "$FIRST_PROJECT")"
    else
        fail "abstract read failed: $ABSTRACT"
    fi
else
    info "no projects under resources/projects, checking agent scope"
    AGENT_ITEMS=$($OV ls "$BASE"agent 2>&1 | grep -oP 'viking://[^ ]+' | head -3 || true)
    if [ -n "$AGENT_ITEMS" ]; then
        FIRST_AGENT=$(echo "$AGENT_ITEMS" | head -1)
        if ABSTRACT=$($OV abstract "$FIRST_AGENT" 2>&1); then
            ok "abstract (L0) read for agent item"
        else
            fail "abstract read failed: $ABSTRACT"
        fi
    fi
fi

# ── 4. Search ──
step "4. Search & Retrieval"

OUT=$($OV find "homelab" -n 5 2>&1)
if echo "$OUT" | grep -qi "result\|node\|uri\|score"; then
    ok "find: semantic search returns results"
else
    fail "find returned no results: $OUT"
fi

OUT=$($OV search "kubernetes cluster" -n 5 2>&1)
if echo "$OUT" | grep -qi "result\|node\|uri\|score"; then
    ok "search: context-aware search returns results"
else
    fail "search returned no results: $OUT"
fi

OUT=$($OV grep "kubernetes\|k3s" -i -n 5 2>&1)
if echo "$OUT" | grep -qi "result\|node\|uri\|match"; then
    ok "grep: pattern search returns matches"
else
    fail "grep returned no matches: $OUT"
fi

# ── 5. Agent scope ──
step "5. Agent Scope Navigation"

OUT=$($OV ls "$BASE"agent 2>&1)
if echo "$OUT" | grep -q "memories\|instructions\|skills"; then
    ok "agent/ contains subdirectories"
else
    info "agent/ contents: $OUT"
fi

# ── 6. Export (read-only) ──
step "6. Export (read-only test)"

TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

OUT=$($OV export "$BASE"resources "$TMPDIR/test-export.ovpack" -o json 2>&1)
if echo "$OUT" | grep -q '"ok":\s*true'; then
    if [ -f "$TMPDIR/test-export.ovpack" ]; then
        SIZE=$(stat --format=%s "$TMPDIR/test-export.ovpack" 2>/dev/null || stat -f%z "$TMPDIR/test-export.ovpack" 2>/dev/null)
        ok "export created .ovpack ($SIZE bytes)"
    else
        # Server-side export: API reports success but file may not write locally
        info "export API ok (file may be server-side: $(echo "$OUT" | grep -oP '"file":"[^"]*"'))"
    fi
else
    fail "export failed: $OUT"
fi

# ── Summary ──
step "Results"
printf "  ${GREEN}Passed:${NC} %d\n" "$PASS"
printf "  ${RED}Failed:${NC} %d\n" "$FAIL"
TOTAL=$((PASS+FAIL))
printf "  ${CYAN}Total:${NC}  %d\n" "$TOTAL"

if [ "$FAIL" -eq 0 ]; then
    printf "\n  ${GREEN}${BOLD}All tests passed.${NC}\n"
    exit 0
else
    printf "\n  ${RED}${BOLD}${FAIL} test(s) failed.${NC}\n"
    exit 1
fi
