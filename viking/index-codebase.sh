#!/usr/bin/env bash
# index-codebase.sh — Sequential OpenViking indexer for a codebase
#
# Walks a directory, uploads each file to OpenViking one-by-one,
# waits for semantic processing to complete before moving on.
# Avoids lock contention from parallel uploads.
#
# Usage:
#   ./index-codebase.sh <source-dir> <viking-target-dir> [options]
#
# Examples:
#   ./index-codebase.sh ~/code/homelab viking://resources/projects/homelab
#   ./index-codebase.sh ~/code/homelab viking://resources/projects/homelab --dry-run
#   ./index-codebase.sh ~/code/homelab viking://resources/projects/homelab --ext "yaml,sh,py"
#   ./index-codebase.sh ~/code/homelab viking://resources/projects/homelab --subdir llama
#
# Environment:
#   OPENVIKING_URL     (default: https://context.nathanwhyte.dev)
#   OPENVIKING_KEY     (required)
#   OPENVIKING_ACCOUNT (default: default)
#   OPENVIKING_USER    (default: noot)

set -euo pipefail

# ── Config ──────────────────────────────────────────────────────────

OV_URL="${OPENVIKING_URL:-https://context.nathanwhyte.dev}"
OV_KEY="${OPENVIKING_KEY:?Set OPENVIKING_KEY}"
OV_ACCOUNT="${OPENVIKING_ACCOUNT:-default}"
OV_USER="${OPENVIKING_USER:-noot}"

# Directories to skip during walk
SKIP_DIRS=".git .venv venv node_modules dist build target __pycache__ .mypy_cache .ruff_cache .cache .idea .vscode .cursor"

# File extensions to index (empty = all text files)
DEFAULT_EXTENSIONS="yaml,yml,sh,py,json,md,toml,cfg,conf,ini,txt,go,rs,ts,js,jsx,tsx,sql,tf,hcl,Dockerfile"

# Files to skip
SKIP_FILES=".DS_Store Thumbs.db .gitignore .gitattributes package-lock.json yarn.lock pnpm-lock.yaml Cargo.lock poetry.lock .index-cache.json"

# File patterns to skip (glob-style matching on filename)
SKIP_PATTERNS="*.secret.yaml *.secret.yaml.example *.env *.env.* *.key *.pem *.crt credentials*"

# Path patterns to skip (vendored/upstream content)
SKIP_PATHS="garage/garage/doc garage/garage/src garage/garage/script"

# Max file size (bytes) — skip files larger than this
MAX_FILE_SIZE=102400  # 100KB

# Delay between uploads (seconds) — gives semantic queue breathing room
UPLOAD_DELAY=2

# ── Parse args ──────────────────────────────────────────────────────

SOURCE_DIR=""
VIKING_TARGET=""
DRY_RUN=false
EXTENSIONS="$DEFAULT_EXTENSIONS"
SUBDIR=""
VERBOSE=false

usage() {
    echo "Usage: $0 <source-dir> <viking-target-dir> [options]"
    echo ""
    echo "Options:"
    echo "  --dry-run       Show what would be indexed without uploading"
    echo "  --ext EXT       Comma-separated file extensions (default: $DEFAULT_EXTENSIONS)"
    echo "  --subdir DIR    Only index files under this subdirectory"
    echo "  --delay SECS    Delay between uploads (default: $UPLOAD_DELAY)"
    echo "  --max-size N    Max file size in bytes (default: $MAX_FILE_SIZE)"
    echo "  --verbose       Show detailed output"
    echo "  --help          Show this help"
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)  DRY_RUN=true; shift ;;
        --ext)      EXTENSIONS="$2"; shift 2 ;;
        --subdir)   SUBDIR="$2"; shift 2 ;;
        --delay)    UPLOAD_DELAY="$2"; shift 2 ;;
        --max-size) MAX_FILE_SIZE="$2"; shift 2 ;;
        --verbose)  VERBOSE=true; shift ;;
        --help|-h)  usage ;;
        -*)         echo "Unknown option: $1"; usage ;;
        *)
            if [[ -z "$SOURCE_DIR" ]]; then
                SOURCE_DIR="$1"
            elif [[ -z "$VIKING_TARGET" ]]; then
                VIKING_TARGET="$1"
            else
                echo "Too many arguments"; usage
            fi
            shift ;;
    esac
done

[[ -z "$SOURCE_DIR" || -z "$VIKING_TARGET" ]] && usage

# Resolve source dir
SOURCE_DIR="$(cd "$SOURCE_DIR" && pwd)"
[[ -d "$SOURCE_DIR" ]] || { echo "ERROR: Source directory not found: $SOURCE_DIR"; exit 1; }

# Apply subdir filter
WALK_DIR="$SOURCE_DIR"
if [[ -n "$SUBDIR" ]]; then
    WALK_DIR="$SOURCE_DIR/$SUBDIR"
    [[ -d "$WALK_DIR" ]] || { echo "ERROR: Subdirectory not found: $WALK_DIR"; exit 1; }
fi

# ── Helpers ─────────────────────────────────────────────────────────

should_skip_dir() {
    local dir_name="$1"
    for skip in $SKIP_DIRS; do
        [[ "$dir_name" == "$skip" ]] && return 0
    done
    return 1
}

should_skip_file() {
    local file_name="$1"
    for skip in $SKIP_FILES; do
        [[ "$file_name" == "$skip" ]] && return 0
    done
    for pattern in $SKIP_PATTERNS; do
        # shellcheck disable=SC2254
        case "$file_name" in $pattern) return 0 ;; esac
    done
    return 1
}

