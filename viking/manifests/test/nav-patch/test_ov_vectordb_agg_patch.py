"""BUG-1268: ov-vectordb answers the server's count call on /api/vikingdb/data/aggregate.

The parse, engine, handler and guard cases use fakes. The installed cases import the
real v0.5.1.1 vectordb service (sitecustomize applies the patch on import), serve it
on a local port, and count through the real ``HttpCollectionAdapter``, the path
``ov status`` takes, including an engine failure that must not read as 0.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import importlib.util
import json
import os
import socket
import threading
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ov_vectordb_agg_patch as agg

ROUTE = "/api/vikingdb/data/aggregate"
INVALID, MISSING, INTERNAL = 1000001, 1000003, 1000004


class FakeEngine:
    class SearchRequest:
        topk = None
        dsl = None


class FakeIndexEngine:
    def __init__(self, extra_json="", error=None):
        self.extra_json = extra_json
        self.error = error
        self.requests = []

    def search(self, req):
        self.requests.append(json.loads(req.dsl))
        if self.error:
            raise self.error
        return types.SimpleNamespace(extra_json=self.extra_json)


def fake_index(extra_json="", error=None, converter=None, engine=True):
    index_engine = FakeIndexEngine(extra_json, error) if engine else None
    return types.SimpleNamespace(
        engine_proxy=types.SimpleNamespace(index_engine=index_engine),
        field_type_converter=converter,
    )


def fake_collection(index=None, index_name="default"):
    """Collection -> LocalCollection.indexes -> LocalIndex, by their real attribute names."""
    indexes = {} if index is None else {index_name: index}
    coll = types.SimpleNamespace()
    setattr(coll, "_Collection__collection", types.SimpleNamespace(indexes=indexes))
    return coll


class FakeVikingDBException(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message


def fake_service(collection=None):
    """The parts of api_fastapi the handler uses."""

    def get_collection_or_raise(name, project="default"):
        if collection is None:
            raise FakeVikingDBException(
                types.SimpleNamespace(value=MISSING), "collection not exist"
            )
        return collection

    code = types.SimpleNamespace
    return types.SimpleNamespace(
        data_router=types.SimpleNamespace(
            prefix="/api/vikingdb/data",
            routes=[types.SimpleNamespace(path="/api/vikingdb/data/upsert")],
        ),
        get_collection_or_raise=get_collection_or_raise,
        success_response=lambda m, d=None, request=None: {
            "code": 0,
            "message": m,
            "data": d,
        },
        error_response=lambda m, c, request=None: {"code": c, "message": m, "data": {}},
        VikingDBException=FakeVikingDBException,
        ErrorCode=types.SimpleNamespace(
            INVALID_PARAM=code(value=INVALID),
            INDEX_NOT_EXIST=code(value=MISSING),
            INTERNAL_ERR=code(value=INTERNAL),
        ),
    )


def client_body(**overrides):
    """What HttpCollection.aggregate_data sends for CollectionAdapter.count()."""
    body = {
        "project": "default",
        "collection_name": "context",
        "index_name": "default",
        "agg": {"op": "count", "field": None},
        "filter": None,
    }
    body.update(overrides)
    return body


class Parse(unittest.TestCase):
    def test_client_request(self):
        flt = {"op": "must", "field": "kind", "conds": ["x"]}
        self.assertEqual(
            agg.parse_request(client_body(filter=flt)),
            ("context", "default", "default", flt),
        )
        self.assertEqual(agg.parse_request(client_body(filter=None))[3], {})
        self.assertEqual(agg.parse_request(client_body(agg=None))[3], {})
        self.assertEqual(
            agg.parse_request(client_body(filter=json.dumps(flt), project=None))[1:],
            ("default", "default", flt),
        )

    def test_rejects_malformed_requests(self):
        bad = [
            {"collection_name": ""},
            {"collection_name": None},
            {"collection_name": 5},
            {"project": ""},
            {"project": 5},
            {"index_name": ""},
            {"index_name": None},
            {"index_name": 5},
            {"agg": 123},
            {"agg": ["count"]},
            {"agg": "{not json"},
            {"agg": ""},
            {"filter": ""},
            {"cond": ""},
            {"agg": {"op": False}},
            {"agg": {"op": "sum"}},
            {"agg": {"op": None}},
            {"agg": {"op": "count", "field": "kind"}},
            {"agg": {"op": "count", "extra": 1}},
            {"filter": [1]},
            {"filter": 7},
            {"filter": "{bad"},
            {"cond": {"gt": 1}},
            {"cond": "nope"},
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides):
                with self.assertRaises(agg.InvalidRequest):
                    agg.parse_request(client_body(**overrides))

    def test_rejects_a_body_that_is_not_an_object(self):
        for body in (None, [], "x", 3):
            with self.subTest(body=body):
                with self.assertRaises(agg.InvalidRequest):
                    agg.parse_request(body)


class EngineCount(unittest.TestCase):
    def setUp(self):
        p = patch.object(agg, "_engine_module", lambda: FakeEngine)
        p.start()
        self.addCleanup(p.stop)

    def test_total(self):
        index = fake_index('{"__total_count__": 3}')
        self.assertEqual(agg.engine_count(index, {}), 3)
        self.assertEqual(
            index.engine_proxy.index_engine.requests, [{"sorter": {"op": "count"}}]
        )

    def test_filter_and_converter(self):
        flt = {"op": "must", "field": "kind", "conds": ["x"]}
        seen = []

        def convert(dsl):
            seen.append(dsl)
            return {**dsl, "converted": True}

        index = fake_index(
            '{"__total_count__": 2}',
            converter=types.SimpleNamespace(convert_filter_for_index=convert),
        )
        self.assertEqual(agg.engine_count(index, flt), 2)
        self.assertEqual(seen, [{"sorter": {"op": "count"}, "filter": flt}])
        self.assertTrue(index.engine_proxy.index_engine.requests[0]["converted"])

    def test_filtered_count_with_no_match_is_zero(self):
        self.assertEqual(agg.engine_count(fake_index(""), {"op": "must"}), 0)

    def test_converter_rejection_is_an_invalid_request(self):
        for error in (ValueError("geo_range field must be geo_point"), TypeError("x")):
            with self.subTest(error=error):

                def convert(dsl, error=error):
                    raise error

                index = fake_index(
                    '{"__total_count__": 2}',
                    converter=types.SimpleNamespace(convert_filter_for_index=convert),
                )
                with self.assertRaises(agg.InvalidRequest):
                    agg.engine_count(index, {"op": "geo_range", "field": "kind"})
                self.assertEqual(index.engine_proxy.index_engine.requests, [])

    def test_failures_raise_instead_of_reading_zero(self):
        cases = {
            "unfiltered empty result": (fake_index(""), {}),
            "engine error": (fake_index(error=RuntimeError("boom")), {"op": "must"}),
            "no engine": (fake_index(engine=False), {}),
            "no proxy": (types.SimpleNamespace(engine_proxy=None), {}),
            "bad json": (fake_index("{oops"), {}),
            "no counter": (fake_index('{"other": 1}'), {}),
            "bool counter": (fake_index('{"__total_count__": true}'), {}),
            "negative counter": (fake_index('{"__total_count__": -1}'), {}),
            "float counter": (fake_index('{"__total_count__": 1.5}'), {}),
            "list result": (fake_index("[]"), {}),
        }
        for name, (index, filters) in cases.items():
            with self.subTest(name):
                with self.assertRaises(agg.AggregateFailed):
                    agg.engine_count(index, filters)


class Handler(unittest.TestCase):
    def setUp(self):
        p = patch.object(agg, "_engine_module", lambda: FakeEngine)
        p.start()
        self.addCleanup(p.stop)

    def test_total_count_envelope(self):
        svc = fake_service(fake_collection(fake_index('{"__total_count__": 3}')))
        self.assertEqual(
            agg.aggregate(svc, client_body()),
            {
                "code": 0,
                "message": "aggregate success",
                "data": {"agg": {"_total": 3}, "op": "count", "field": None},
            },
        )

    def test_errors_get_error_envelopes(self):
        good = fake_index('{"__total_count__": 3}')
        cases = [
            ("missing collection", fake_service(None), client_body(), MISSING),
            (
                "missing index",
                fake_service(fake_collection(good, "other")),
                client_body(),
                MISSING,
            ),
            (
                "engine error",
                fake_service(fake_collection(fake_index(error=RuntimeError("boom")))),
                client_body(),
                INTERNAL,
            ),
            (
                "closed collection",
                fake_service(types.SimpleNamespace()),
                client_body(),
                INTERNAL,
            ),
            (
                "malformed",
                fake_service(fake_collection(good)),
                client_body(agg=["count"]),
                INVALID,
            ),
        ]
        for name, svc, body, code in cases:
            with self.subTest(name):
                out = agg.aggregate(svc, body)
                self.assertEqual(out["code"], code)
                self.assertEqual(out["data"], {})

    def test_unexpected_errors_still_get_an_envelope(self):
        svc = fake_service(fake_collection(fake_index('{"__total_count__": 3}')))

        def explode(name, project="default"):
            raise KeyError("unexpected")

        svc.get_collection_or_raise = explode
        out = agg.aggregate(svc, client_body())
        self.assertEqual((out["code"], out["data"]), (INTERNAL, {}))
        out = agg.aggregate(svc, None)
        self.assertEqual((out["code"], out["data"]), (INVALID, {}))

    def test_index_without_an_engine_is_an_error(self):
        coll = fake_collection(fake_index(engine=False))
        self.assertEqual(
            agg.aggregate(fake_service(coll), client_body())["code"], INTERNAL
        )


class Guards(unittest.TestCase):
    def test_refusals_leave_the_router_alone(self):
        svc = fake_service()
        ov = types.SimpleNamespace(__version__="v0.4.20")
        good = dict(agg.EXPECTED_SHA256)
        with (
            patch.dict(sys.modules, {"openviking": ov}),
            patch.dict(os.environ, {}, clear=True),
            patch.object(agg, "_source_hashes", lambda: dict(good)),
            patch.object(agg, "_register", side_effect=AssertionError("registered")),
        ):
            self.assertFalse(agg.apply(svc))  # version
            ov.__version__ = agg.EXPECTED_VERSION
            with patch.dict(os.environ, {"OV_VECTORDB_AGG_PATCH": "0"}):
                self.assertFalse(agg.apply(svc))  # off switch
            for key in good:
                with self.subTest(drift=key):
                    drifted = {**good, key: agg._hash_text("drift")}
                    with patch.object(agg, "_source_hashes", lambda d=drifted: d):
                        self.assertFalse(agg.apply(svc))
            with patch.object(agg, "_source_hashes", lambda: {**good, "client": None}):
                self.assertFalse(agg.apply(svc))  # source unreadable
            svc.data_router.routes.append(types.SimpleNamespace(path=ROUTE))
            self.assertFalse(agg.apply(svc))  # upstream serves it already
            self.assertFalse(agg.apply(types.SimpleNamespace()))  # no data_router

    def test_applies_once(self):
        svc = fake_service()
        calls = []
        ov = types.SimpleNamespace(__version__=agg.EXPECTED_VERSION)
        with (
            patch.dict(sys.modules, {"openviking": ov}),
            patch.dict(os.environ, {}, clear=True),
            patch.object(agg, "_source_hashes", lambda: dict(agg.EXPECTED_SHA256)),
            patch.object(agg, "_register", lambda module: calls.append(module)),
        ):
            self.assertTrue(agg.apply(svc))
            self.assertTrue(agg.apply(svc))
        self.assertEqual(calls, [svc])


# Skip only when the package is absent; a broken install fails (IMPR-1413).
if importlib.util.find_spec("openviking") is None:  # not in the openviking image
    service = None
else:
    # Volatile mode: api_fastapi opens its ProjectGroup at import, from this variable.
    if "openviking.storage.vectordb.service.api_fastapi" not in sys.modules:
        os.environ["VIKINGDB_PERSIST_PATH"] = ""
    from openviking.storage.vectordb.service import server_fastapi as service


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RaisingProxy:
    """Stands in for a real index's engine proxy whose native search fails."""

    class _Engine:
        def search(self, req):
            raise RuntimeError("native search failed")

    index_engine = _Engine()


