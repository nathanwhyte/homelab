"""BUG-1268: ov-vectordb answers the server's count call on /api/vikingdb/data/aggregate.

The guard and handler cases use fakes. The installed cases import the real v0.5.1.1
vectordb service (sitecustomize applies the patch on import), serve it on a local
port, and count through the real ``HttpCollectionAdapter``, the path ``ov status``
takes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import importlib.util
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


class FakeRouter:
    prefix = "/api/vikingdb/data"

    def __init__(self, paths=()):
        self.routes = [types.SimpleNamespace(path=p) for p in paths]


class FakeVikingDBException(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message


class FakeCollection:
    def __init__(self, agg_result=None):
        self.calls = []
        self.agg_result = agg_result or {"_total": 3}

    def aggregate_data(
        self, index_name, op="count", field=None, filters=None, cond=None
    ):
        self.calls.append((index_name, op, field, filters, cond))
        return types.SimpleNamespace(agg=self.agg_result, op=op, field=field)


def fake_service(collection=None, missing=False):
    """The parts of api_fastapi the handler uses."""

    def get_collection_or_raise(name, project="default"):
        if missing:
            raise FakeVikingDBException(
                types.SimpleNamespace(value=1000002), "collection not exist"
            )
        return collection

    def convert_dict(p):
        import json

        if not p:
            return {}
        return json.loads(p) if isinstance(p, str) else p

    return types.SimpleNamespace(
        data_router=FakeRouter(["/api/vikingdb/data/upsert"]),
        get_collection_or_raise=get_collection_or_raise,
        success_response=lambda m, d=None, request=None: {
            "code": 0,
            "message": m,
            "data": d,
        },
        error_response=lambda m, c, request=None: {"code": c, "message": m, "data": {}},
        VikingDBException=FakeVikingDBException,
        ErrorCode=types.SimpleNamespace(
            INVALID_PARAM=types.SimpleNamespace(value=1000001)
        ),
        data_utils=types.SimpleNamespace(convert_dict=convert_dict),
    )


class Handler(unittest.TestCase):
    def test_total_count(self):
        coll = FakeCollection()
        out = agg.aggregate(
            fake_service(coll),
            {
                "collection_name": "context",
                "index_name": "default",
                "agg": {"op": "count"},
            },
        )
        self.assertEqual(out["code"], 0)
        self.assertEqual(
            out["data"], {"agg": {"_total": 3}, "op": "count", "field": None}
        )
        self.assertEqual(coll.calls, [("default", "count", None, {}, {})])

    def test_filter_field_and_cond_pass_through(self):
        coll = FakeCollection({"x": 2, "y": 1})
        flt = {"op": "must", "field": "kind", "conds": ["x", "y"]}
        out = agg.aggregate(
            fake_service(coll),
            {
                "collection_name": "context",
                "index_name": "default",
                "agg": {"op": "count", "field": "kind"},
                "filter": flt,
                "cond": '{"gt": 0}',
            },
        )
        self.assertEqual(out["data"]["agg"], {"x": 2, "y": 1})
        self.assertEqual(coll.calls, [("default", "count", "kind", flt, {"gt": 0})])

    def test_missing_agg_defaults_to_count(self):
        coll = FakeCollection()
        out = agg.aggregate(
            fake_service(coll), {"collection_name": "context", "index_name": "default"}
        )
        self.assertEqual(out["code"], 0)
        self.assertEqual(coll.calls[0][1:3], ("count", None))

    def test_rejects_empty_index_and_other_ops(self):
        coll = FakeCollection()
        svc = fake_service(coll)
        for body in (
            {"collection_name": "context", "index_name": ""},
            {
                "collection_name": "context",
                "index_name": "default",
                "agg": {"op": "sum"},
            },
        ):
            self.assertEqual(agg.aggregate(svc, body)["code"], 1000001)
        self.assertEqual(coll.calls, [])

    def test_missing_collection_is_an_error_not_zero(self):
        out = agg.aggregate(
            fake_service(missing=True),
            {"collection_name": "nope", "index_name": "default"},
        )
        self.assertEqual(out["code"], 1000002)
        self.assertEqual(out["data"], {})


class Guards(unittest.TestCase):
    def _sources(self, client="client", collection="collection"):
        return {
            "client": agg._hash_text(client),
            "collection": agg._hash_text(collection),
        }

    def test_refusals_leave_the_router_alone(self):
        svc = fake_service()
        ov = types.SimpleNamespace(__version__="v0.4.20")
        good = self._sources()
        with (
            patch.dict(sys.modules, {"openviking": ov}),
            patch.dict(os.environ, {}, clear=True),
            patch.object(agg, "_source_hashes", lambda: good),
            patch.object(agg, "EXPECTED_CLIENT_SHA256", good["client"]),
            patch.object(agg, "EXPECTED_COLLECTION_SHA256", good["collection"]),
            patch.object(agg, "_register", side_effect=AssertionError("registered")),
        ):
            self.assertFalse(agg.apply(svc))  # version
            ov.__version__ = agg.EXPECTED_VERSION
            with patch.dict(os.environ, {"OV_VECTORDB_AGG_PATCH": "0"}):
                self.assertFalse(agg.apply(svc))  # off switch
            with patch.object(agg, "_source_hashes", lambda: self._sources("drift")):
                self.assertFalse(agg.apply(svc))  # client wire shape changed
            with patch.object(
                agg, "_source_hashes", lambda: self._sources(collection="drift")
            ):
                self.assertFalse(agg.apply(svc))  # collection call changed
            svc.data_router.routes.append(types.SimpleNamespace(path=ROUTE))
            self.assertFalse(agg.apply(svc))  # upstream serves it already
            self.assertFalse(agg.apply(types.SimpleNamespace()))  # no data_router

    def test_applies_once(self):
        svc = fake_service()
        good = self._sources()
        calls = []
        ov = types.SimpleNamespace(__version__=agg.EXPECTED_VERSION)
        with (
            patch.dict(sys.modules, {"openviking": ov}),
            patch.dict(os.environ, {}, clear=True),
            patch.object(agg, "_source_hashes", lambda: good),
            patch.object(agg, "EXPECTED_CLIENT_SHA256", good["client"]),
            patch.object(agg, "EXPECTED_COLLECTION_SHA256", good["collection"]),
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

    def test_applied_on_import(self):
        from openviking.storage.vectordb.service import api_fastapi

        self.assertTrue(getattr(api_fastapi, "_ov_vectordb_agg_patch", False))
        paths = [getattr(r, "path", None) for r in api_fastapi.data_router.routes]
        self.assertEqual(paths.count(ROUTE), 1)
        self.assertIn(ROUTE, service.app.openapi()["paths"])

    def test_guard_hashes_match_the_installed_sources(self):
        self.assertEqual(
            agg._source_hashes(),
            {
                "client": agg.EXPECTED_CLIENT_SHA256,
                "collection": agg.EXPECTED_COLLECTION_SHA256,
            },
        )

    def test_count_through_the_http_adapter(self):
        self.assertEqual(self.adapter.count(), 3)
        self.assertEqual(self.adapter.strict_count(), 3)

    def test_filtered_count(self):
        flt = {"op": "must", "field": "kind", "conds": ["x"]}
        self.assertEqual(self.adapter.count(flt), 2)

    def test_missing_collection_still_fails_the_count(self):
        from openviking.storage.vectordb.collection.http_collection import (
            HttpCollection,
        )

        missing = HttpCollection(
            "127.0.0.1", self.port, {"CollectionName": "bug1268-missing"}
        )
        self.assertEqual(missing.aggregate_data("default").agg, {})


if __name__ == "__main__":
    unittest.main()
