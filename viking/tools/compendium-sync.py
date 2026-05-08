#!/usr/bin/env python3
"""Compendium → OpenViking sync script (Pattern #1 prototype).

Reads markdown entries from the compendium vault, builds pointer payloads,
and uploads them to OpenViking via the `ov` CLI.

Modes:
  sync-one <path>          - Sync a single entry
  sync-all [--update]      - Walk the vault and sync all entries
  sync-all --update        - Update existing entries (use ov write instead of add-resource)
  backfill                 - Add missing ov_mode: pointer to frontmatter, commit, then sync-all
"""

import os
import re
import sys
import subprocess
import tempfile

VAULT_ROOT = os.path.expanduser("~/code/compendium")
OV_TARGET_BASE = "viking://resources/compendium"

# Directories and files to EXCLUDE from sync
EXCLUDE_DIRS = {
    "_templates", "_sources", "_briefs", "_verification-reports",
    "_inbox", "_scripts", ".claude", ".git", "docs"
}
EXCLUDE_FILES = {
    "dashboard.md", "DATAVIEW.md", "README.md", "index.md", "log.md",
    "CLAUDE.md", ".gitignore"
}

# Entry type → ID prefix mapping
TYPE_PREFIX = {
    "ideas": "IDEA",
    "bugs": "BUG",
    "features": "FEAT",
    "tasks": "TASK",
    "projects": "PROJ",
    "info": "INFO",
}


def parse_frontmatter(content):
    """Extract YAML frontmatter from markdown content."""
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
    if not match:
        return None, content
    fm_text = match.group(1)
    body = content[match.end():]
    return fm_text, body


def get_field(frontmatter, field):
    """Extract a field value from YAML frontmatter text."""
    # Simple regex-based extraction (no yaml dependency)
    match = re.search(rf'^{field}:\s*(.+)$', frontmatter, re.MULTILINE)
    if not match:
        return None
    value = match.group(1).strip()
    # Handle list format: [tag1, tag2, ...]
    if value.startswith('[') and value.endswith(']'):
        return [t.strip().strip("'\"") for t in value[1:-1].split(',')]
    return value


def get_effective_ov_mode(frontmatter):
    """Determine effective ov_mode from frontmatter. Default: pointer."""
    ov_mode = get_field(frontmatter, 'ov_mode')
    if ov_mode is None:
        return 'pointer'
    return ov_mode.strip().lower()


def extract_first_paragraph(body):
    """Extract the first non-empty, non-heading paragraph after frontmatter."""
    lines = body.split('\n')
    para_lines = []
    in_para = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_para and para_lines:
                break
            continue
        if stripped.startswith('#'):
            if in_para and para_lines:
                break
            continue
        # Skip lines that are just emphasis markers
        if stripped.startswith('_') and stripped.endswith('_') and len(stripped) > 2:
            # Keep the text, strip the emphasis
            para_lines.append(stripped)
            in_para = True
            continue
        para_lines.append(stripped)
        in_para = True
    return ' '.join(para_lines) if para_lines else ''


def extract_headings(body):
    """Extract ## and ### headings from the body."""
    headings = []
    for line in body.split('\n'):
        stripped = line.strip()
        if stripped.startswith('## ') or stripped.startswith('### '):
            # Remove trailing colons
            heading = stripped.rstrip(':')
            headings.append(heading)
    return headings


def get_entry_id(frontmatter, filepath):
    """Extract entry ID from frontmatter or filename."""
    fm_id = get_field(frontmatter, 'id')
    if fm_id:
        return fm_id
    # Derive from filename: BUG-003-slug.md → BUG-003
    basename = os.path.basename(filepath)
    match = re.match(r'^([A-Z]+-\d+)', basename)
    if match:
        return match.group(1)
    # For date-prefixed plan files: 2026-04-28-slug.md → use filename stem
    date_match = re.match(r'^(\d{4}-\d{2}-\d{2}-.+)\.md$', basename)
    if date_match:
        return date_match.group(1)
    # For info/ and other entries without type prefix: slug-name.md → use filename stem
    stem_match = re.match(r'^(.+)\.md$', basename)
    if stem_match:
        return stem_match.group(1)
    return None


