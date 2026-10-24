"""Serve POST /api/vikingdb/data/aggregate on the ov-vectordb service (BUG-1268).

The server's vector count (``HttpCollection.aggregate_data``, behind ``ov status``,
the exporter's vikingdb table and ``get_stats``) POSTs to that route, which
``server_fastapi`` has never registered. v0.4.20 showed the 404 as "0 vectors";
v0.5.1.1 marks ``[vikingdb]`` and ``[system]`` unhealthy on every status read.

This adds the route to ``api_fastapi.data_router`` at import, before
``server_fastapi`` includes the router, and answers total counts only (the one
request the client sends: ``op: count``, no ``field``) in the
``{"agg": {"_total": N}}`` shape the client parses.

It does not answer through ``Collection.aggregate_data``: ``LocalIndex.aggregate``
returns ``{}`` on any engine failure and ``LocalCollection.aggregate_data`` turns
that into ``_total: 0``, so a fault would read as an empty store. The handler runs
the same engine search itself (``engine_count``) and lets failures raise. The one
``{}`` that is a real zero is a filtered count that matches nothing: the engine
then emits no counter, while an unfiltered count always does (probed on v0.5.1.1).
The contract is the client's: a request ``HttpCollection.aggregate_data`` sends
gets either a correct count or an error code with empty data (a missing collection
or index, an engine failure), so the client's count raises instead of reading 0.
Anything else is answered best-effort with the service's envelope (HTTP 200, an
error ``code``, ASCII-safe ``message``, empty ``data``): the route reads the raw JSON
body instead of a typed model (no FastAPI 422), ``parse_request`` checks its shape,
a filter the field-type converter rejects is INVALID_PARAM, and one boundary
answers anything unexpected with INTERNAL_ERR. Filter semantics
beyond conversion are the engine's, as on every stock search route: the native
filter parser decides what a filter matches, and this patch does not second-guess it.
The handler is ``async`` like the stock routes, so the count runs on the event loop
between writes, never beside one.

Loaded by sitecustomize.py in the ov-vectordb pod (PYTHONPATH=/ov-nav-patch).
Guarded on the version, the sha256 of every method it answers for or mirrors
(``EXPECTED_SHA256``), and the route being absent, so an upstream fix makes it
stand down. OV_VECTORDB_AGG_PATCH=0 rolls back.
"""

import hashlib
import inspect
import json
import logging
import os

EXPECTED_VERSION = "v0.5.1.1"
# Source sha256s on v0.5.1.1. client: the request this route answers. collection,
# local_collection: the delegation _local_index walks (the name-mangled
# Collection.__collection, then LocalCollection.indexes.get). local_index: the
# engine search engine_count mirrors.
EXPECTED_SHA256 = {
    "client": "e45de48be175470080da5a5563301542c903676c8688a3dc8cecc471ce987bcb",
    "collection": "5ff7c8034a4ddd7a2f8b99113b4a6f2fa40aa4f37644cf31213308546568d4aa",
    "local_collection": "fb9fdb383c50dc23e852ee9b70ef2c9b0e3e787bef80094c481b143a9fedaadc",
    "local_index": "e15b1677c58b5f51e20ed9dce99b491ab8adcd70d3102ab1c29c106940c46165",
}
ROUTE = "/aggregate"
TOTAL_KEY = (
    "__total_count__"  # the engine's counter (AggregateKeys.TOTAL_COUNT_INTERNAL)
)
logger = logging.getLogger("ov_vectordb_agg_patch")


class InvalidRequest(Exception):
    """The request is malformed; answered with INVALID_PARAM."""


class AggregateFailed(Exception):
    """The count could not be taken; answered with INTERNAL_ERR, never with 0."""


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
    from openviking.storage.vectordb.collection.local_collection import LocalCollection
    from openviking.storage.vectordb.index.local_index import LocalIndex

    return {
        "client": _source_hash(HttpCollection.aggregate_data),
        "collection": _source_hash(Collection.aggregate_data),
        "local_collection": _source_hash(LocalCollection.aggregate_data),
        "local_index": _source_hash(LocalIndex.aggregate),
    }


def _as_object(value, name):
    """``value`` as a dict: null is ``{}``, a JSON string is parsed, nothing else passes."""
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            raise InvalidRequest(f"{name} is not valid JSON") from None
    if not isinstance(value, dict):
        raise InvalidRequest(f"{name} must be an object")
    return value


