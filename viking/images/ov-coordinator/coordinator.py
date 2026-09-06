"""
OV Coordinator Proxy — routes writes by URI hash, federates reads across workers.

Sits in front of N OpenViking worker pods (StatefulSet) and provides a single
unified API endpoint that is backward-compatible with the existing openviking
ClusterIP service.

Env vars:
  OV_WORKERS  — comma-separated base URLs (e.g. http://ov-worker-0...:1933,...)
  OV_API_KEY  — API key forwarded to workers
  PORT        — listen port (default 1933)
"""

import asyncio
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WORKERS = [u.strip() for u in os.environ.get("OV_WORKERS", "").split(",") if u.strip()]
API_KEY = os.environ.get("OV_API_KEY", "")
PORT = int(os.environ.get("PORT", "1933"))
MERGED_URL = os.environ.get("OV_MERGED_URL", "")
MERGE_STATUS_URL = os.environ.get("OV_MERGE_STATUS_URL", "")

HEADERS = {
    "X-API-Key": API_KEY,
    "X-OpenViking-Account": "default",
    "X-OpenViking-User": "noot",
    "Content-Type": "application/json",
}

HEALTH_INTERVAL = 30  # seconds between health polls

logger = logging.getLogger("ov-coordinator")

# ---------------------------------------------------------------------------
# Worker health state
# ---------------------------------------------------------------------------

worker_healthy: dict[str, bool] = {w: True for w in WORKERS}
_rr_counter = 0  # round-robin counter for stateless routes

# Merged instance state (populated by _health_loop polling the merge service)
merged_healthy: bool = False
merged_stale: bool = True

# Temp file store: maps temp_path -> (worker_url, raw_body, content_type)
# Ensures POST /resources can re-upload to the hash-routed worker if
# temp_upload landed on a different one. Entries auto-expire on use.
_temp_store: dict[str, tuple[str, bytes, str]] = {}


def healthy_workers() -> list[str]:
    return [w for w in WORKERS if worker_healthy.get(w)]


def _hash_route(key: str) -> str:
    """Deterministic worker selection by MD5 of key."""
    idx = hashlib.md5(key.encode()).digest()[0] % len(WORKERS)
    target = WORKERS[idx]
    logger.debug("hash_route key=%s -> worker[%d] %s", key, idx, target)
    return target


def _round_robin() -> str:
    global _rr_counter
    hw = healthy_workers()
    if not hw:
        raise RuntimeError("no healthy workers")
    target = hw[_rr_counter % len(hw)]
    _rr_counter += 1
    return target


def _any_single() -> str:
    """Pick a healthy worker via round-robin (reuses the same counter as _round_robin)."""
    return _round_robin()


def _use_merged() -> bool:
    """Whether to route reads to the merged instance instead of fan-out."""
    return bool(MERGED_URL) and merged_healthy and not merged_stale


# ---------------------------------------------------------------------------
# Background health monitor
# ---------------------------------------------------------------------------


async def _health_loop():
    global merged_healthy, merged_stale
    async with httpx.AsyncClient(timeout=120) as client:
        while True:
            for w in WORKERS:
                try:
                    r = await client.get(f"{w}/health")
                    worker_healthy[w] = r.status_code == 200
                except Exception:
                    worker_healthy[w] = False
            # Poll merge service status
            if MERGE_STATUS_URL:
                try:
                    r = await client.get(f"{MERGE_STATUS_URL}/merge-status")
                    if r.status_code == 200:
                        data = r.json()
                        merged_healthy = data.get("healthy", False)
                        merged_stale = data.get("stale", True)
                    else:
                        merged_healthy = False
                        merged_stale = True
                except Exception:
                    merged_healthy = False
                    merged_stale = True
            await asyncio.sleep(HEALTH_INTERVAL)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