def get_rel_path(filepath):
    """Get path relative to VAULT_ROOT."""
    return os.path.relpath(filepath, VAULT_ROOT)


def get_target_dir(filepath):
    """Determine OV target_dir mirroring vault subdirectory structure."""
    rel_path = get_rel_path(filepath)
    parts = rel_path.split(os.sep)
    # Remove the filename, keep the directory path
    dir_parts = parts[:-1]
    # Build target_dir
    dir_path = '/'.join(dir_parts) if dir_parts else ''
    return f"{OV_TARGET_BASE}/{dir_path}/" if dir_path else f"{OV_TARGET_BASE}/"


def get_ov_name(entry_id):
    """Derive OV resource name from entry ID (lowercase)."""
    if entry_id:
        # For date-prefixed plan IDs, keep as-is (already lowercase-friendly)
        if re.match(r'^\d{4}-\d{2}-\d{2}-', entry_id):
            return entry_id
        return entry_id.lower()
    return None


def build_payload(frontmatter, body, filepath):
    """Build the pointer payload per the COMPENDIUM_OV_SPEC.md format."""
    entry_id = get_entry_id(frontmatter, filepath)
    title = get_field(frontmatter, 'title') or ''
    tags = get_field(frontmatter, 'tags')
    tags_str = ', '.join(tags) if isinstance(tags, list) else (tags or '')
    ov_mode = get_effective_ov_mode(frontmatter)
    rel_path = get_rel_path(filepath)
    first_para = extract_first_paragraph(body)
    headings = extract_headings(body)

    lines = [
        f"[ov_mode: {ov_mode}]",
        f"Path: ~/code/compendium/{rel_path}",
        f"ID: {entry_id or 'unknown'}",
        f"Title: {title}",
        f"Tags: {tags_str}",
        "---",
        frontmatter,
        "---",
        first_para,
        "",
        "Headings:",
    ]
    for h in headings:
        lines.append(h)

    return '\n'.join(lines)


def uri_exists(uri):
    """Check if a URI already exists in OV."""
    result = subprocess.run(['ov', 'stat', uri], capture_output=True, text=True, timeout=10)
    return result.returncode == 0


