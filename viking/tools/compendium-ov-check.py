#!/usr/bin/env python3
"""Validate compendium ↔ OpenViking sync consistency.

Checks:
  1. Structure: local terminal dirs (resolved/completed) have matching OV entries
  2. Stale: OV entries that no longer exist locally (ghosts)
  3. Required fields: terminal items have required completion frontmatter fields
  4. Active items not in OV: ensures "active items never pushed" rule

Outputs a summary table and exits non-zero if any issues found.
"""

import os
import re
import subprocess
import sys

VAULT_ROOT = os.path.expanduser("~/code/compendium")
OV_TARGET_BASE = "viking://resources/compendium"
ACTIVE_STATUSES = {"todo", "open", "proposed", "in-progress", "investigating", "active"}

# Terminal directories and their required frontmatter fields
TERMINAL_DIRS = {
    "bugs/resolved":      {"prefix": "BUG",  "required_field": "fixed_in"},
    "features/completed": {"prefix": "FEAT", "required_field": "completed_in"},
    "ideas/completed":     {"prefix": "IDEA", "required_field": "completed_in"},
}

# Active directories (should NOT be in OV)
ACTIVE_DIRS = [
    "bugs",
    "features",
    "ideas",
    "projects",
    "tasks",
]


def parse_frontmatter(content):
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
    if not match:
        return None, content
    return match.group(1), content[match.end():]