_proxy_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _proxy_client
    _proxy_client = httpx.AsyncClient(timeout=None)
    task = asyncio.create_task(_health_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await _proxy_client.aclose()
    _proxy_client = None


app = FastAPI(title="OV Coordinator", lifespan=lifespan)

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _forward_json(
    client: httpx.AsyncClient,
    method: str,
    worker: str,
    path: str,
    body: bytes | None,
    query: str,
) -> httpx.Response:
    url = f"{worker}{path}"
    if query:
        url = f"{url}?{query}"
    return await client.request(method, url, content=body, headers=HEADERS)


async def _forward_raw(
    client: httpx.AsyncClient,
    method: str,
    worker: str,
    path: str,
    body: bytes | None,
    query: str,
    content_type: str,
) -> httpx.Response:
    """Forward non-JSON requests (e.g. multipart uploads)."""
    url = f"{worker}{path}"
    if query:
        url = f"{url}?{query}"
    hdrs = {**HEADERS, "Content-Type": content_type}
    return await client.request(method, url, content=body, headers=hdrs)


def _build_response(resp: httpx.Response, partial: bool = False) -> Response:
    headers = {}
    if partial:
        headers["X-OV-Partial"] = "true"
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
        headers=headers,
    )


def _error(status: int, msg: str) -> Response:
    body = json.dumps({"status": "error", "error": {"message": msg}})
    return Response(content=body, status_code=status, media_type="application/json")


# ---------------------------------------------------------------------------
# Fan-out helpers
# ---------------------------------------------------------------------------


async def _fanout_search(
    client: httpx.AsyncClient, path: str, body: bytes, query: str
) -> Response:
    """Fan out to all healthy workers, merge results by score, dedup by URI."""
    targets = healthy_workers()
    if not targets:
        return _error(503, "all workers down")

    partial = len(targets) < len(WORKERS)
    tasks = [_forward_json(client, "POST", w, path, body, query) for w in targets]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    merged: dict[str, dict] = {}  # uri -> item (keep highest score)
    total = 0

    for r in results:
        if isinstance(r, Exception):
            partial = True
            continue
        if r.status_code != 200:
            partial = True
            continue
        data = r.json()
        if data.get("status") != "ok":
            continue
        result = data.get("result", {})
        total += result.get("total", 0)
        for section in ("resources", "memories", "skills"):
            for item in result.get(section, []):
                uri = item.get("uri", "")
                score = item.get("score", 0)
                if uri not in merged or score > merged[uri].get("score", 0):
                    item["_section"] = section
                    merged[uri] = item

    # Sort by score descending
    sorted_items = sorted(
        merged.values(), key=lambda x: x.get("score", 0), reverse=True
    )

    # Rebuild sectioned response
    out: dict[str, list] = {"resources": [], "memories": [], "skills": []}
    for item in sorted_items:
        sec = item.pop("_section", "resources")
        out[sec].append(item)

    body_out = json.dumps(
        {
            "status": "ok",
            "result": {"total": len(merged), **out},
        }
    )
    headers = {}
    if partial:
        headers["X-OV-Partial"] = "true"
    return Response(
        content=body_out,
        status_code=200,
        media_type="application/json",
        headers=headers,
    )


async def _fanout_merge_list(
    client: httpx.AsyncClient, method: str, path: str, body: bytes | None, query: str
) -> Response:
    """Fan out, merge list results (find/grep). Same dedup logic as search."""
    targets = healthy_workers()
    if not targets:
        return _error(503, "all workers down")

    partial = len(targets) < len(WORKERS)
    tasks = [_forward_json(client, method, w, path, body, query) for w in targets]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    merged: dict[str, dict] = {}
    for r in results:
        if isinstance(r, Exception):
            partial = True
            continue
        if r.status_code != 200:
            partial = True
            continue
        data = r.json()
        if data.get("status") != "ok":
            continue
        result = data.get("result", {})
        for section in ("resources", "memories", "skills", "matches"):
            for item in result.get(section, []):
                if not isinstance(item, dict):
                    continue
                uri = item.get("uri", "")
                score = item.get("score", 0)
                if uri not in merged or score > merged[uri].get("score", 0):
                    item["_section"] = section
                    merged[uri] = item

    sorted_items = sorted(
        merged.values(), key=lambda x: x.get("score", 0), reverse=True
    )

    out: dict[str, list] = {}
    for item in sorted_items:
        sec = item.pop("_section", "resources")
        out.setdefault(sec, []).append(item)

    body_out = json.dumps({"status": "ok", "result": {"total": len(merged), **out}})
    headers = {"X-OV-Partial": "true"} if partial else {}
    return Response(
        content=body_out,
        status_code=200,
        media_type="application/json",
        headers=headers,
    )


