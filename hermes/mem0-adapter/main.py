"""Mem0 Platform-to-OSS adapter sidecar for Hermes.

Hermes ships a mem0 plugin that uses the managed Mem0 Platform client
(`MemoryClient`). That client emits v1/v3 requests targeted at
`https://api.mem0.ai`. This adapter listens on localhost, accepts those
Platform-style requests, and translates them into the self-hosted OSS server's
REST surface (`/memories`, `/search`, etc.) and auth scheme (`X-API-Key`).

It is intended to run as a sidecar in the `hermes-agent` pod. Hermes is
configured with `memory.mem0.base_url: http://localhost:18080`, and the plugin
is patched to pass that `host` to `MemoryClient`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

MEM0_URL = os.environ.get("MEM0_URL", "http://mem0-server.mem0.svc.cluster.local:8080")
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")

# Platform client sends "Authorization: Token <key>" or "Authorization: Bearer <jwt>".
AUTH_RE = re.compile(r"^(Token|Bearer)\s+(.+)$", re.IGNORECASE)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("mem0-adapter starting; upstream=%s", MEM0_URL)
    yield
    logger.info("mem0-adapter shutting down")


app = FastAPI(title="mem0-platform-to-oss-adapter", lifespan=lifespan)


def _extract_api_key(request: Request) -> str:
    """Return the API key from the Authorization header or env fallback."""
    auth = request.headers.get("authorization", "")
    match = AUTH_RE.match(auth)
    if match:
        return match.group(2)
    return ADMIN_API_KEY


def _forward_headers(request: Request) -> dict[str, str]:
    """Build headers for the upstream OSS request.

    Drop the Platform Authorization and Mem0-User-ID headers; the OSS server
    expects `X-API-Key`.
    """
    dropped = {"authorization", "mem0-user-id", "content-length"}
    headers = {
        k.lower(): v for k, v in request.headers.items() if k.lower() not in dropped
    }
    headers["x-api-key"] = _extract_api_key(request)
    return headers


async def _forward(
    method: str,
    target_path: str,
    request: Request,
    content: bytes | None = None,
    params: dict[str, Any] | None = None,
) -> Response:
    """Forward a request to the OSS server and return the response."""
    url = f"{MEM0_URL.rstrip('/')}{target_path}"
    headers = _forward_headers(request)
    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                content=content,
                params=params,
            )
        except httpx.RequestError as exc:
            logger.exception("upstream request failed: %s %s", method, url)
            return JSONResponse(
                status_code=503,
                content={"detail": f"Upstream mem0-server unreachable: {exc}"},
            )

    logger.info(
        "proxy %s %s -> %s %s => %d",
        request.method,
        request.url.path,
        method,
        target_path,
        response.status_code,
    )
    return Response(
        content=response.content,
        status_code=response.status_code,
        headers={
            k: v
            for k, v in response.headers.items()
            if k.lower() not in {"content-encoding", "transfer-encoding"}
        },
    )


@app.get("/v1/ping/")
async def ping(request: Request):
    """Platform client validates the API key on init via /v1/ping/.

    The OSS server has no equivalent, so return a static success payload that
    satisfies `MemoryClient._validate_api_key()` (it expects `user_email`).
    """
    key = _extract_api_key(request)
    if not key:
        return JSONResponse(status_code=401, content={"detail": "Missing API key"})

    logger.info("proxy GET /v1/ping/ -> static pong")
    return JSONResponse(
        content={
            "message": "pong",
            "user_email": "hermes@homelab.local",
            "org_id": "homelab-org",
            "project_id": "homelab-project",
        }
    )


@app.post("/v3/memories/add/")
async def add_memory(request: Request):
    """Create memories: Platform POST /v3/memories/add/ -> OSS POST /memories."""
    body = await request.body()
    return await _forward("POST", "/memories", request, content=body)


@app.post("/v3/memories/search/")
async def search_memories(request: Request):
    """Search memories: Platform POST /v3/memories/search/ -> OSS POST /search."""
    body = await request.body()
    return await _forward("POST", "/search", request, content=body)


@app.post("/v3/memories/")
async def get_all_memories(request: Request):
    """List memories: Platform POST /v3/memories/ -> OSS GET /memories.

    The Platform client sends filters/top-level entity fields in the JSON
    body. The OSS server expects them as query parameters on `GET /memories`.
    """
    body = await request.body()
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})

    query_params: dict[str, Any] = {}
    page = payload.pop("page", None)
    page_size = payload.pop("page_size", None)
    if page is not None:
        query_params["page"] = page
    if page_size is not None:
        query_params["page_size"] = page_size

    # Body contains either `filters={user_id:...}` or top-level entity keys.
    filters = payload.pop("filters", {})
    for entity_key in ("user_id", "agent_id", "run_id"):
        value = payload.pop(entity_key, None)
        if value is not None:
            filters[entity_key] = value
    query_params.update(filters)

    if payload:
        logger.warning("unmapped fields in get_all body: %s", payload.keys())

    return await _forward("GET", "/memories", request, params=query_params)


@app.get("/v1/memories/{memory_id}/")
async def get_memory(memory_id: str, request: Request):
    return await _forward("GET", f"/memories/{memory_id}", request)


@app.put("/v1/memories/{memory_id}/")
async def update_memory(memory_id: str, request: Request):
    body = await request.body()
    return await _forward("PUT", f"/memories/{memory_id}", request, content=body)


@app.delete("/v1/memories/{memory_id}/")
async def delete_memory(memory_id: str, request: Request):
    return await _forward("DELETE", f"/memories/{memory_id}", request)


@app.delete("/v1/memories/")
async def delete_all_memories(request: Request):
    return await _forward(
        "DELETE", "/memories", request, params=dict(request.query_params)
    )


@app.get("/v1/memories/{memory_id}/history/")
async def memory_history(memory_id: str, request: Request):
    return await _forward("GET", f"/memories/{memory_id}/history", request)


@app.get("/v1/entities/")
async def list_entities(request: Request):
    return await _forward(
        "GET", "/entities", request, params=dict(request.query_params)
    )


@app.delete("/v2/entities/{entity_type}/{entity_name}/")
async def delete_entity(entity_type: str, entity_name: str, request: Request):
    return await _forward("DELETE", f"/entities/{entity_type}/{entity_name}", request)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def catch_all(request: Request, path: str):
    """Pass through any unhandled Platform path verbatim for diagnosis."""
    logger.warning(
        "unhandled route %s /%s; forwarding verbatim to upstream",
        request.method,
        path,
    )
    body = await request.body()
    target = f"/{path}"
    if request.url.query:
        target += f"?{request.url.query}"
    return await _forward(request.method, target, request, content=body)
