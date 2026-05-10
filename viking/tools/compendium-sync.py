#!/usr/bin/env python3
"""Progressive Compendium -> OpenViking pointer sync.

This indexes compact pointer payloads from ~/code/compendium into OpenViking.
It is intentionally staged: start with --dry-run/plan, then sync small batches
with --limit and increase only after search and queue behavior look healthy.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


VAULT_ROOT = Path(os.environ.get("COMPENDIUM_ROOT", "~/code/compendium")).expanduser()
DEFAULT_TARGET_BASE = os.environ.get("OV_TARGET_BASE", "viking://resources/compendium")
ACTIVE_STATUSES = {"todo", "open", "proposed", "in-progress", "investigating", "active"}

EXCLUDE_DIRS = {
    ".claude",
    ".git",
    ".obsidian",
    "_briefs",
    "_inbox",
    "_scripts",
    "_sources",
    "_templates",
    "_verification-reports",
    "docs",
}
EXCLUDE_FILES = {
    ".gitignore",
    "CLAUDE.md",
    "DATAVIEW.md",
    "README.md",
    "dashboard.md",
    "index.md",
    "log.md",
}


@dataclass(frozen=True)
class Entry:
    path: Path
    rel_path: str
    frontmatter: str
    body: str
    entry_id: str
    name: str
    ov_mode: str
    payload: str
    target_dir: str
    uri: str
    status: str
    entry_type: str
    repo: str


def parse_frontmatter(text: str) -> tuple[str | None, str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not match:
        return None, text
    return match.group(1), text[match.end() :]


def field(frontmatter: str, key: str):
    lines = frontmatter.splitlines()
    start = None
    raw = None
    for i, line in enumerate(lines):
        match = re.match(rf"^{re.escape(key)}:\s*(.*)$", line)
        if match:
            start = i
            raw = match.group(1).strip()
            break
    if start is None or raw is None:
        return None

    if raw in ("null", "~", "[]"):
        return None

    if raw.startswith("[") or raw == "":
        collected = [raw] if raw else []
        for line in lines[start + 1 :]:
            if line and not line.startswith((" ", "\t")):
                break
            stripped = line.strip()
            if not stripped:
                continue
            collected.append(stripped)
            if stripped.endswith("]"):
                break
        joined = " ".join(collected).strip()
        if joined.startswith("[") and joined.endswith("]"):
            return [
                item.strip().strip(",").strip("'\"")
                for item in joined[1:-1].split(",")
                if item.strip().strip(",").strip("'\"")
            ]

    if raw == "":
        items = []
        for line in lines[start + 1 :]:
            if line and not line.startswith((" ", "\t")):
                break
            stripped = line.strip()
            if stripped.startswith("- "):
                items.append(stripped[2:].strip().strip("'\""))
        return items if items else ""

    if raw.startswith("[") and raw.endswith("]"):
        return [item.strip().strip("'\"") for item in raw[1:-1].split(",") if item.strip()]
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]
    return raw


def scalar(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def first_field(frontmatter: str, *keys: str):
    for key in keys:
        value = field(frontmatter, key)
        if value is not None:
            return value
    return None


def first_useful_paragraph(body: str) -> str:
    paragraph = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            if paragraph:
                break
            continue
        if stripped.startswith("#") or stripped == "---":
            if paragraph:
                break
            continue
        if stripped.startswith("> Canonical source:"):
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            continue
        if stripped.startswith("> "):
            stripped = stripped[2:].strip()
        if stripped.startswith("_") and stripped.endswith("_"):
            stripped = stripped.strip("_")
        paragraph.append(stripped)
    return " ".join(paragraph)


def headings(body: str) -> list[str]:
    out = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## ") or stripped.startswith("### "):
            out.append(stripped.rstrip(":"))
    return out


def entry_id(frontmatter: str, path: Path) -> str:
    explicit = field(frontmatter, "id")
    if explicit:
        return str(explicit)
    stem = path.stem
    match = re.match(r"^([A-Z]+-\d+)", stem)
    return match.group(1) if match else stem


def ov_name(eid: str) -> str:
    if re.match(r"^\d{4}-\d{2}-\d{2}-", eid):
        return eid
    return eid.lower()


def target_dir_for(rel_path: str, target_base: str) -> str:
    parent = Path(rel_path).parent
    if str(parent) == ".":
        return target_base.rstrip("/") + "/"
    return f"{target_base.rstrip('/')}/{str(parent).replace(os.sep, '/')}/"


def normalize_compendium_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = VAULT_ROOT / path
    return path.resolve()


def uri_for_path(path: Path, target_base: str) -> str:
    rel_path = str(path.relative_to(VAULT_ROOT))
    eid = entry_id("", path)
    return f"{target_dir_for(rel_path, target_base)}{ov_name(eid)}.md"


def payload_for(path: Path, target_base: str) -> Entry | None:
    text = path.read_text()
    frontmatter, body = parse_frontmatter(text)
    if frontmatter is None:
        return None

    mode = scalar(field(frontmatter, "ov_mode") or "pointer").lower()
    if mode == "none":
        return None

    rel_path = str(path.relative_to(VAULT_ROOT))
    eid = entry_id(frontmatter, path)
    name = ov_name(eid)
    target_dir = target_dir_for(rel_path, target_base)
    uri = f"{target_dir}{name}.md"
    entry_type = scalar(field(frontmatter, "type") or path.parent.name)
    status = scalar(field(frontmatter, "status"))
    repo = scalar(field(frontmatter, "repo") or field(frontmatter, "repos"))
    customers = first_field(frontmatter, "customers", "customer")

    lines = [
        "[ov_mode: pointer]",
        f"Path: ~/code/compendium/{rel_path}",
        f"ID: {eid}",
        f"Title: {scalar(field(frontmatter, 'title'))}",
        f"Type: {entry_type}",
        f"Status: {status}",
        f"Repo: {repo}",
        f"Priority: {scalar(field(frontmatter, 'priority') or field(frontmatter, 'severity'))}",
        f"Customer: {scalar(field(frontmatter, 'customer'))}",
        f"Customers: {scalar(customers)}",
        f"People: {scalar(first_field(frontmatter, 'people', 'person', 'assignee'))}",
        f"Themes: {scalar(field(frontmatter, 'themes'))}",
        f"Domains: {scalar(first_field(frontmatter, 'domains', 'domain', 'topic'))}",
        f"Pipeline: {scalar(field(frontmatter, 'pipeline'))}",
        f"Tags: {scalar(field(frontmatter, 'tags'))}",
        f"Aliases: {scalar(field(frontmatter, 'aliases'))}",
        f"Retrieval Summary: {scalar(first_field(frontmatter, 'retrieval_summary', 'summary'))}",
        f"Related: {scalar(field(frontmatter, 'related'))}",
        f"Source: {scalar(field(frontmatter, 'source'))}",
        f"Sources: {scalar(field(frontmatter, 'sources'))}",
        "---",
        frontmatter,
        "---",
        "Summary:",
        first_useful_paragraph(body),
        "",
        "Headings:",
    ]
    lines.extend(headings(body))

    return Entry(
        path=path,
        rel_path=rel_path,
        frontmatter=frontmatter,
        body=body,
        entry_id=eid,
        name=name,
        ov_mode=mode,
        payload="\n".join(lines),
        target_dir=target_dir,
        uri=uri,
        status=status,
        entry_type=entry_type,
        repo=repo,
    )


def iter_markdown() -> list[Path]:
    paths = []
    for path in VAULT_ROOT.rglob("*.md"):
        rel_parts = path.relative_to(VAULT_ROOT).parts
        if any(part in EXCLUDE_DIRS for part in rel_parts):
            continue
        if path.name in EXCLUDE_FILES:
            continue
        paths.append(path)
    return sorted(paths)


def entries(target_base: str) -> list[Entry]:
    return [entry for path in iter_markdown() if (entry := payload_for(path, target_base))]


def entries_for_paths(paths: list[str], target_base: str) -> list[Entry]:
    selected = []
    seen = set()
    for raw_path in paths:
        path = normalize_compendium_path(raw_path)
        if path in seen:
            continue
        seen.add(path)
        if not path.exists():
            raise FileNotFoundError(path)
        entry = payload_for(path, target_base)
        if entry is None:
            raise ValueError(f"No pointer payload for {path}")
        selected.append(entry)
    return selected


def select(entries_: list[Entry], args) -> list[Entry]:
    selected = entries_
    if args.repo:
        selected = [e for e in selected if args.repo in e.repo.split(", ")]
    if args.type:
        selected = [e for e in selected if e.entry_type == args.type]
    if args.status:
        selected = [e for e in selected if e.status == args.status]
    if args.exclude_active:
        selected = [e for e in selected if e.status not in ACTIVE_STATUSES]
    if args.order == "small-first":
        selected = sorted(selected, key=lambda e: len(e.payload))
    elif args.order == "large-first":
        selected = sorted(selected, key=lambda e: len(e.payload), reverse=True)
    else:
        selected = sorted(selected, key=lambda e: e.rel_path)
    return selected[args.offset : args.offset + args.limit if args.limit else None]


def ov_cli_config() -> tuple[dict[str, str], str | None]:
    """Build an explicit CLI config so ov subprocesses match OPENVIKING_* env."""
    url = os.environ.get("OPENVIKING_URL")
    if not url:
        return os.environ.copy(), None

    key = os.environ.get("OPENVIKING_KEY")
    if not key:
        raise RuntimeError("OPENVIKING_URL is set but OPENVIKING_KEY is missing")

    config = {
        "url": url.rstrip("/"),
        "api_key": key,
        "account": os.environ.get("OPENVIKING_ACCOUNT", "default"),
        "user": os.environ.get("OPENVIKING_USER", "noot"),
    }
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", prefix="ovcli-", delete=False)
    try:
        json.dump(config, tmp)
        tmp.write("\n")
        tmp.close()
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise

    env = os.environ.copy()
    env["OPENVIKING_CLI_CONFIG_FILE"] = tmp.name
    return env, tmp.name


def run_ov(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    env, config_path = ov_cli_config()
    try:
        return subprocess.run(["ov", *cmd], capture_output=True, text=True, timeout=timeout, env=env)
    finally:
        if config_path:
            Path(config_path).unlink(missing_ok=True)


def check_ov_cli_config() -> tuple[bool, str]:
    try:
        env, config_path = ov_cli_config()
    except RuntimeError as exc:
        return False, str(exc)

    if not config_path:
        return True, "using existing ov CLI config"

    Path(config_path).unlink(missing_ok=True)
    return True, f"using OPENVIKING_URL={env['OPENVIKING_URL'].rstrip('/')}"


def health_check() -> str:
    url = os.environ.get("OPENVIKING_URL")
    if not url:
        return "health: skipped (OPENVIKING_URL unset)"
    key = os.environ.get("OPENVIKING_KEY", "")
    req = urllib.request.Request(f"{url.rstrip('/')}/health")
    if key:
        req.add_header("X-API-Key", key)
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return f"health: HTTP {resp.status} in {elapsed_ms}ms"
    except (urllib.error.URLError, TimeoutError) as exc:
        return f"health: failed ({exc})"


def sync_entry(entry: Entry, wait: bool) -> tuple[str, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".md", prefix=f"{entry.name}-", delete=False) as tmp:
        tmp.write(entry.payload)
        tmp_path = tmp.name
    try:
        mkdir = run_ov(["mkdir", entry.target_dir], timeout=30)
        if mkdir.returncode != 0 and "exists" not in mkdir.stderr.lower():
            return "error", mkdir.stderr.strip()[:300]
        cmd = ["add-resource", tmp_path, "--to", entry.uri]
        if wait:
            cmd.append("--wait")
        result = run_ov(cmd, timeout=600 if wait else 120)
        if result.returncode == 0:
            return "synced", entry.uri
        if "already exists" in result.stderr.lower() or "exists" in result.stderr.lower():
            return "exists", entry.uri
        return "error", result.stderr.strip()[:300]
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def print_stats(entries_: list[Entry], target_base: str) -> None:
    sizes = [len(e.payload) for e in entries_]
    print(f"Vault: {VAULT_ROOT}")
    print(f"Target: {target_base}")
    print(f"Entries: {len(entries_)}")
    if sizes:
        print(f"Payload bytes: avg={sum(sizes)//len(sizes)} min={min(sizes)} max={max(sizes)}")
    for label, getter in (
        ("Types", lambda e: e.entry_type or "(none)"),
        ("Statuses", lambda e: e.status or "(none)"),
        ("Repos", lambda e: e.repo or "(none)"),
    ):
        counts = {}
        for entry in entries_:
            key = getter(entry)
            counts[key] = counts.get(key, 0) + 1
        print(f"\n{label}")
        for key, count in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
            print(f"  {key}: {count}")


def cmd_sync(args, entries_: list[Entry]) -> int:
    selected = select(entries_, args)
    print(f"Selected {len(selected)} entries (offset={args.offset}, limit={args.limit or 'all'})")
    if args.dry_run:
        for raw_path in args.delete_path:
            uri = uri_for_path(normalize_compendium_path(raw_path), args.target_base)
            print(f"dry-run delete {uri} <- {raw_path}")
        for entry in selected:
            print(f"dry-run {entry.uri} <- {entry.rel_path} ({len(entry.payload)} bytes)")
        return 0

    config_ok, config_msg = check_ov_cli_config()
    if not config_ok:
        print(f"OpenViking CLI config error: {config_msg}", file=sys.stderr)
        return 1
    print(f"OpenViking CLI: {config_msg}")
    if args.update:
        print("--update is deprecated: add-resource --to refreshes existing resources in place")

    for raw_path in args.delete_path:
        uri = uri_for_path(normalize_compendium_path(raw_path), args.target_base)
        result = run_ov(["rm", "-r", uri], timeout=120)
        if result.returncode == 0:
            print(f"deleted: {uri}", flush=True)
        elif "not found" in result.stderr.lower() or "does not exist" in result.stderr.lower():
            print(f"delete skipped: {uri} (missing)", flush=True)
        else:
            print(f"delete error: {uri} {result.stderr.strip()[:300]}", file=sys.stderr)
            return 1

    ok = 0
    errors = 0
    for idx, entry in enumerate(selected, 1):
        status, detail = sync_entry(entry, wait=not args.no_wait)
        print(f"[{idx}/{len(selected)}] {status}: {entry.entry_id} {detail}", flush=True)
        if status in {"synced", "exists"}:
            ok += 1
        else:
            errors += 1
            if errors >= args.max_errors:
                print(f"stopping after {errors} errors")
                return 1
        if idx % args.batch_size == 0:
            print(health_check(), flush=True)
            if args.pause:
                time.sleep(args.pause)
    print(f"Done: {ok} ok, {errors} errors")
    print(health_check())
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-base", default=DEFAULT_TARGET_BASE)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stats")

    preview = sub.add_parser("preview")
    preview.add_argument("path")

    plan = sub.add_parser("plan")
    sync = sub.add_parser("sync")
    for p in (plan, sync):
        p.add_argument("paths", nargs="*", help="Optional compendium markdown paths to sync")
        p.add_argument("--limit", type=int, default=0, help="Maximum entries to select; 0 means all")
        p.add_argument("--offset", type=int, default=0)
        p.add_argument("--repo")
        p.add_argument("--type")
        p.add_argument("--status")
        p.add_argument(
            "--exclude-active",
            action="store_true",
            default=True,
            help="Exclude active/open work items from sync selection (default)",
        )
        p.add_argument(
            "--include-active",
            action="store_false",
            dest="exclude_active",
            help="Include active/open work items in sync selection",
        )
        p.add_argument("--order", choices=["path", "small-first", "large-first"], default="path")
    sync.add_argument("--batch-size", type=int, default=10)
    sync.add_argument("--pause", type=float, default=0)
    sync.add_argument(
        "--update",
        action="store_true",
        help="Deprecated no-op; add-resource --to refreshes existing resources in place",
    )
    sync.add_argument(
        "--delete-path",
        action="append",
        default=[],
        help="Delete the OpenViking pointer URI for a previous compendium path before syncing",
    )
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--no-wait", action="store_true")
    sync.add_argument("--max-errors", type=int, default=3)

    args = parser.parse_args()
    try:
        if args.command in {"plan", "sync"} and getattr(args, "paths", None):
            all_entries = entries_for_paths(args.paths, args.target_base)
        else:
            all_entries = entries(args.target_base)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Selection error: {exc}", file=sys.stderr)
        return 1

    if args.command == "stats":
        print_stats(all_entries, args.target_base)
        return 0
    if args.command == "preview":
        entry = payload_for(Path(args.path).expanduser(), args.target_base)
        if entry is None:
            print("No pointer payload for that file", file=sys.stderr)
            return 1
        print(entry.payload)
        return 0
    if args.command == "plan":
        for entry in select(all_entries, args):
            print(f"{entry.uri}\t{entry.rel_path}\t{len(entry.payload)} bytes")
        return 0
    if args.command == "sync":
        return cmd_sync(args, all_entries)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