async def _fanout_first(
    client: httpx.AsyncClient, method: str, path: str, body: bytes | None, query: str
) -> Response:
    """Fan out to all workers, return first non-empty successful result."""
    targets = healthy_workers()
    if not targets:
        return _error(503, "all workers down")

    partial = len(targets) < len(WORKERS)
    tasks = [_forward_json(client, method, w, path, body, query) for w in targets]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        if isinstance(r, Exception):
            partial = True
            continue
        if r.status_code != 200:
            continue
        data = r.json()
        if data.get("status") == "ok" and data.get("result"):
            resp = _build_response(r, partial)
            return resp

    # If no result found, return first non-exception response or 404
    for r in results:
        if not isinstance(r, Exception):
            return _build_response(r, partial)
    return _error(404, "no result from any worker")


async def _broadcast(
    client: httpx.AsyncClient, method: str, path: str, body: bytes | None, query: str
) -> Response:
    """Send to ALL workers, return first success."""
    targets = healthy_workers()
    if not targets:
        return _error(503, "all workers down")

    tasks = [_forward_json(client, method, w, path, body, query) for w in targets]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        if not isinstance(r, Exception) and r.status_code < 400:
            return _build_response(r)
    # Return first non-exception even if error
    for r in results:
        if not isinstance(r, Exception):
            return _build_response(r)
    return _error(503, "all workers failed")


# ---------------------------------------------------------------------------
# Route dispatcher
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    status = {w: worker_healthy.get(w, False) for w in WORKERS}
    all_up = all(status.values())
    return {
        "status": "ok" if all_up else "degraded",
        "workers": status,
        "healthy_count": sum(1 for v in status.values() if v),
        "total_count": len(WORKERS),
        "merged": {
            "url": MERGED_URL or None,
            "healthy": merged_healthy,
            "stale": merged_stale,
            "active": _use_merged(),
        },
    }


@app.get("/ready")
async def ready():
    if all(worker_healthy.get(w, False) for w in WORKERS):
        return {"status": "ok"}
    return Response(
        content='{"status": "not_ready"}',
        status_code=503,
        media_type="application/json",
    )


