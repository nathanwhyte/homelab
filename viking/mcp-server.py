#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "httpx[http2]", "boto3"]
# ///
"""
OpenViking MCP Server for Claude Code.

Exposes OpenViking's context database as MCP tools so Claude can
store, search, and retrieve agent context across sessions.

Requires:
  OPENVIKING_URL  — e.g. https://context.nathanwhyte.dev
  OPENVIKING_KEY  — API key for the OpenViking instance
"""

import os
import json
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("openviking")

OV_URL = os.environ.get("OPENVIKING_URL", "https://context.nathanwhyte.dev")
OV_KEY = os.environ.get("OPENVIKING_KEY", "")
OV_ACCOUNT = os.environ.get("OPENVIKING_ACCOUNT", "default")
OV_USER = os.environ.get("OPENVIKING_USER", "noot")


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=OV_URL,
        headers={
            "X-API-Key": OV_KEY,
            "X-OpenViking-Account": OV_ACCOUNT,
            "X-OpenViking-User": OV_USER,
            "Content-Type": "application/json",
        },
        timeout=None,
        http2=True,
    )


def _result(resp: httpx.Response):
    data = resp.json()
    if data.get("status") == "error":
        err = data.get("error", {})
        raise RuntimeError(f"OpenViking error: {err.get('message', data)}")
    return data.get("result")


def _join_resource_uri(parent_uri: str, name: str) -> str:
    parent = parent_uri.rstrip("/")
    return f"{parent}/{name}"


# ── Search & Retrieval ──────────────────────────────────────────────


@mcp.tool()
def viking_search(query: str, scope: str = "viking://") -> str:
    """Semantic search across OpenViking context — resources, memories, and skills.

    Returns ranked results with scores. Use this to find relevant context
    before reading full content.
    """
    with _client() as c:
        r = c.post("/api/v1/search/search", json={"query": query, "scope": scope})
        result = _result(r)
        lines = [f"Total: {result['total']} results"]
        for section in ("resources", "memories", "skills"):
            for item in result.get(section, []):
                lines.append(f"  [{item.get('score', 0):.3f}] {item['uri']}")
                abstract = item.get("abstract", "")
                if abstract:
                    lines.append(f"         {abstract[:120]}")
        return "\n".join(lines)


@mcp.tool()
def viking_find(query: str, target_uri: str = "viking://") -> str:
    """Directory-recursive semantic search — combines filesystem navigation with semantic matching.

    More targeted than viking_search. Use when you know the general area to search.
    """
    with _client() as c:
        r = c.post("/api/v1/search/find", json={"query": query, "uri": target_uri})
        result = _result(r)
        lines = [f"Total: {result['total']} results"]
        for section in ("resources", "memories", "skills"):
            for item in result.get(section, []):
                lines.append(f"  [{item.get('score', 0):.3f}] {item['uri']}")
        return "\n".join(lines)


@mcp.tool()
def viking_grep(pattern: str, uri: str = "viking://resources/") -> str:
    """Text pattern search across stored files (like grep)."""
    with _client() as c:
        r = c.post("/api/v1/search/grep", json={"pattern": pattern, "uri": uri})
        result = _result(r)
        lines = [f"Matches: {result.get('count', 0)}"]
        for m in result.get("matches", []):
            lines.append(f"  {m}")
        return "\n".join(lines)


# ── Filesystem ──────────────────────────────────────────────────────


@mcp.tool()
def viking_ls(uri: str = "viking://") -> str:
    """List contents of an OpenViking directory."""
    with _client() as c:
        r = c.get("/api/v1/fs/ls", params={"uri": uri})
        result = _result(r)
        lines = []
        for item in result:
            kind = "d" if item["isDir"] else "f"
            abstract = item.get("abstract", "")
            line = f"[{kind}] {item['uri']}"
            if abstract:
                line += f"  — {abstract[:80]}"
            lines.append(line)
        return "\n".join(lines) or "(empty)"


@mcp.tool()
def viking_tree(uri: str = "viking://", depth: int = 3) -> str:
    """Tree view of the OpenViking filesystem."""
    with _client() as c:
        r = c.get("/api/v1/fs/tree", params={"uri": uri, "level_limit": depth})
        result = _result(r)
        lines = []
        for item in result:
            indent = item.get("rel_path", "").count("/")
            kind = "d" if item["isDir"] else "f"
            lines.append(f"{'  ' * indent}[{kind}] {item.get('rel_path', item['uri'])}")
        return "\n".join(lines) or "(empty)"


# ── Content Reading ─────────────────────────────────────────────────


@mcp.tool()
def viking_read(uri: str) -> str:
    """Read the full content (L2) of a file in the OpenViking filesystem."""
    with _client() as c:
        r = c.get("/api/v1/content/read", params={"uri": uri})
        return _result(r) or "(empty)"