has_valid_extension() {
    local file="$1"
    local ext="${file##*.}"
    local base="$(basename "$file")"

    # Files without extensions (Dockerfile, Makefile, etc.)
    if [[ "$base" == "$ext" ]]; then
        case "$base" in
            Dockerfile|Makefile|Justfile|Procfile|Vagrantfile) return 0 ;;
            *) return 1 ;;
        esac
    fi

    IFS=',' read -ra EXTS <<< "$EXTENSIONS"
    for valid_ext in "${EXTS[@]}"; do
        [[ "$ext" == "$valid_ext" ]] && return 0
    done
    return 1
}

upload_file() {
    local file_path="$1"
    local viking_uri="$2"

    # Step 1: temp_upload
    local upload_resp
    upload_resp=$(curl -s -X POST "$OV_URL/api/v1/resources/temp_upload" \
        -H "X-API-Key: $OV_KEY" \
        -H "X-OpenViking-Account: $OV_ACCOUNT" \
        -H "X-OpenViking-User: $OV_USER" \
        -F "file=@$file_path;type=text/plain" \
        --max-time 30)

    local temp_path
    temp_path=$(echo "$upload_resp" | grep -o '"temp_path":"[^"]*"' | sed 's/"temp_path":"//;s/"//')

    if [[ -z "$temp_path" ]]; then
        echo "  UPLOAD_FAIL: could not get temp_path"
        $VERBOSE && echo "  Response: $upload_resp"
        return 1
    fi

    # Step 2: add as resource
    local add_resp
    add_resp=$(curl -s -X POST "$OV_URL/api/v1/resources" \
        -H "Content-Type: application/json" \
        -H "X-API-Key: $OV_KEY" \
        -H "X-OpenViking-Account: $OV_ACCOUNT" \
        -H "X-OpenViking-User: $OV_USER" \
        -d "{\"temp_path\":\"$temp_path\",\"to\":\"$viking_uri\"}" \
        --max-time 30)

    if echo "$add_resp" | grep -q '"status":"ok"'; then
        return 0
    else
        echo "  ADD_FAIL: $add_resp"
        return 1
    fi
}

# ── Main ────────────────────────────────────────────────────────────

echo "=== OpenViking Codebase Indexer ==="
echo "Source:  $WALK_DIR"
echo "Target:  $VIKING_TARGET"
echo "Extensions: $EXTENSIONS"
echo "Max size: $MAX_FILE_SIZE bytes"
echo "Delay: ${UPLOAD_DELAY}s between uploads"
$DRY_RUN && echo "Mode: DRY RUN"
echo ""

# Collect files
FILES=()
while IFS= read -r -d '' file; do
    # Get path relative to SOURCE_DIR
    rel_path="${file#$SOURCE_DIR/}"
    file_name="$(basename "$file")"
    file_size=$(stat -c%s "$file" 2>/dev/null || stat -f%z "$file" 2>/dev/null || echo 0)

    # Skip oversized files
    [[ $file_size -gt $MAX_FILE_SIZE ]] && continue

    # Skip unwanted files
    should_skip_file "$file_name" && continue

    # Skip vendored/upstream paths
    _skip=0
    for skip_path in $SKIP_PATHS; do
        [[ "$rel_path" == "$skip_path"* ]] && { _skip=1; break; }
    done
    [[ $_skip -eq 1 ]] && continue
    _skip=0

    # Check extension
    has_valid_extension "$file" || continue

    # Check for skip dirs in path
    _skip=0
    IFS='/' read -ra parts <<< "$rel_path"
    for part in "${parts[@]}"; do
        if should_skip_dir "$part"; then
            _skip=1
            break
        fi
    done
    [[ $_skip -eq 1 ]] && continue

    FILES+=("$file")
done < <(find "$WALK_DIR" -type f -print0 | sort -z)

TOTAL=${#FILES[@]}
echo "Found $TOTAL files to index"
echo ""

if [[ $TOTAL -eq 0 ]]; then
    echo "Nothing to index."
    exit 0
fi

# Index files one by one
OK=0
FAIL=0
SKIP=0
COUNT=0

for file in "${FILES[@]}"; do
    COUNT=$((COUNT + 1))
    rel_path="${file#$SOURCE_DIR/}"

    # Build viking URI: strip extension, use path components as directory structure
    dir_part="$(dirname "$rel_path")"
    base_name="$(basename "$rel_path" | sed 's/\.[^.]*$//')"

    if [[ "$dir_part" == "." ]]; then
        viking_uri="$VIKING_TARGET/$base_name"
    else
        viking_uri="$VIKING_TARGET/$dir_part/$base_name"
    fi

    if $DRY_RUN; then
        echo "[$COUNT/$TOTAL] $rel_path -> $viking_uri"
        OK=$((OK + 1))
        continue
    fi

    printf "[%d/%d] %s ... " "$COUNT" "$TOTAL" "$rel_path"

    if upload_file "$file" "$viking_uri"; then
        echo "OK"
        OK=$((OK + 1))
    else
        FAIL=$((FAIL + 1))
    fi

    # Pace uploads to avoid overwhelming the semantic queue
    if [[ $COUNT -lt $TOTAL ]]; then
        sleep "$UPLOAD_DELAY"
    fi
done

echo ""
echo "=== Complete ==="
echo "Total: $TOTAL | OK: $OK | Failed: $FAIL | Skipped: $SKIP"

if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
