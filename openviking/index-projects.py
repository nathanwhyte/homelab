#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx"]
# ///
"""
Project Indexer for OpenViking.

Spawns one Claude CLI agent per project directory under ~/code to generate
a structured index, then uploads each as markdown to Viking so any future
agent session can search across projects.

Requires:
  OPENVIKING_URL  — e.g. https://viking.nathanwhyte.dev
  OPENVIKING_KEY  — API key for the OpenViking instance
  claude          — Claude Code CLI in PATH

Usage:
  uv run --script index-projects.py
  uv run --script index-projects.py --dry-run
  uv run --script index-projects.py --projects credit-coach,homelab
  uv run --script index-projects.py --verbose --concurrency 2
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ── Config ─────────────────────────────────────────────────────────

CODE_DIR = Path.home() / "code"
SKIP_DIRS = {"archive", "repos", "sources", "keys", "tokens"}
PROJECT_MARKERS = {
    ".git", "Cargo.toml", "package.json", "mix.exs", "pyproject.toml",
    "go.mod", "Makefile", "flake.nix", "docker-compose.yml", "Dockerfile",
    "setup.py", "requirements.txt",
}

OV_URL = os.environ.get("OPENVIKING_URL", "https://viking.nathanwhyte.dev")
OV_KEY = os.environ.get("OPENVIKING_KEY", "openviking-homelab")

VIKING_BASE_DIR = "viking://resources/projects"

CLAUDE_MODEL = "sonnet"
CLAUDE_BUDGET = "0.30"
CLAUDE_TIMEOUT = 300  # 5 minutes

INDEX_SCHEMA = json.dumps({
    "type": "object",
    "required": ["name", "description", "tech_stack", "languages", "status"],
    "properties": {
        "name": {"type": "string", "description": "Project name"},
        "description": {"type": "string", "description": "One-paragraph description of what this project does"},
        "tech_stack": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Frameworks, libraries, platforms (e.g. Axum, React, Phoenix, Kubernetes)"
        },
        "languages": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Programming languages used"
        },
        "status": {
            "type": "string",
            "enum": ["active", "maintenance", "archived", "experiment"],
            "description": "Project status"
        },
        "key_components": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "path": {"type": "string"},
                    "purpose": {"type": "string"}
                },
                "required": ["name", "path", "purpose"]
            },
            "description": "Major modules or components"
        },
        "entry_points": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Main entry points (e.g. src/main.rs, lib/app.ex)"
        },
        "deployment": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "description": "How it's deployed (k8s, docker, vercel, etc.)"},
                "url": {"type": "string", "description": "Production URL if any"},
                "namespace": {"type": "string", "description": "K8s namespace if applicable"}
            }
        },
        "commands": {
            "type": "object",
            "properties": {
                "build": {"type": "string"},
                "run": {"type": "string"},
                "test": {"type": "string"}
            },
            "description": "Key commands to build, run, and test"
        },
        "architecture_notes": {
            "type": "string",
            "description": "Brief notes on architecture, patterns, or anything notable"
        }
    },
    "additionalProperties": False,
})

AGENT_PROMPT = """\
Index this project for a searchable knowledge base. Be fast and factual.

1. Read README.md or AGENTS.md if present
2. Check project manifest (package.json, Cargo.toml, mix.exs, pyproject.toml, go.mod, etc.)
3. List top-level directory structure
4. Identify languages, frameworks, entry points, deployment method