@mcp.tool()
def viking_abstract(uri: str) -> str:
    """Get the L0 abstract (short summary) of a resource or directory."""
    with _client() as c:
        r = c.get("/api/v1/content/abstract", params={"uri": uri})
        return _result(r) or "(no abstract)"


@mcp.tool()
def viking_overview(uri: str) -> str:
    """Get the L1 overview (detailed summary with navigation) of a directory."""
    with _client() as c:
        r = c.get("/api/v1/content/overview", params={"uri": uri})
        return _result(r) or "(no overview)"


# ── Resource Management ─────────────────────────────────────────────


@mcp.tool()
def viking_add_text(
    content: str, name: str, target_dir: str = "viking://resources/"
) -> str:
    """Add text content as a resource to OpenViking.

    Use this to store notes, documentation, code snippets, or any text
    that should be searchable and summarized.

    Args:
        content: The text content to store
        name: A short name for the resource (used in the URI)
        target_dir: Viking directory to store in (e.g. viking://resources/projects/myproject)
    """
    dest_uri = _join_resource_uri(target_dir, name)
    # Use a dedicated client for multipart temp upload (no Content-Type default)
    upload_headers = {
        "X-API-Key": OV_KEY,
        "X-OpenViking-Account": OV_ACCOUNT,
        "X-OpenViking-User": OV_USER,
    }
    with httpx.Client(headers=upload_headers, timeout=None, http2=True) as uc:
        upload = uc.post(
            f"{OV_URL}/api/v1/resources/temp_upload",
            files={"file": (f"{name}.md", content.encode(), "text/markdown")},
        )
    upload_data = upload.json()
    if upload_data.get("status") == "error":
        err = upload_data.get("error", {})
        raise RuntimeError(f"Temp upload failed: {err.get('message', upload_data)}")
    temp_path = upload_data["result"]["temp_path"]
    # Add as resource at the target location
    with _client() as c:
        r = c.post(
            "/api/v1/resources",
            json={
                "temp_path": temp_path,
                "to": dest_uri,
            },
        )
        result = _result(r)
        return f"Added: {result['root_uri']}"


MIME_MAP = {
    ".py": "text/x-python",
    ".sh": "text/x-shellscript",
    ".bash": "text/x-shellscript",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".json": "application/json",
    ".toml": "text/toml",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".js": "text/javascript",
    ".ts": "text/typescript",
}


@mcp.tool()
def viking_add_file(
    content: str,
    name: str,
    target_dir: str = "viking://resources/",
    extension: str = "",
) -> str:
    """Add a file to OpenViking, preserving its original extension and MIME type.

    Use this for scripts, configs, and other files where exact content
    retrieval matters. The file is stored as-is and retrievable via viking_read.

    Args:
        content: The file content to store
        name: Filename for the resource (e.g. 'backup-db.sh', 'values.yaml')
        target_dir: Viking directory to store in (e.g. viking://resources/filestore/scripts)
        extension: File extension override (e.g. '.py'). Auto-detected from name if omitted.
    """
    # Detect extension from name if not provided
    ext = extension
    if not ext:
        for suffix in MIME_MAP:
            if name.endswith(suffix):
                ext = suffix
                break
    if not ext:
        ext = ".txt"
    mime = MIME_MAP.get(ext, "text/plain")
    # Ensure name has the extension
    filename = name if name.endswith(ext) else f"{name}{ext}"

    dest_uri = _join_resource_uri(target_dir, name.rsplit(".", 1)[0] if "." in name else name)
    upload_headers = {
        "X-API-Key": OV_KEY,
        "X-OpenViking-Account": OV_ACCOUNT,
        "X-OpenViking-User": OV_USER,
    }
    with httpx.Client(headers=upload_headers, timeout=None, http2=True) as uc:
        upload = uc.post(
            f"{OV_URL}/api/v1/resources/temp_upload",
            files={"file": (filename, content.encode(), mime)},
        )
    upload_data = upload.json()
    if upload_data.get("status") == "error":
        err = upload_data.get("error", {})
        raise RuntimeError(f"Temp upload failed: {err.get('message', upload_data)}")
    temp_path = upload_data["result"]["temp_path"]
    with _client() as c:
        r = c.post(
            "/api/v1/resources",
            json={
                "temp_path": temp_path,
                "to": dest_uri,
            },
        )
        result = _result(r)
        return f"Added: {result['root_uri']}"


@mcp.tool()
def viking_mkdir(uri: str) -> str:
    """Create a directory in the OpenViking filesystem."""
    with _client() as c:
        r = c.post("/api/v1/fs/mkdir", json={"uri": uri})
        result = _result(r)
        return f"Created: {result['uri']}"


@mcp.tool()
def viking_rm(uri: str) -> str:
    """Remove a file or directory from the OpenViking filesystem."""
    with _client() as c:
        r = c.request("DELETE", "/api/v1/fs", params={"uri": uri, "recursive": "true"})
        _result(r)
        return f"Removed: {uri}"