@unittest.skipIf(service is None, "needs the openviking v0.5.1.1 image")
class Installed(unittest.TestCase):
    """Applied by sitecustomize to the real service; counted over real HTTP."""

    @classmethod
    def setUpClass(cls):
        import uvicorn

        from openviking.storage.vectordb_adapters.http_adapter import (
            HttpCollectionAdapter,
        )

        cls.port = _free_port()
        cls.server = uvicorn.Server(
            uvicorn.Config(
                service.app, host="127.0.0.1", port=cls.port, log_level="error"
            )
        )
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 30
        while not cls.server.started:
            if time.monotonic() > deadline or not cls.thread.is_alive():
                raise RuntimeError("vectordb service did not start")
            time.sleep(0.05)
        cls.adapter = HttpCollectionAdapter(
            "127.0.0.1", cls.port, "default", "bug1268", "default"
        )
        cls.adapter.create_collection(
            "bug1268",
            {
                "Fields": [
                    {"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True},
                    {"FieldName": "vector", "FieldType": "vector", "Dim": 4},
                    {"FieldName": "kind", "FieldType": "string"},
                ],
                "ScalarIndex": ["kind"],
            },
            distance="cosine",
            sparse_weight=0.0,
            index_name="default",
        )
        cls.adapter.upsert(
            [
                {"id": "a", "vector": [1, 0, 0, 0], "kind": "x"},
                {"id": "b", "vector": [0, 1, 0, 0], "kind": "x"},
                {"id": "c", "vector": [0, 0, 1, 0], "kind": "y"},
            ]
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=10)

    def _post(self, raw=None, **overrides):
        import requests

        url = f"http://127.0.0.1:{self.port}{ROUTE}"
        if raw is not None:
            resp = requests.post(
                url, data=raw, headers={"Content-Type": "application/json"}, timeout=10
            )
        else:
            body = client_body(collection_name="bug1268")
            body.update(overrides)
            resp = requests.post(url, json=body, timeout=10)
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def _real_index(self):
        from openviking.storage.vectordb.service import api_fastapi

        coll = api_fastapi.get_collection_or_raise("bug1268")
        return agg._local_index(coll, "default")

    def test_applied_on_import(self):
        from openviking.storage.vectordb.service import api_fastapi

        self.assertTrue(getattr(api_fastapi, "_ov_vectordb_agg_patch", False))
        paths = [getattr(r, "path", None) for r in api_fastapi.data_router.routes]
        self.assertEqual(paths.count(ROUTE), 1)
        self.assertIn(ROUTE, service.app.openapi()["paths"])

    def test_guard_hashes_match_the_installed_sources(self):
        self.assertEqual(agg._source_hashes(), agg.EXPECTED_SHA256)

    def test_walks_to_the_real_local_index(self):
        from openviking.storage.vectordb.index.local_index import LocalIndex

        self.assertIsInstance(self._real_index(), LocalIndex)

    def test_count_through_the_http_adapter(self):
        self.assertEqual(self.adapter.count(), 3)
        self.assertEqual(self.adapter.strict_count(), 3)

    def test_filtered_counts(self):
        self.assertEqual(
            self.adapter.count({"op": "must", "field": "kind", "conds": ["x"]}), 2
        )
        self.assertEqual(
            self.adapter.count({"op": "must", "field": "kind", "conds": ["zzz"]}), 0
        )

    def test_engine_failure_fails_the_count(self):
        with patch.object(self._real_index(), "engine_proxy", RaisingProxy()):
            self.assertEqual(self._post()["code"], INTERNAL)
            with self.assertRaises(RuntimeError):
                self.adapter.count()
        self.assertEqual(self.adapter.count(), 3)

    def test_error_envelopes_over_http(self):
        cases = [
            ({"collection_name": "bug1268-missing"}, MISSING),
            ({"index_name": "missing"}, MISSING),
            ({"agg": ["count"]}, INVALID),
            ({"agg": {"op": "count", "field": "kind"}}, INVALID),
            ({"filter": "{bad"}, INVALID),
            ({"filter": ""}, INVALID),
            ({"agg": ""}, INVALID),
            ({"index_name": None}, INVALID),
            ({"index_name": 5}, INVALID),
            ({"collection_name": None}, INVALID),
            # The field-type converter rejects it before the engine runs.
            (
                {"filter": {"op": "geo_range", "field": "kind", "center": "0,0"}},
                INVALID,
            ),
        ]
        for overrides, code in cases:
            with self.subTest(overrides=overrides):
                out = self._post(**overrides)
                self.assertEqual(out["code"], code)
                self.assertEqual(out["data"], {})
        for raw in (b"{not json", b"[1, 2]", b"", b"\xff"):
            with self.subTest(raw=raw):
                out = self._post(raw=raw)
                self.assertEqual((out["code"], out["data"]), (INVALID, {}))


if __name__ == "__main__":
    unittest.main()
