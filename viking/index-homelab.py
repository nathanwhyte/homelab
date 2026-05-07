#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx"]
# ///
"""
Index the homelab repository into OpenViking via the coordinator.
Uses async HTTP for concurrent indexing.

Usage:
  python viking/index-homelab.py [--coordinator URL] [--concurrency N] [--verify-only] [--dry-run]

Env vars:
  OV_COORDINATOR_URL  default: http://openviking.viking.svc.cluster.local:1933
  OV_API_KEY          default: (from configmap)
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

# ── Configuration ────────────────────────────────────────────────────

DEFAULT_COORDINATOR = "http://openviking.viking.svc.cluster.local:1933"
DEFAULT_API_KEY = ""  # Must be set via OV_API_KEY env var
DEFAULT_REPO = "/home/natew/code/homelab"
TARGET_BASE = "viking://resources/projects/homelab"

INDEXABLE_EXTENSIONS = {
    ".yaml", ".yml", ".md", ".sh", ".py", ".json", ".toml", ".conf",
}

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".claude", "vendor", "charts",
}

# Vendored helm chart path to skip entirely
SKIP_PATHS = {"garage/garage"}

MIME_MAP = {
    ".py": "text/x-python",
    ".sh": "text/x-shellscript",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".json": "application/json",
    ".toml": "text/toml",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".js": "text/javascript",
    ".ts": "text/typescript",
    ".conf": "text/plain",
}

MAX_FILE_SIZE = 100 * 1024  # 100KB


# ── Helpers ──────────────────────────────────────────────────────────


def collect_files(repo_path: Path) -> list[Path]:
    """Walk the repo and collect indexable files."""
    files = []
    for path in sorted(repo_path.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(repo_path)
        # Skip excluded directories
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        # Skip vendored paths
        rel_str = str(rel)
        if any(rel_str.startswith(sp) for sp in SKIP_PATHS):
            continue
        # Skip secret files
        if ".secret." in path.name:
            continue
        # Skip non-indexable extensions
        if path.suffix not in INDEXABLE_EXTENSIONS:
            continue
        # Skip large files
        if path.stat().st_size > MAX_FILE_SIZE:
            continue
        # Skip binary files (quick heuristic: first 1KB must be valid UTF-8)
        try:
            with open(path, "rb") as f:
                f.read(1024).decode("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        files.append(path)
    return files


def collect_dirs(files: list[Path], repo_path: Path) -> list[str]:
    """Collect unique directory URIs from the file list."""
    dirs: set[str] = set()
    for f in files:
        rel = f.relative_to(repo_path)
        # Collect all parent directories
        for parent in rel.parents:
            if parent == Path("."):
                continue
            dirs.add(str(parent))
    return sorted(dirs)


def resource_uri(rel_path: Path) -> str:
    """Build the OV resource URI, stripping the file extension."""
    stem = rel_path.with_suffix("")
    return f"{TARGET_BASE}/{stem}"


def _error_msg(data: dict) -> str:
    """Extract error message from an OV API response."""
    err = data.get("error", {})
    return err.get("message", str(data)) if isinstance(err, dict) else str(err)


def upload_headers(api_key: str) -> dict[str, str]:
    """Headers for multipart temp_upload (no Content-Type)."""
    return {
        "X-API-Key": api_key,
        "X-OpenViking-Account": "default",
        "X-OpenViking-User": "noot",
    }


def json_headers(api_key: str) -> dict[str, str]:
    """Headers for JSON API calls."""
    return {
        "X-API-Key": api_key,
        "X-OpenViking-Account": "default",
        "X-OpenViking-User": "noot",
        "Content-Type": "application/json",
    }


# ── Phase 1: Directory Creation ─────────────────────────────────────


async def create_directories(
    client,  # httpx.AsyncClient
    dirs: list[str],
    headers: dict[str, str],
    semaphore: asyncio.Semaphore,
) -> int:
    """Create OV directories. Returns count of directories created."""
    created = 0

    async def mkdir(d: str) -> None:
        nonlocal created
        uri = f"{TARGET_BASE}/{d}"
        async with semaphore:
            try:
                resp = await client.post(
                    "/api/v1/fs/mkdir", json={"uri": uri}, headers=headers,
                )
                data = resp.json()
                if data.get("status") == "ok":
                    created += 1
                    print(f"  mkdir: {uri}")
                elif "already exists" in str(data.get("error", "")):
                    pass  # Fine — directory exists
                else:
                    print(f"  mkdir error: {uri} — {data}", file=sys.stderr)
            except Exception as e:
                print(f"  mkdir exception: {uri} — {e}", file=sys.stderr)

    print(f"Creating {len(dirs)} directories...")
    await asyncio.gather(*(mkdir(d) for d in dirs))
    print(f"Directories created: {created}")
    return created


# ── Phase 2: File Indexing ───────────────────────────────────────────


async def index_file(
    client,  # httpx.AsyncClient
    file_path: Path,
    repo_path: Path,
    up_headers: dict[str, str],
    js_headers: dict[str, str],
    semaphore: asyncio.Semaphore,
    index: int,
    total: int,
) -> tuple[bool, str]:
    """Index a single file. Returns (success, message)."""
    rel = file_path.relative_to(repo_path)
    uri = resource_uri(rel)
    ext = file_path.suffix
    mime = MIME_MAP.get(ext, "text/plain")
    filename = file_path.name

    async with semaphore:
        try:
            content = file_path.read_bytes()

            # Step 1: temp_upload (route to same worker as step 2 via header)
            route_headers = {**up_headers, "X-OV-Route-To": uri}
            resp = await client.post(
                "/api/v1/resources/temp_upload",
                files={"file": (filename, content, mime)},
                headers=route_headers,
            )
            data = resp.json()
            if data.get("status") != "ok":
                return False, f"upload failed: {_error_msg(data)}"

            temp_path = data["result"]["temp_path"]

            # Step 2: add resource
            resp = await client.post(
                "/api/v1/resources",
                json={"temp_path": temp_path, "to": uri},
                headers=js_headers,
            )
            data = resp.json()
            if data.get("status") != "ok":
                return False, f"resource add failed: {_error_msg(data)}"

            print(f"[{index}/{total}] Indexed: {uri}")
            return True, ""

        except Exception as e:
            return False, str(e)


async def index_files(
    coordinator: str,
    api_key: str,
    files: list[Path],
    repo_path: Path,
    concurrency: int,
) -> tuple[int, int, float]:
    """Index all files. Returns (indexed, errors, elapsed)."""
    import httpx  # noqa: lazy import — not needed for dry-run

    semaphore = asyncio.Semaphore(concurrency)
    up_h = upload_headers(api_key)
    js_h = json_headers(api_key)

    start = time.monotonic()

    async with httpx.AsyncClient(
        base_url=coordinator, timeout=None,
    ) as client:
        # Phase 1: directories
        dirs = collect_dirs(files, repo_path)
        await create_directories(client, dirs, js_h, semaphore)

        # Phase 2: files
        print(f"\nIndexing {len(files)} files (concurrency={concurrency})...")
        tasks = [
            index_file(client, f, repo_path, up_h, js_h, semaphore, i + 1, len(files))
            for i, f in enumerate(files)
        ]
        results = await asyncio.gather(*tasks)

    elapsed = time.monotonic() - start
    indexed = sum(1 for ok, _ in results if ok)
    errors = sum(1 for ok, _ in results if not ok)

    # Print errors
    for (ok, msg), f in zip(results, files):
        if not ok:
            rel = f.relative_to(repo_path)
            print(f"  ERROR: {rel} — {msg}", file=sys.stderr)

    return indexed, errors, elapsed


# ── Phase 4: Verification ───────────────────────────────────────────

VERIFICATION_QUERIES = [
    ("GPU tuning LACT power profile", "homelab/gpu/"),
    ("Qwen3 llamacpp ROCm deployment", "homelab/"),
    ("OpenViking AGFS S3 configuration", "homelab/viking/openviking-configmap"),
    ("Garage Helm S3 storage", "homelab/garage/"),
    ("flannel subnet inotify", "homelab/"),
]


async def verify(coordinator: str, api_key: str) -> tuple[int, int]:
    """Run verification queries. Returns (passed, total)."""
    import httpx  # noqa: lazy import — not needed for dry-run

    headers = json_headers(api_key)
    passed = 0
    total = len(VERIFICATION_QUERIES)

    print("\n=== Verification ===")
    async with httpx.AsyncClient(
        base_url=coordinator, timeout=None,
    ) as client:
        for query, expected in VERIFICATION_QUERIES:
            try:
                resp = await client.post(
                    "/api/v1/search/search",
                    json={"query": query},
                    headers=headers,
                )
                data = resp.json()
                result = data.get("result", {})
                result_total = result.get("total", 0)
                uris = [
                    r["uri"]
                    for section in ("resources", "memories", "skills")
                    for r in result.get(section, [])
                ]
                match = any(expected in uri for uri in uris)
                if match:
                    passed += 1
                status = "PASS" if match else "FAIL"
                print(f"  {status}: {query} -> {result_total} results, match={match}")
            except Exception as e:
                print(f"  ERROR: {query} -> {e}")

    print(f"Verification: {passed}/{total} passed")
    return passed, total


# ── CLI ──────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Index the homelab repo into OpenViking",
    )
    parser.add_argument(
        "--coordinator",
        default=os.environ.get("OV_COORDINATOR_URL", DEFAULT_COORDINATOR),
        help="Coordinator URL",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OV_API_KEY", DEFAULT_API_KEY),
        help="OV API key",
    )
    parser.add_argument(
        "--repo-path",
        default=DEFAULT_REPO,
        type=Path,
        help="Path to homelab repo",
    )
    parser.add_argument(
        "--concurrency",
        default=10,
        type=int,
        help="Max concurrent uploads",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Skip indexing, just run verification",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files that would be indexed without uploading",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    repo = args.repo_path.resolve()

    if not repo.is_dir():
        print(f"Error: repo path does not exist: {repo}", file=sys.stderr)
        sys.exit(1)

    # Collect files
    files = collect_files(repo)
    print(f"Found {len(files)} indexable files in {repo}")

    if args.dry_run:
        print("\n=== Dry Run — files that would be indexed ===")
        for f in files:
            rel = f.relative_to(repo)
            uri = resource_uri(rel)
            size = f.stat().st_size
            print(f"  {rel} ({size:,} bytes) -> {uri}")
        print(f"\nTotal: {len(files)} files")
        return

    if args.verify_only:
        await verify(args.coordinator, args.api_key)
        return

    # Index
    indexed, errors, elapsed = await index_files(
        args.coordinator, args.api_key, files, repo, args.concurrency,
    )

    # Summary
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)
    throughput = indexed / elapsed if elapsed > 0 else 0

    print(f"\n=== Indexing Summary ===")
    print(f"Files indexed: {indexed}")
    print(f"Errors: {errors}")
    print(f"Elapsed: {minutes}m {seconds}s")
    print(f"Throughput: {throughput:.2f} files/sec")

    # Verification
    await verify(args.coordinator, args.api_key)


if __name__ == "__main__":
    asyncio.run(main())