def parse_request(body):
    """``(collection_name, project, index_name, filters)`` for a total count, or InvalidRequest."""
    if not isinstance(body, dict):
        raise InvalidRequest("request body must be a JSON object")
    name = body.get("collection_name")
    if not isinstance(name, str) or not name:
        raise InvalidRequest("collection name is empty")
    project = body.get("project")
    if project is None:
        project = "default"
    if not isinstance(project, str) or not project:
        raise InvalidRequest("project must be a non-empty string")
    index_name = body.get("index_name")
    if not isinstance(index_name, str) or not index_name:
        raise InvalidRequest("index name is empty")
    spec = _as_object(body.get("agg"), "agg")
    unknown = set(spec) - {"op", "field"}
    if unknown:
        raise InvalidRequest(f"agg has unknown keys {sorted(unknown)}")
    if spec.get("op", "count") != "count":
        raise InvalidRequest(f"unsupported aggregate op {spec.get('op')!r}; only count")
    if spec.get("field") is not None:
        raise InvalidRequest("grouped counts (agg.field) are not supported")
    if _as_object(body.get("cond"), "cond"):
        raise InvalidRequest("cond is not supported for a total count")
    filters = _as_object(body.get("filter"), "filter")
    return name, project, index_name, filters


def _local_index(collection, index_name):
    """The LocalIndex ``collection`` holds as ``index_name``, as LocalCollection.aggregate_data
    looks it up, or None when it has no such index."""
    inner = getattr(collection, "_Collection__collection", None)
    indexes = getattr(inner, "indexes", None)
    if indexes is None:
        raise AggregateFailed("collection is closed or not a local collection")
    return indexes.get(index_name)


def _engine_module():
    import openviking.storage.vectordb.engine as engine

    return engine


def engine_count(index, filters):
    """LocalIndex.aggregate's engine search, with every failure raised instead of ``{}``."""
    engine = _engine_module()
    proxy = getattr(index, "engine_proxy", None)
    if not proxy or not proxy.index_engine:
        raise AggregateFailed("index engine is not loaded")
    dsl = {"sorter": {"op": "count"}}
    if filters:
        dsl["filter"] = filters
    try:
        if index.field_type_converter:
            dsl = index.field_type_converter.convert_filter_for_index(dsl)
        dsl_json = json.dumps(dsl)
    except Exception as e:  # noqa: BLE001 — conversion only reads the filter, so any error rejects it
        raise InvalidRequest(f"filter rejected: {e}") from e
    req = engine.SearchRequest()
    req.topk = 1  # CounterOp needs no query vector
    req.dsl = dsl_json
    try:
        extra_json = proxy.index_engine.search(req).extra_json
    except Exception as e:  # noqa: BLE001 — any engine error is a failed count
        raise AggregateFailed(f"aggregation failed: {e}") from e
    if not extra_json:
        if filters:
            return 0  # a filter that matches nothing emits no counter
        raise AggregateFailed("engine returned no aggregation result")
    try:
        data = json.loads(extra_json)
    except ValueError as e:
        raise AggregateFailed(f"unparseable aggregation result: {e}") from e
    total = data.get(TOTAL_KEY) if isinstance(data, dict) else None
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AggregateFailed(f"aggregation result has no valid {TOTAL_KEY}")
    return total


def _safe_message(message):
    """ASCII with backslash escapes, so the response serializes whatever the input held."""
    return str(message).encode("ascii", "backslashreplace").decode("ascii")


def aggregate(module, body, req=None):
    """Answer one aggregate request body with ``module``'s (api_fastapi's) helpers."""
    codes = module.ErrorCode
    try:
        name, project, index_name, filters = parse_request(body)
        collection = module.get_collection_or_raise(name, project)
        index = _local_index(collection, index_name)
        if index is None:
            code, message = codes.INDEX_NOT_EXIST.value, "index not exist"
        else:
            total = engine_count(index, filters)
            return module.success_response(
                "aggregate success",
                {"agg": {"_total": total}, "op": "count", "field": None},
                request=req,
            )
    except module.VikingDBException as e:
        code, message = e.code.value, e.message
    except InvalidRequest as e:
        code, message = codes.INVALID_PARAM.value, e
    except AggregateFailed as e:
        code, message = codes.INTERNAL_ERR.value, e
    except Exception as e:  # noqa: BLE001 — the boundary for anything unexpected
        logger.exception("ov-vectordb-agg-patch: unexpected error")
        code, message = codes.INTERNAL_ERR.value, f"aggregate failed: {e!r}"
    return module.error_response(_safe_message(message), code, request=req)


def _register(module):
    from fastapi import Request

    # The raw body, not a typed model: a model's 422 would bypass the envelope.
    async def aggregate_data(req: Request):
        """Count data in an index (BUG-1268 runtime patch; total counts only).

        Body: {collection_name, index_name, project?, agg?: {op: "count"},
        filter?: object}."""
        try:
            body = await req.json()
        except Exception:  # noqa: BLE001 — bad JSON, invalid UTF-8, nesting past the recursion limit
            body = None  # parse_request answers INVALID_PARAM
        return aggregate(module, body, req)

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
    if version != EXPECTED_VERSION or hashes != EXPECTED_SHA256:
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