def get_field(frontmatter, field):
    match = re.search(rf'^{field}:\s*(.+)$', frontmatter, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip()


def get_status(frontmatter):
    status = get_field(frontmatter, 'status')
    if status:
        # Strip quotes
        return status.strip('"').strip("'")
    return None


def entry_id_from_stem(stem):
    match = re.match(r'^([A-Z]+-\d+)', stem)
    return match.group(1) if match else stem


def ov_name_for_stem(stem):
    eid = entry_id_from_stem(stem)
    if re.match(r'^\d{4}-\d{2}-\d{2}-', eid):
        return eid
    return eid.lower()


def ov_segment(seg):
    """Casing rule for one URI path segment — same as ov_name (IMPR-1055)."""
    if re.match(r'^\d{4}-\d{2}-\d{2}-', seg):
        return seg
    return seg.lower()


def ov_uri_for(subdir, stem, target_base=OV_TARGET_BASE):
    """Expected OV URI for a vault file — the single derivation site here.

    Mirrors compendium-sync.py's target_dir_for + ov_name: OV paths are
    case-sensitive with no server-side normalization (IMPR-1055), so every
    parent segment takes the same casing rule as the ID leaf (tasks/PROJ-028/
    → tasks/proj-028/). Kept in lockstep by the compendium vault's
    _scripts/test-uri-derivation-parity.py.
    """
    normalized = "/".join(ov_segment(s) for s in subdir.strip("/").split("/"))
    return f"{target_base.rstrip('/')}/{normalized}/{ov_name_for_stem(stem)}.md"


def ov_ls(uri):
    """List an OV directory. Returns list of entry names or empty list."""
    result = subprocess.run(
        ['ov', 'ls', uri],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        return []
    entries = []
    uri_prefix = uri.rstrip("/") + "/"
    for line in result.stdout.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        # ov ls output: "[d] dirname" or "[f] filename"
        match = re.match(r'\[[df]\]\s+(.+)', line)
        if match:
            entries.append(match.group(1).strip())
            continue
        # Newer table output starts rows with full Viking URIs.
        if line.startswith(uri_prefix):
            entry_uri = line.split()[0]
            entries.append(entry_uri.removeprefix(uri_prefix).strip("/"))
    return entries


def local_terminal_files(subdir):
    """Return dict of {stem: filepath} for .md files in a local subdir."""
    full_dir = os.path.join(VAULT_ROOT, subdir)
    if not os.path.isdir(full_dir):
        return {}
    files = {}
    for fn in os.listdir(full_dir):
        if fn.endswith('.md'):
            stem = fn[:-3]  # Remove .md
            files[stem] = os.path.join(full_dir, fn)
    return files


def local_active_files(subdir):
    """Return dict of {stem: filepath} for .md files in an active (non-terminal) subdir."""
    full_dir = os.path.join(VAULT_ROOT, subdir)
    if not os.path.isdir(full_dir):
        return {}
    files = {}
    for fn in os.listdir(full_dir):
        if fn.endswith('.md') and not fn.startswith('_'):
            stem = fn[:-3]
            files[stem] = os.path.join(full_dir, fn)
    return files


def check_structure():
    """Check terminal dirs have matching OV entries."""
    issues = []
    for subdir, meta in TERMINAL_DIRS.items():
        local_files = local_terminal_files(subdir)
        ov_entries = ov_ls(f"{OV_TARGET_BASE}/{subdir}")

        # Normalize OV entry names (they're lowercase, may have .md suffix stripped)
        ov_names = set()
        for entry in ov_entries:
            name = entry.replace('.md', '').strip()
            ov_names.add(name)

        for stem, filepath in local_files.items():
            ov_name = ov_name_for_stem(stem)
            if ov_name not in ov_names:
                issues.append({
                    'type': 'missing_in_ov',
                    'item': stem,
                    'subdir': subdir,
                    'local_path': filepath,
                    'expected_uri': f"{OV_TARGET_BASE}/{subdir}/{ov_name}.md",
                })

    return issues


def check_stale():
    """Check for OV entries that no longer exist locally."""
    issues = []
    for subdir, meta in TERMINAL_DIRS.items():
        local_files = local_terminal_files(subdir)
        local_stems = {ov_name_for_stem(stem) for stem in local_files.keys()}

        ov_entries = ov_ls(f"{OV_TARGET_BASE}/{subdir}")
        for entry in ov_entries:
            name = entry.replace('.md', '').strip().lower()
            if name not in local_stems:
                issues.append({
                    'type': 'ghost_in_ov',
                    'item': name,
                    'subdir': subdir,
                    'uri': f"{OV_TARGET_BASE}/{subdir}/{name}.md",
                })

    return issues


def check_required_fields():
    """Check terminal items have required completion frontmatter fields."""
    issues = []
    for subdir, meta in TERMINAL_DIRS.items():
        local_files = local_terminal_files(subdir)
        for stem, filepath in local_files.items():
            with open(filepath, 'r') as f:
                content = f.read()
            frontmatter, _ = parse_frontmatter(content)
            if frontmatter is None:
                issues.append({
                    'type': 'missing_frontmatter',
                    'item': stem,
                    'subdir': subdir,
                    'local_path': filepath,
                    'detail': 'no frontmatter found',
                })
                continue

            required = meta['required_field']
            value = get_field(frontmatter, required)
            if not value:
                issues.append({
                    'type': 'missing_field',
                    'item': stem,
                    'subdir': subdir,
                    'local_path': filepath,
                    'detail': f'missing required field: {required}',
                })

    return issues


def check_active_not_in_ov():
    """Verify active items are NOT in OV (only terminal items should be synced)."""
    issues = []
    # Check top-level active dirs, excluding terminal subdirs
    for subdir in ACTIVE_DIRS:
        local_files = local_active_files(subdir)
        for stem, filepath in local_files.items():
            # Read frontmatter to check status
            with open(filepath, 'r') as f:
                content = f.read()
            frontmatter, _ = parse_frontmatter(content)
            if frontmatter is None:
                continue
            status = get_status(frontmatter)
            if status not in ACTIVE_STATUSES:
                continue

            # Active item by sync policy — check if it exists in OV
            ov_name = ov_name_for_stem(stem)
            ov_uri = f"{OV_TARGET_BASE}/{subdir}/{ov_name}.md"
            result = subprocess.run(
                ['ov', 'stat', ov_uri],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                issues.append({
                    'type': 'active_in_ov',
                    'item': stem,
                    'subdir': subdir,
                    'local_path': filepath,
                    'uri': ov_uri,
                    'detail': f'active item (status: {status}) found in OV',
                })

    return issues


def run_checks():
    """Run all checks and print results."""
    all_issues = []
    checks = [
        ("Structure: terminal items in OV", check_structure),
        ("Stale: ghost entries in OV", check_stale),
        ("Fields: required completion fields", check_required_fields),
        ("Active: active items not in OV", check_active_not_in_ov),
    ]

    print("=== Compendium ↔ OpenViking Sync Validation ===\n")

    for name, check_fn in checks:
        print(f"Running: {name}...")
        issues = check_fn()
        all_issues.extend(issues)
        print(f"  Found {len(issues)} issues\n")

    # Summary table
    if not all_issues:
        print("✓ All checks passed. Compendium-OV sync is consistent.")
        return 0

    print("\n### Issues Found\n")
    print("| Type | Item | Dir | Detail |")
    print("|------|------|-----|--------|")
    for issue in all_issues:
        print(f"| {issue['type']} | {issue['item']} | {issue['subdir']} | {issue.get('detail', '—')} |")

    print(f"\n**Total: {len(all_issues)} issues**")
    return 1


if __name__ == '__main__':
    sys.exit(run_checks())