# ── Raw File Store (S3-backed) ────────────────────────────────────
# Stores files directly in Garage S3, bypassing OpenViking's parser.
# Use for scripts, configs, and other files needing exact retrieval.

import boto3
from botocore.config import Config as BotoConfig

_S3_ENDPOINT = os.environ.get("GARAGE_S3_ENDPOINT", "http://10.43.237.247:3900")
_S3_ACCESS_KEY = os.environ.get("GARAGE_S3_ACCESS_KEY", "GKb0157d7a1f22c8e39c5464fc")
_S3_SECRET_KEY = os.environ.get("GARAGE_S3_SECRET_KEY", "870141d4a56187469b076fa35840a8b0422313c43df47d8d367879c8bf603866")
_S3_BUCKET = os.environ.get("FILESTORE_BUCKET", "filestore")
_S3_REGION = "garage"


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=_S3_ENDPOINT,
        aws_access_key_id=_S3_ACCESS_KEY,
        aws_secret_access_key=_S3_SECRET_KEY,
        region_name=_S3_REGION,
        config=BotoConfig(signature_version="s3v4"),
    )


def _ensure_bucket():
    """Create the filestore bucket if it doesn't exist."""
    s3 = _s3()
    try:
        s3.head_bucket(Bucket=_S3_BUCKET)
    except Exception:
        s3.create_bucket(Bucket=_S3_BUCKET)


@mcp.tool()
def file_store(content: str, path: str) -> str:
    """Store a file in the raw filestore (exact content, no parsing).

    Files are stored in Garage S3 and retrievable exactly as uploaded.
    Use for scripts, configs, skills, and other files needing exact retrieval.

    Args:
        content: The file content to store
        path: File path in the store (e.g. 'scripts/backfill-claude-mem.py',
              'configs/garage-values.yaml', 'skills/index-codebase.md')
    """
    _ensure_bucket()
    s3 = _s3()
    s3.put_object(Bucket=_S3_BUCKET, Key=path, Body=content.encode("utf-8"))
    return f"Stored: filestore://{path} ({len(content)} chars)"


@mcp.tool()
def file_get(path: str) -> str:
    """Retrieve a file from the raw filestore (exact content).

    Returns the file content exactly as it was stored.

    Args:
        path: File path in the store (e.g. 'scripts/backfill-claude-mem.py')
    """
    s3 = _s3()
    try:
        obj = s3.get_object(Bucket=_S3_BUCKET, Key=path)
        return obj["Body"].read().decode("utf-8")
    except s3.exceptions.NoSuchKey:
        raise RuntimeError(f"File not found: filestore://{path}")


@mcp.tool()
def file_list(prefix: str = "") -> str:
    """List files in the raw filestore.

    Args:
        prefix: Optional path prefix to filter (e.g. 'scripts/', 'configs/')
    """
    _ensure_bucket()
    s3 = _s3()
    resp = s3.list_objects_v2(Bucket=_S3_BUCKET, Prefix=prefix)
    contents = resp.get("Contents", [])
    if not contents:
        return f"(empty) filestore://{prefix}"
    lines = [f"filestore://{prefix} ({len(contents)} files)"]
    for obj in contents:
        size = obj["Size"]
        key = obj["Key"]
        if size < 1024:
            size_str = f"{size}B"
        else:
            size_str = f"{size // 1024}KB"
        lines.append(f"  {key} ({size_str})")
    return "\n".join(lines)


# ── Sessions ────────────────────────────────────────────────────────


@mcp.tool()
def viking_session_create() -> str:
    """Create a new OpenViking session for tracking a conversation."""
    with _client() as c:
        r = c.post("/api/v1/sessions", json={})
        result = _result(r)
        return json.dumps(result, indent=2)


@mcp.tool()
def viking_session_message(session_id: str, role: str, content: str) -> str:
    """Add a message to an OpenViking session.

    Args:
        session_id: The session ID from viking_session_create
        role: Either "user" or "assistant"
        content: The message text
    """
    with _client() as c:
        r = c.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={
                "role": role,
                "content": content,
            },
        )
        result = _result(r)
        return f"Message added (count: {result['message_count']})"


@mcp.tool()
def viking_session_commit(session_id: str) -> str:
    """Commit and archive an OpenViking session, extracting long-term memories."""
    with _client() as c:
        r = c.post(f"/api/v1/sessions/{session_id}/commit", json={})
        result = _result(r)
        return json.dumps(result, indent=2)


# ── Status ──────────────────────────────────────────────────────────


@mcp.tool()
def viking_status() -> str:
    """Check OpenViking system health and queue status."""
    with _client() as c:
        health = c.get("/health").json()
        status = c.get("/api/v1/system/status", headers={"X-API-Key": OV_KEY}).json()
        lines = [
            f"Health: {health.get('status')} (v{health.get('version')})",
            f"Initialized: {status.get('result', {}).get('initialized')}",
        ]
        return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
