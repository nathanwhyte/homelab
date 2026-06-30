#!/usr/bin/env bash
# check-bare-fences.sh — Reject bare code fences (```) that lack a language tag.
# This catches MD040 violations before they reach markdownlint, giving a clear
# actionable error message: "Every code block must specify a language."
#
# Usage: check-bare-fences.sh [file ...]
# If no files are given, scans all staged .md files.
set -euo pipefail

if [ $# -eq 0 ]; then
    files=$(git diff --cached --name-only --diff-filter=ACMR -- '*.md')
else
    files=("$@")
fi

if [ -z "$files" ]; then
    exit 0
fi

bare=0
for f in $files; do
    [ -f "$f" ] || continue
    # Track code block state: every line matching ^``` toggles in/out of a code block.
    # Opening fences (odd count) MUST have a language tag.
    # Closing fences (even count) are always bare — that's correct.
    linenr=0
    fence_count=0
    bad_lines=""
    while IFS= read -r line; do
        linenr=$((linenr + 1))
        # Match ANY fence line (``` with or without a language tag)
        if echo "$line" | grep -qE '^```'; then
            fence_count=$((fence_count + 1))
            # Odd count = opening fence — must have a language tag
            if [ $((fence_count % 2)) -eq 1 ]; then
                # Check if it's bare (exactly ``` with no language after it)
                if echo "$line" | grep -qE '^```[[:space:]]*$'; then
                    bad_lines="${bad_lines}  ${linenr}:${line}"$'\n'
                fi
            fi
        fi
    done < "$f"
    if [ -n "$bad_lines" ]; then
        echo "ERROR: $f has bare opening code fences (no language tag):"
        printf '%s' "$bad_lines"
        echo "  Fix: change \`\`\` to \`\`\`text (or the appropriate language)."
        bare=$((bare + 1))
    fi
done

if [ "$bare" -gt 0 ]; then
    echo ""
    echo "Found bare code fences in $bare file(s). Every \`\`\` fence must have a language tag."
    echo "Use \`\`\`text as the fallback for untyped content. See CLAUDE.md § Code Fence Language Tags."
    exit 1
fi

exit 0