async def _hash_route_forward(
    client: httpx.AsyncClient,
    body: bytes,
    field: str,
    full_path: str,
    query: str,
    method: str,
) -> Response:
    """Parse body JSON, hash-route by field, forward to owning worker."""
    try:
        data = json.loads(body)
        key = data.get(field, "")
    except Exception:
        key = ""
    if not key:
        return _error(400, f"missing '{field}' field")
    target = _hash_route(key)
    if not worker_healthy.get(target, False):
        return _error(503, f"owning worker {target} is down")
    logger.debug("hash_route_forward %s -> %s", field, target)
    r = await _forward_json(client, method, target, full_path, body, query)
    return _build_response(r)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(request: Request, path: str):
    body = await request.body()
    query = str(request.url.query) if request.url.query else ""
    method = request.method
    full_path = f"/{path}"
    content_type = request.headers.get("content-type", "application/json")
    client = _proxy_client

    logger.debug("proxy %s %s", method, full_path)

    # --- Write routes (hash or broadcast) ---

    if full_path == "/api/v1/resources/temp_upload" and method == "POST":
        # Route via X-OV-Route-To header if provided, else round-robin.
        # Store the upload body so POST /resources can re-upload to the
        # correct hash-routed worker if it differs.
        route_key = request.headers.get("x-ov-route-to", "")
        if route_key:
            target = _hash_route(route_key)
        else:
            target = _round_robin()
        logger.debug("temp_upload -> %s (route_key=%s)", target, route_key or "none")
        r = await _forward_raw(
            client, method, target, full_path, body, query, content_type
        )
        if r.status_code < 400:
            try:
                data = r.json()
                temp_path = data.get("result", {}).get("temp_path", "")
                if temp_path:
                    _temp_store[temp_path] = (target, body, content_type)
            except Exception:
                pass
        return _build_response(r)

    if full_path == "/api/v1/resources" and method == "POST":
        # Hash-route by "to" URI. If the temp file was uploaded to a
        # different worker, re-upload it to the target worker first.
        try:
            data = json.loads(body)
            to_uri = data.get("to", "")
            temp_path = data.get("temp_path", "")
        except Exception:
            to_uri = ""
            temp_path = ""
        if not to_uri:
            return _error(400, "missing 'to' field")
        target = _hash_route(to_uri)
        if not worker_healthy.get(target, False):
            return _error(503, f"owning worker {target} is down")

        # Re-upload temp file if it landed on a different worker
        stored = _temp_store.pop(temp_path, None)
        if stored:
            original_worker, original_body, original_ct = stored
            if original_worker != target:
                logger.debug(
                    "re-uploading temp %s from %s to %s",
                    temp_path,
                    original_worker,
                    target,
                )
                reup = await _forward_raw(
                    client,
                    "POST",
                    target,
                    "/api/v1/resources/temp_upload",
                    original_body,
                    "",
                    original_ct,
                )
                if reup.status_code < 400:
                    try:
                        new_temp = reup.json().get("result", {}).get("temp_path", "")
                        if new_temp:
                            data["temp_path"] = new_temp
                            body = json.dumps(data).encode()
                    except Exception:
                        pass

        r = await _forward_json(client, method, target, full_path, body, query)
        return _build_response(r)

    if full_path == "/api/v1/fs/mkdir" and method == "POST":
        return await _broadcast(client, method, full_path, body, query)

    if full_path.startswith("/api/v1/fs") and method == "DELETE":
        return await _broadcast(client, method, full_path, body, query)

    if full_path == "/api/v1/content/reindex" and method == "POST":
        return await _hash_route_forward(client, body, "uri", full_path, query, method)

    # --- Read routes (fan-out + merge) ---

    if full_path == "/api/v1/search/search" and method == "POST":
        if _use_merged():
            try:
                r = await _forward_json(
                    client, method, MERGED_URL, full_path, body, query
                )
                if r.status_code == 200:
                    return _build_response(r)
            except Exception:
                logger.debug("merged search failed, falling back to fan-out")
        return await _fanout_search(client, full_path, body, query)

    if full_path in ("/api/v1/search/find", "/api/v1/search/grep") and method == "POST":
        if _use_merged():
            try:
                r = await _forward_json(
                    client, method, MERGED_URL, full_path, body, query
                )
                if r.status_code == 200:
                    return _build_response(r)
            except Exception:
                logger.debug("merged find/grep failed, falling back to fan-out")
        return await _fanout_merge_list(client, method, full_path, body, query)

    if (
        full_path
        in (
            "/api/v1/content/read",
            "/api/v1/content/abstract",
            "/api/v1/content/overview",
        )
        and method == "GET"
    ):
        if _use_merged():
            try:
                r = await _forward_json(
                    client, method, MERGED_URL, full_path, body, query
                )
                if r.status_code == 200:
                    data = r.json()
                    if data.get("status") == "ok" and data.get("result"):
                        return _build_response(r)
            except Exception:
                logger.debug("merged content read failed, falling back to fan-out")
        return await _fanout_first(client, method, full_path, body, query)

    # --- Filesystem (shared S3 — any single worker) ---

    if full_path in ("/api/v1/fs/ls", "/api/v1/fs/tree") and method == "GET":
        target = _any_single()
        r = await _forward_json(client, method, target, full_path, body, query)
        return _build_response(r)

    # --- Passthrough (sessions, system status) ---

    if full_path.startswith("/api/v1/sessions") or full_path == "/api/v1/system/status":
        target = _any_single()
        r = await _forward_json(client, method, target, full_path, body, query)
        return _build_response(r)

    # --- Default: forward to any single worker ---
    logger.debug("default passthrough %s %s", method, full_path)
    target = _any_single()
    r = await _forward_json(client, method, target, full_path, body, query)
    return _build_response(r)