Omit fields you can't determine. Output ONLY the JSON matching the schema.
"""

ALLOWED_TOOLS = "Read,Glob,Grep,Bash(git:*,ls:*,find:*,head:*,wc:*)"


# ── Discovery ──────────────────────────────────────────────────────

def discover_projects(only: list[str] | None = None) -> list[Path]:
    """Find project directories under CODE_DIR."""
    projects = []
    for entry in sorted(CODE_DIR.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        if entry.name in SKIP_DIRS:
            continue
        if only and entry.name not in only:
            continue
        # Check for project markers
        if any((entry / marker).exists() for marker in PROJECT_MARKERS):
            projects.append(entry)
    return projects


# ── Claude Agent ───────────────────────────────────────────────────

async def run_claude_agent(
    project_path: Path,
    semaphore: asyncio.Semaphore,
    verbose: bool = False,
) -> dict | None:
    """Spawn a Claude agent to index a single project."""
    name = project_path.name
    prompt = AGENT_PROMPT.format(project_path=project_path)

    cmd = [
        "claude",
        "-p", prompt,
        "--model", CLAUDE_MODEL,
        "--max-budget-usd", CLAUDE_BUDGET,
        "--output-format", "json",
        "--json-schema", INDEX_SCHEMA,
        "--allowedTools", ALLOWED_TOOLS,
    ]

    async with semaphore:
        print(f"  [{name}] Starting agent...")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(project_path),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=CLAUDE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            print(f"  [{name}] TIMEOUT after {CLAUDE_TIMEOUT}s")
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return None
        except Exception as e:
            print(f"  [{name}] ERROR: {e}")
            return None

        if verbose and stderr:
            for line in stderr.decode(errors="replace").splitlines():
                print(f"  [{name}] stderr: {line}")

        if proc.returncode != 0:
            print(f"  [{name}] FAILED (exit {proc.returncode})")
            if stderr:
                err_text = stderr.decode(errors="replace")[:500]
                print(f"  [{name}] {err_text}")
            return None

        try:
            # claude --output-format json wraps result in {"type":"result","result":...}
            outer = json.loads(stdout.decode())

            # Check for budget/error subtypes
            subtype = outer.get("subtype", "")
            if "error" in subtype:
                print(f"  [{name}] Agent ended with: {subtype}")

            result_text = outer.get("result", "")
            if not result_text:
                print(f"  [{name}] Empty result (subtype={subtype})")
                return None

            # The result text should be the JSON matching our schema
            # It might be wrapped in markdown code fences
            text = result_text.strip()
            if text.startswith("```"):
                lines = text.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                text = "\n".join(lines)

            index = json.loads(text)
            cost = outer.get("total_cost_usd", 0)
            print(f"  [{name}] OK (${cost:.3f})")
            return index
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(f"  [{name}] Failed to parse output: {e}")
            raw = stdout.decode(errors="replace")[:500]
            print(f"  [{name}] Raw: {raw}")
            return None


# ── Markdown Formatting ────────────────────────────────────────────

def format_as_markdown(index: dict) -> str:
    """Convert a project index dict to readable markdown."""
    lines = []
    name = index.get("name", "Unknown")
    lines.append(f"# {name}")
    lines.append("")

    if desc := index.get("description"):
        lines.append(desc)
        lines.append("")

    # Status
    if status := index.get("status"):
        lines.append(f"**Status:** {status}")
        lines.append("")

    # Languages & Tech Stack
    if langs := index.get("languages"):
        lines.append(f"**Languages:** {', '.join(langs)}")
    if tech := index.get("tech_stack"):
        lines.append(f"**Tech Stack:** {', '.join(tech)}")
    if langs or tech:
        lines.append("")

    # Key Components
    if components := index.get("key_components"):
        lines.append("## Key Components")
        lines.append("")
        for comp in components:
            purpose = comp.get("purpose", "")
            path = comp.get("path", "")
            lines.append(f"- **{comp['name']}** (`{path}`): {purpose}")
        lines.append("")

    # Entry Points
    if entries := index.get("entry_points"):
        lines.append("## Entry Points")
        lines.append("")
        for ep in entries:
            lines.append(f"- `{ep}`")
        lines.append("")

    # Deployment
    if deploy := index.get("deployment"):
        lines.append("## Deployment")
        lines.append("")
        if method := deploy.get("method"):
            lines.append(f"- **Method:** {method}")
        if url := deploy.get("url"):
            lines.append(f"- **URL:** {url}")
        if ns := deploy.get("namespace"):
            lines.append(f"- **Namespace:** {ns}")
        lines.append("")

    # Commands
    if cmds := index.get("commands"):
        lines.append("## Commands")
        lines.append("")
        for key in ("build", "run", "test"):
            if cmd := cmds.get(key):
                lines.append(f"- **{key}:** `{cmd}`")
        lines.append("")

    # Architecture Notes
    if notes := index.get("architecture_notes"):
        lines.append("## Architecture Notes")
        lines.append("")
        lines.append(notes)
        lines.append("")

    return "\n".join(lines)


def format_manifest(results: dict[str, dict]) -> str:
    """Create a top-level manifest summarizing all indexed projects."""
    lines = []
    lines.append("# Project Index Manifest")
    lines.append("")
    lines.append(f"*Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*")
    lines.append("")
    lines.append(f"**{len(results)} projects indexed**")
    lines.append("")

    # Summary table
    lines.append("| Project | Languages | Tech Stack | Status |")
    lines.append("|---------|-----------|------------|--------|")
    for name in sorted(results):
        idx = results[name]
        langs = ", ".join(idx.get("languages", []))
        tech = ", ".join(idx.get("tech_stack", [])[:3])
        status = idx.get("status", "unknown")
        lines.append(f"| [{name}](viking://resources/projects/{name}/{name}-index.md) | {langs} | {tech} | {status} |")
    lines.append("")

    # Quick descriptions
    lines.append("## Descriptions")
    lines.append("")
    for name in sorted(results):
        idx = results[name]
        desc = idx.get("description", "No description")
        lines.append(f"### {name}")
        lines.append(f"{desc}")
        lines.append("")

    return "\n".join(lines)


# ── Viking Upload ──────────────────────────────────────────────────

async def upload_to_viking(
    name: str,
    markdown: str,
    client: httpx.AsyncClient,
) -> bool:
    """Upload a project index to Viking."""
    resource_dir = f"{VIKING_BASE_DIR}/{name}"
    resource_name = f"{name}-index"

    try:
        # 1. Delete old resource (ignore 404)
        resp = await client.request(
            "DELETE",
            "/api/v1/fs",
            json={"uri": resource_dir, "recursive": True},
        )
        if resp.status_code not in (200, 404):
            data = resp.json()
            if data.get("status") == "error":
                err = data.get("error", {})
                # Ignore "not found" errors
                if "not found" not in str(err).lower():
                    print(f"  [{name}] Warning: delete returned {err}")

        # 2. Create directory
        resp = await client.post(
            "/api/v1/fs/mkdir",
            json={"uri": resource_dir},
        )

        # 3. Temp upload
        content_bytes = markdown.encode("utf-8")
        resp = await client.post(
            "/api/v1/resources/temp_upload",
            headers={"Content-Type": None},  # let httpx set multipart headers
            files={"file": (f"{resource_name}.md", content_bytes, "text/markdown")},
        )
        upload_result = resp.json()
        if upload_result.get("status") == "error":
            print(f"  [{name}] Upload failed: {upload_result}")
            return False
        temp_path = upload_result["result"]["temp_path"]

        # 4. Add as resource
        resp = await client.post(
            "/api/v1/resources",
            json={
                "temp_path": temp_path,
                "target_dir": resource_dir,
                "name": resource_name,
            },
        )
        result = resp.json()
        if result.get("status") == "error":
            print(f"  [{name}] Add resource failed: {result}")
            return False

        print(f"  [{name}] Uploaded to {result['result']['root_uri']}")
        return True

    except Exception as e:
        print(f"  [{name}] Upload error: {e}")
        return False


async def upload_manifest(markdown: str, client: httpx.AsyncClient) -> bool:
    """Upload the top-level manifest."""
    try:
        # Ensure base dir exists
        await client.post("/api/v1/fs/mkdir", json={"uri": VIKING_BASE_DIR})

        # Delete old manifest
        await client.request(
            "DELETE",
            "/api/v1/fs",
            json={"uri": f"{VIKING_BASE_DIR}/manifest", "recursive": True},
        )

        # Temp upload
        resp = await client.post(
            "/api/v1/resources/temp_upload",
            headers={"Content-Type": None},
            files={"file": ("manifest.md", markdown.encode("utf-8"), "text/markdown")},
        )
        upload_result = resp.json()
        if upload_result.get("status") == "error":
            print(f"  [manifest] Upload failed: {upload_result}")
            return False
        temp_path = upload_result["result"]["temp_path"]

        resp = await client.post(
            "/api/v1/resources",
            json={
                "temp_path": temp_path,
                "target_dir": VIKING_BASE_DIR,
                "name": "manifest",
            },
        )
        result = resp.json()
        if result.get("status") == "error":
            print(f"  [manifest] Add resource failed: {result}")
            return False

        print(f"  [manifest] Uploaded to {result['result']['root_uri']}")
        return True

    except Exception as e:
        print(f"  [manifest] Upload error: {e}")
        return False


# ── Main ───────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Index project directories into OpenViking")
    parser.add_argument("--projects", type=str, help="Comma-separated list of project names to index")
    parser.add_argument("--dry-run", action="store_true", help="Index but skip Viking upload")
    parser.add_argument("--concurrency", type=int, default=4, help="Max concurrent Claude agents")
    parser.add_argument("--verbose", action="store_true", help="Print Claude agent stderr")
    args = parser.parse_args()

    only = [p.strip() for p in args.projects.split(",")] if args.projects else None

    # Discover
    projects = discover_projects(only)
    if not projects:
        print("No projects found to index.")
        sys.exit(1)

    print(f"Found {len(projects)} projects to index:")
    for p in projects:
        print(f"  - {p.name}")
    print()

    # Index with Claude agents
    print("Indexing with Claude agents...")
    semaphore = asyncio.Semaphore(args.concurrency)
    tasks = [
        run_claude_agent(p, semaphore, verbose=args.verbose)
        for p in projects
    ]
    raw_results = await asyncio.gather(*tasks)

    # Collect successful results
    results: dict[str, dict] = {}
    for project, result in zip(projects, raw_results):
        if result is not None:
            results[project.name] = result

    print(f"\nSuccessfully indexed {len(results)}/{len(projects)} projects")

    if not results:
        print("No projects indexed successfully.")
        sys.exit(1)

    # Format as markdown
    markdowns: dict[str, str] = {}
    for name, index in results.items():
        markdowns[name] = format_as_markdown(index)

    manifest_md = format_manifest(results)

    if args.dry_run:
        print("\n--- DRY RUN: Showing results ---\n")
        for name in sorted(markdowns):
            print(f"{'='*60}")
            print(f"  {name}")
            print(f"{'='*60}")
            print(markdowns[name])
        print(f"{'='*60}")
        print("  MANIFEST")
        print(f"{'='*60}")
        print(manifest_md)
        return

    # Upload to Viking
    print(f"\nUploading to Viking ({OV_URL})...")
    async with httpx.AsyncClient(
        base_url=OV_URL,
        headers={"X-API-Key": OV_KEY, "Content-Type": "application/json"},
        timeout=120.0,
    ) as client:
        # Ensure base directory exists
        await client.post("/api/v1/fs/mkdir", json={"uri": VIKING_BASE_DIR})

        # Upload project indexes
        upload_tasks = [
            upload_to_viking(name, md, client)
            for name, md in markdowns.items()
        ]
        upload_results = await asyncio.gather(*upload_tasks)

        success = sum(1 for r in upload_results if r)
        print(f"\nUploaded {success}/{len(upload_results)} project indexes")

        # Upload manifest
        await upload_manifest(manifest_md, client)

    print("\nDone!")


if __name__ == "__main__":
    asyncio.run(main())
