"""Serve POST /api/vikingdb/data/aggregate on the ov-vectordb service (BUG-1268).

The server's vector count (``HttpCollection.aggregate_data``, behind ``ov status``,
the exporter's vikingdb table and ``get_stats``) POSTs to that route, which
``server_fastapi`` has never registered. v0.4.20 showed the 404 as "0 vectors";
v0.5.1.1 marks ``[vikingdb]`` and ``[system]`` unhealthy on every status read.

This adds the route to ``api_fastapi.data_router`` at import, before
``server_fastapi`` includes the router. It answers with
``Collection.aggregate_data``, the in-process count the local backend already uses,
in the ``{"agg": {"_total": N}}`` shape the client parses. Errors keep the service's
own error response, so a missing collection or index still fails the count instead
of reading 0. The handler is ``async`` like the stock routes, so the count runs on
the event loop between writes, never beside one.

Loaded by sitecustomize.py in the ov-vectordb pod (PYTHONPATH=/ov-nav-patch).
Guarded on the version, the client's request shape and the collection call it
serves (source sha256s), and on the route being absent, so an upstream fix makes
it stand down. OV_VECTORDB_AGG_PATCH=0 rolls back.
"""

import hashlib
import inspect
import logging
import os

EXPECTED_VERSION = "v0.5.1.1"
# openviking.storage.vectordb.collection.http_collection.HttpCollection.aggregate_data
# on v0.5.1.1: the request this route answers.
EXPECTED_CLIENT_SHA256 = (
    "e45de48be175470080da5a5563301542c903676c8688a3dc8cecc471ce987bcb"
)
# openviking.storage.vectordb.collection.collection.Collection.aggregate_data on
# v0.5.1.1: the call that answers it.
EXPECTED_COLLECTION_SHA256 = (
    "5ff7c8034a4ddd7a2f8b99113b4a6f2fa40aa4f37644cf31213308546568d4aa"
)
ROUTE = "/aggregate"
logger = logging.getLogger("ov_vectordb_agg_patch")


def _hash_text(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _source_hash(fn):
    try:
        return _hash_text(inspect.getsource(fn))
    except (OSError, TypeError):
        return None


def _source_hashes():
    from openviking.storage.vectordb.collection.collection import Collection
    from openviking.storage.vectordb.collection.http_collection import HttpCollection

    return {
        "client": _source_hash(HttpCollection.aggregate_data),
        "collection": _source_hash(Collection.aggregate_data),
    }


def aggregate(module, body, req=None):
    """Answer one aggregate request body with ``module``'s (api_fastapi's) helpers."""
    try:
        collection = module.get_collection_or_raise(
            body.get("collection_name"), body.get("project") or "default"
        )
        index_name = body.get("index_name")
        if not index_name:
            return module.error_response(
                "index name is empty", module.ErrorCode.INVALID_PARAM.value, request=req
            )
        spec = module.data_utils.convert_dict(body.get("agg")) or {}
        op = spec.get("op") or "count"
        if op != "count":
            return module.error_response(
                f"unsupported aggregate op {op!r}; only count is supported",
                module.ErrorCode.INVALID_PARAM.value,
                request=req,
            )
        field = spec.get("field") or None
        result = collection.aggregate_data(
            index_name=index_name,
            op=op,
            field=field,
            filters=module.data_utils.convert_dict(body.get("filter")),
            cond=module.data_utils.convert_dict(body.get("cond")),
        )
        return module.success_response(
            "aggregate success",
            {"agg": result.agg, "op": result.op, "field": result.field},
            request=req,
        )
    except module.VikingDBException as e:
        return module.error_response(e.message, e.code.value, request=req)


def _register(module):
    from typing import Any, Optional

    from fastapi import Request
    from pydantic import BaseModel, Field

    class AggregateRequest(BaseModel):
        collection_name: str = Field(..., description="Collection name")
        index_name: str = Field(..., description="Index name")
        project: Optional[str] = Field("default", description="Project name")
        agg: Optional[Any] = Field(None, description='{"op": "count", "field": ...}')
        filter: Optional[Any] = Field(None, description="Filter conditions")
        cond: Optional[Any] = Field(None, description="Post-aggregation conditions")

    async def aggregate_data(request: AggregateRequest, req: Request):
        """Aggregate (count) data in an index (BUG-1268 runtime patch)"""
        return aggregate(module, request.model_dump(), req)

    module.data_router.add_api_route(
        ROUTE,
        aggregate_data,
        methods=["POST"],
        response_model=module.ApiResponse,
    )


def apply(module):
    if os.environ.get("OV_VECTORDB_AGG_PATCH", "1") == "0":
        logger.warning("ov-vectordb-agg-patch: disabled by OV_VECTORDB_AGG_PATCH=0")
        return False
    if getattr(module, "_ov_vectordb_agg_patch", False):
        return True
    import openviking

    router = getattr(module, "data_router", None)
    if router is None:
        logger.warning("ov-vectordb-agg-patch: NOT applied (no data_router)")
        return False
    full = router.prefix + ROUTE
    if any(getattr(r, "path", None) == full for r in router.routes):
        logger.warning("ov-vectordb-agg-patch: NOT applied (%s already served)", full)
        return False
    version = getattr(openviking, "__version__", None)
    hashes = _source_hashes() if version == EXPECTED_VERSION else {}
    if (
        version != EXPECTED_VERSION
        or hashes.get("client") != EXPECTED_CLIENT_SHA256
        or hashes.get("collection") != EXPECTED_COLLECTION_SHA256
    ):
        logger.warning(
            "ov-vectordb-agg-patch: NOT applied (version=%r sha256=%s)", version, hashes
        )
        return False
    _register(module)
    module._ov_vectordb_agg_patch = True
    logger.warning(
        "ov-vectordb-agg-patch: applied; serving POST %s (pid %d)", full, os.getpid()
    )
    return True