def sync_one(filepath, update=False):
    """Sync a single entry to OV. If update=True, overwrite existing entries."""
    with open(filepath, 'r') as f:
        content = f.read()

    frontmatter, body = parse_frontmatter(content)
    if frontmatter is None:
        return {'status': 'skip', 'path': filepath, 'reason': 'no frontmatter'}

    ov_mode = get_effective_ov_mode(frontmatter)
    if ov_mode == 'none':
        return {'status': 'skipped', 'path': filepath, 'reason': 'ov_mode: none'}

    entry_id = get_entry_id(frontmatter, filepath)
    name = get_ov_name(entry_id)
    if not name:
        return {'status': 'error', 'path': filepath, 'reason': 'no entry ID'}

    payload = build_payload(frontmatter, body, filepath)
    target_dir = get_target_dir(filepath)
    uri = f"{target_dir}{name}.md"

    # Write payload to temp file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, prefix=f'{name}-') as tmp:
        tmp.write(payload)
        tmp_path = tmp.name

    try:
        if update and uri_exists(uri):
            # Update existing entry by deleting and recreating
            # (ov write only works on files, not directories; entries are directories)
            subprocess.run(['ov', 'rm', '-r', uri], capture_output=True, text=True, timeout=10)
            subprocess.run(['ov', 'mkdir', target_dir], capture_output=True, text=True)
            result = subprocess.run(
                ['ov', 'add-resource', tmp_path, '--to', uri],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                return {'status': 'error', 'path': filepath, 'reason': result.stderr[:200]}
            return {'status': 'updated', 'id': entry_id, 'name': name, 'target_dir': target_dir, 'uri': uri}
        else:
            # Create new entry
            subprocess.run(['ov', 'mkdir', target_dir], capture_output=True, text=True)
            result = subprocess.run(
                ['ov', 'add-resource', tmp_path, '--to', uri],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                if 'already exists' in result.stderr or 'EXISTS' in result.stderr:
                    return {'status': 'exists', 'id': entry_id, 'name': name, 'target_dir': target_dir, 'uri': uri}
                return {'status': 'error', 'path': filepath, 'reason': result.stderr[:200]}
            return {'status': 'synced', 'id': entry_id, 'name': name, 'target_dir': target_dir, 'uri': uri}
    finally:
        os.unlink(tmp_path)


def should_sync(filepath):
    """Check if a file should be synced (not excluded)."""
    rel_path = os.path.relpath(filepath, VAULT_ROOT)
    parts = rel_path.split(os.sep)

    # Check excluded directories
    for part in parts:
        if part in EXCLUDE_DIRS:
            return False

    # Check excluded files
    basename = os.path.basename(filepath)
    if basename in EXCLUDE_FILES:
        return False

    # Only .md files
    if not basename.endswith('.md'):
        return False

    return True


def walk_vault():
    """Walk the vault and return all syncable .md files."""
    files = []
    for root, dirs, filenames in os.walk(VAULT_ROOT):
        # Filter out excluded directories in-place
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith('.')]
        for fn in filenames:
            if fn.endswith('.md'):
                filepath = os.path.join(root, fn)
                if should_sync(filepath):
                    files.append(filepath)
    return sorted(files)


def check_id_collisions(files):
    """Check for duplicate entry IDs across the vault. Returns list of collisions."""
    id_map = {}
    for filepath in files:
        with open(filepath, 'r') as f:
            content = f.read()
        frontmatter, _ = parse_frontmatter(content)
        if frontmatter is None:
            continue
        entry_id = get_entry_id(frontmatter, filepath)
        if entry_id:
            if entry_id not in id_map:
                id_map[entry_id] = []
            id_map[entry_id].append(filepath)

    collisions = {eid: paths for eid, paths in id_map.items() if len(paths) > 1}
    return collisions


def sync_all(update=False):
    """Sync all entries in the vault. If update=True, overwrite existing entries."""
    files = walk_vault()

    # Check for ID collisions before syncing
    collisions = check_id_collisions(files)
    if collisions:
        print("WARNING: Duplicate entry IDs found:")
        for eid, paths in collisions.items():
            print(f"  {eid}:")
            for p in paths:
                print(f"    - {os.path.relpath(p, VAULT_ROOT)}")
        print(f"Skipping {sum(len(v) for v in collisions.values())} entries with duplicate IDs.\n")

    results = {'synced': [], 'updated': [], 'exists': [], 'skipped': [], 'errors': []}

    for filepath in files:
        # Skip entries with duplicate IDs
        with open(filepath, 'r') as f:
            content = f.read()
        frontmatter, _ = parse_frontmatter(content)
        entry_id = get_entry_id(frontmatter, filepath) if frontmatter else None
        if entry_id and entry_id in collisions:
            results['skipped'].append({'path': filepath, 'reason': f'duplicate ID: {entry_id}'})
            continue

        result = sync_one(filepath, update=update)
        print(f"  [{len(results['synced'])+len(results['updated'])+len(results['exists'])+len(results['skipped'])+len(results['errors'])+1}/{len(files)}] {result['status']}: {result.get('id', '?')}", flush=True)
        if result['status'] in ('synced', 'updated', 'exists'):
            results[result['status']].append(result)
        elif result['status'] == 'skipped':
            results['skipped'].append(result)
        elif result['status'] == 'error':
            results['errors'].append(result)

    return results


def print_results(results):
    """Print a summary table."""
    print(f"\n| Status | ID | Target dir | Mode | Notes |")
    print(f"|--------|-----|------------|------|-------|")
    for r in results.get('synced', []):
        print(f"| synced | {r.get('id', '?')} | {r.get('target_dir', '?')} | pointer | — |")
    for r in results.get('updated', []):
        print(f"| updated | {r.get('id', '?')} | {r.get('target_dir', '?')} | pointer | — |")
    for r in results.get('exists', []):
        print(f"| exists | {r.get('id', '?')} | {r.get('target_dir', '?')} | pointer | — |")
    for r in results.get('skipped', []):
        rel = os.path.relpath(r['path'], VAULT_ROOT)
        print(f"| skipped | — | — | none | {rel}: {r['reason']} |")
    for r in results.get('errors', []):
        rel = os.path.relpath(r['path'], VAULT_ROOT)
        print(f"| error | — | {r.get('target_dir', '?')} | pointer | {rel}: {r['reason']} |")

    total = len(results.get('synced', [])) + len(results.get('updated', [])) + len(results.get('exists', [])) + len(results.get('skipped', [])) + len(results.get('errors', []))
    print(f"\nTotal: {len(results.get('synced', []))} synced, {len(results.get('updated', []))} updated, {len(results.get('exists', []))} exists, {len(results.get('skipped', []))} skipped, {len(results.get('errors', []))} errors ({total} entries scanned)")


def backfill():
    """Add ov_mode: pointer to entries missing it, commit, then sync-all."""
    files = walk_vault()
    modified = []

    for filepath in files:
        with open(filepath, 'r') as f:
            content = f.read()

        frontmatter, _ = parse_frontmatter(content)
        if frontmatter is None:
            continue

        ov_mode = get_field(frontmatter, 'ov_mode')
        if ov_mode is not None:
            continue  # Already has ov_mode

        # Add ov_mode: pointer after the tags line, or at end of frontmatter
        fm_lines = frontmatter.split('\n')
        inserted = False
        new_lines = []
        for line in fm_lines:
            new_lines.append(line)
            if line.startswith('tags:'):
                new_lines.append('ov_mode: pointer')
                inserted = True
        if not inserted:
            new_lines.append('ov_mode: pointer')

        new_fm = '\n'.join(new_lines)
        new_content = content.replace(frontmatter, new_fm, 1)

        with open(filepath, 'w') as f:
            f.write(new_content)
        modified.append(os.path.relpath(filepath, VAULT_ROOT))

    # Commit changes
    if modified:
        subprocess.run(['git', 'add', '-A'], cwd=VAULT_ROOT, capture_output=True)
        subprocess.run(
            ['git', 'commit', '-m', 'backfill: add ov_mode: pointer to all entries'],
            cwd=VAULT_ROOT, capture_output=True
        )
        print(f"Modified {len(modified)} entries, committed.")
    else:
        print("No entries needed ov_mode added.")

    # Now sync all
    results = sync_all()
    print_results(results)


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: compendium-sync.py <mode> [args]")
        print("Modes: sync-one <path>, sync-all [--update], backfill")
        sys.exit(1)

    mode = sys.argv[1]

    if mode == 'sync-one':
        if len(sys.argv) < 3:
            print("Usage: compendium-sync.py sync-one <path>")
            sys.exit(1)
        filepath = os.path.expanduser(sys.argv[2])
        result = sync_one(filepath)
        print(f"| Status | ID | Target dir | Mode | Notes |")
        print(f"|--------|-----|------------|------|-------|")
        if result['status'] == 'synced':
            print(f"| synced | {result.get('id', '?')} | {result.get('target_dir', '?')} | pointer | — |")
        elif result['status'] == 'skipped':
            print(f"| skipped | — | — | none | {result['reason']} |")
        else:
            print(f"| {result['status']} | — | — | — | {result.get('reason', '?')} |")

    elif mode == 'sync-all':
        update = '--update' in sys.argv
        results = sync_all(update=update)
        print_results(results)

    elif mode == 'backfill':
        backfill()

    else:
        print(f"Unknown mode: {mode}")
        sys.exit(1)