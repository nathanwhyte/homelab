"""BUG-1269: a field-only vector update reaches ov-vectordb through HttpCollectionAdapter.

The result-shape, batching and guard cases use fakes. The installed cases import the
real v0.5.1.1 backend module (sitecustomize applies the patch on import), serve the
real vectordb service on a local port, and update through the real
``HttpCollectionAdapter``: a scalar patch lands, the stored URI keeps its encoded
form, and an unknown id reports no ids instead of a success.
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
import ov_http_update_patch as upd


class FakeCollection:
    def __init__(self, answer=None):
        self.answer = answer
        self.calls = []

    def update_data(self, batch):
        self.calls.append([dict(r) for r in batch])
        if self.answer is not None:
            return self.answer(batch)
        return {"ids": [r["id"] for r in batch]}


class FakeAdapter:
    _DATA_BATCH_SIZE = 2

    def __init__(self, coll):
        self.coll = coll

    def _normalize_record_for_write(self, record):
        out = dict(record)
        if "uri" in out:
            out["uri"] = out["uri"].removeprefix("viking:/")
        return out

    def get_collection(self):
        return self.coll


class ResultIds(unittest.TestCase):
    def test_service_success_shape(self):
        self.assertEqual(upd.result_ids({"ids": ["a", 2, None]}), ["a", "2"])

    def test_primary_keys_shape(self):
        self.assertEqual(upd.result_ids({"primary_keys": ["a"]}), ["a"])

    def test_a_list_is_ids(self):
        self.assertEqual(upd.result_ids(["a", "b"]), ["a", "b"])

    def test_errors_name_no_ids(self):
        for value in ([], {}, None, "a", {"ids": "a"}, {"ids": None}):
            with self.subTest(value=value):
                self.assertEqual(upd.result_ids(value), [])


class UpdateData(unittest.TestCase):
    def test_normalizes_each_record_and_returns_ids(self):
        coll = FakeCollection()
        ids = upd.update_data(
            FakeAdapter(coll), [{"id": "a", "uri": "viking://resources/x", "n": 1}]
        )
        self.assertEqual(ids, ["a"])
        self.assertEqual(coll.calls, [[{"id": "a", "uri": "/resources/x", "n": 1}]])

    def test_leaves_the_caller_records_alone(self):
        record = {"id": "a", "uri": "viking://resources/x"}
        upd.update_data(FakeAdapter(FakeCollection()), [record])
        self.assertEqual(record["uri"], "viking://resources/x")

    def test_batches_by_the_adapter_size(self):
        coll = FakeCollection()
        rows = [{"id": str(i)} for i in range(5)]
        self.assertEqual(
            upd.update_data(FakeAdapter(coll), rows), ["0", "1", "2", "3", "4"]
        )
        self.assertEqual([len(c) for c in coll.calls], [2, 2, 1])

    def test_no_batch_size_sends_one_batch(self):
        coll = FakeCollection()
        adapter = FakeAdapter(coll)
        adapter._DATA_BATCH_SIZE = None
        upd.update_data(adapter, [{"id": str(i)} for i in range(3)])
        self.assertEqual([len(c) for c in coll.calls], [3])

    def test_empty_input_sends_nothing(self):
        coll = FakeCollection()
        self.assertEqual(upd.update_data(FakeAdapter(coll), []), [])
        self.assertEqual(upd.update_data(FakeAdapter(coll), None), [])
        self.assertEqual(coll.calls, [])

    def test_an_http_error_reports_no_ids(self):
        coll = FakeCollection(answer=lambda batch: [])
        self.assertEqual(upd.update_data(FakeAdapter(coll), [{"id": "a"}]), [])

    def test_a_partial_batch_reports_no_ids(self):
        coll = FakeCollection(answer=lambda batch: {"ids": [batch[0]["id"]]})
        rows = [{"id": "a"}, {"id": "b"}]
        self.assertEqual(upd.update_data(FakeAdapter(coll), rows), [])

    def test_a_later_failed_batch_reports_no_ids(self):
        answers = iter([{"ids": ["a", "b"]}, []])
        coll = FakeCollection(answer=lambda batch: next(answers))
        rows = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        self.assertEqual(upd.update_data(FakeAdapter(coll), rows), [])

    def test_ids_compare_as_strings(self):
        coll = FakeCollection(answer=lambda batch: {"ids": [7]})
        self.assertEqual(upd.update_data(FakeAdapter(coll), [{"id": 7}]), ["7"])

    def test_a_transport_failure_reports_no_ids(self):
        """A ValueError here would read as INVALID_ARGUMENT in the backend."""
        for exc in (
            json.JSONDecodeError("Expecting value", "<html>", 0),
            ValueError("bad"),
            ConnectionError("refused"),
        ):
            with self.subTest(exc=exc):

                def answer(batch, exc=exc):
                    raise exc

                coll = FakeCollection(answer=answer)
                self.assertEqual(upd.update_data(FakeAdapter(coll), [{"id": "a"}]), [])


def fake_world(adapter_cls):
    """``openviking`` and its http_adapter as stand-ins, for the guard cases."""
    ov = types.ModuleType("openviking")
    ov.__version__ = upd.EXPECTED_VERSION
    http_adapter = types.ModuleType("openviking.storage.vectordb_adapters.http_adapter")
    if adapter_cls is not None:
        http_adapter.HttpCollectionAdapter = adapter_cls
    adapters = types.ModuleType("openviking.storage.vectordb_adapters")
    adapters.http_adapter = http_adapter
    storage = types.ModuleType("openviking.storage")
    storage.vectordb_adapters = adapters
    ov.storage = storage
    return {
        "openviking": ov,
        "openviking.storage": storage,
        "openviking.storage.vectordb_adapters": adapters,
        "openviking.storage.vectordb_adapters.http_adapter": http_adapter,
    }


class Guards(unittest.TestCase):
    def _apply(self, adapter_cls, hashes=None, version=None, env=None):
        mods = fake_world(adapter_cls)
        if version is not None:
            mods["openviking"].__version__ = version
        backend = types.ModuleType("openviking.storage.viking_vector_index_backend")
        got = dict(upd.EXPECTED_SHA256) if hashes is None else hashes
        with (
            patch.dict(sys.modules, mods),
            patch.dict(os.environ, env or {}, clear=True),
            patch.object(upd, "_source_hashes", lambda module: got),
        ):
            return upd.apply(backend), backend

    def test_applies_once(self):
        cls = type("HttpCollectionAdapter", (), {})
        ok, backend = self._apply(cls)
        self.assertTrue(ok)
        self.assertIs(cls.update_data, upd.update_data)
        self.assertTrue(backend._ov_http_update_patch)
        with (
            patch.dict(sys.modules, fake_world(cls)),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertTrue(upd.apply(backend))

    def test_env_switch_off(self):
        cls = type("HttpCollectionAdapter", (), {})
        ok, _ = self._apply(cls, env={"OV_HTTP_UPDATE_PATCH": "0"})
        self.assertFalse(ok)
        self.assertFalse(hasattr(cls, "update_data"))

    def test_stands_down_when_upstream_defines_it(self):
        def own(self, data_list):
            return ["upstream"]

        cls = type("HttpCollectionAdapter", (), {"update_data": own})
        ok, _ = self._apply(cls)
        self.assertFalse(ok)
        self.assertIs(cls.update_data, own)

    def test_refuses_without_the_adapter(self):
        ok, _ = self._apply(None)
        self.assertFalse(ok)

    def test_refuses_another_version(self):
        cls = type("HttpCollectionAdapter", (), {})
        ok, _ = self._apply(cls, version="v0.5.2")
        self.assertFalse(ok)
        self.assertFalse(hasattr(cls, "update_data"))

    def test_refuses_changed_sources(self):
        for key in upd.EXPECTED_SHA256:
            with self.subTest(changed=key):
                cls = type("HttpCollectionAdapter", (), {})
                hashes = dict(upd.EXPECTED_SHA256, **{key: "0" * 64})
                ok, _ = self._apply(cls, hashes=hashes)
                self.assertFalse(ok)
                self.assertFalse(hasattr(cls, "update_data"))


# Skip only when the package is absent; a broken install fails (IMPR-1413).
if importlib.util.find_spec("openviking") is None:  # not in the openviking image
    service = backend_module = None
else:
    # Volatile mode: api_fastapi opens its ProjectGroup at import, from this variable.
    if "openviking.storage.vectordb.service.api_fastapi" not in sys.modules:
        os.environ["VIKINGDB_PERSIST_PATH"] = ""
    from openviking.storage import viking_vector_index_backend as backend_module
    from openviking.storage.vectordb.service import server_fastapi as service


COLLECTION = "bug1269ctx"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipIf(service is None, "needs the openviking v0.5.1.1 image")
class Installed(unittest.TestCase):
    """Applied by sitecustomize to the real adapter; updated over real HTTP."""

    @classmethod
    def setUpClass(cls):
        import uvicorn

        from openviking.storage.collection_schemas import CollectionSchemas
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
            "127.0.0.1", cls.port, "default", COLLECTION, "default"
        )
        cls.adapter.create_collection(
            COLLECTION,
            CollectionSchemas.context_collection(COLLECTION, 4),
            distance="cosine",
            sparse_weight=0.0,
            index_name="default",
        )
        cls.adapter.upsert(
            [
                {
                    "id": "r1",
                    "uri": "viking://resources/compendium/bugs/a.md",
                    "level": 2,
                    "account_id": "noot",
                    "active_count": 0,
                    "vector": [1, 0, 0, 0],
                    "created_at": "2026-10-25T12:00:00Z",
                    "updated_at": "2026-10-25T12:00:00Z",
                },
                {
                    "id": "r2",
                    "uri": "viking://resources/compendium/bugs/b.md",
                    "level": 2,
                    "account_id": "noot",
                    "active_count": 0,
                    "vector": [0, 1, 0, 0],
                    "created_at": "2026-10-25T12:00:00Z",
                    "updated_at": "2026-10-25T12:00:00Z",
                },
            ]
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=10)

    def test_applied_on_import(self):
        from openviking.storage.vectordb_adapters.http_adapter import (
            HttpCollectionAdapter,
        )

        self.assertTrue(getattr(backend_module, "_ov_http_update_patch", False))
        self.assertIs(HttpCollectionAdapter.update_data, upd.update_data)

    def test_guard_hashes_match_the_installed_sources(self):
        self.assertEqual(upd._source_hashes(backend_module), upd.EXPECTED_SHA256)

    def test_field_update_lands_and_keeps_the_uri(self):
        before = self.adapter.get(["r1"])[0]
        ids = self.adapter.update_data(
            [{"id": "r1", "uri": before["uri"], "active_count": 5}]
        )
        self.assertEqual(ids, ["r1"])
        after = self.adapter.get(["r1"])[0]
        self.assertEqual(after["active_count"], 5)
        self.assertEqual(after["uri"], before["uri"])
        self.assertEqual(after["level"], before["level"])

    def test_unknown_id_reports_no_ids(self):
        before = self.adapter.get(["r2"])[0]
        rows = [{"id": "r2", "active_count": 9}, {"id": "missing", "active_count": 1}]
        self.assertEqual(self.adapter.update_data(rows), [])
        self.assertEqual(self.adapter.get(["r2"])[0], before)

    def test_through_the_backend_call(self):
        """The call ``_SingleAccountBackend.update`` makes, on its own facade."""
        import asyncio

        async def run():
            return await backend_module._AsyncVectorAdapter(self.adapter).call(
                "update_data", [{"id": "r2", "active_count": 3}]
            )

        self.assertEqual(asyncio.run(run()), ["r2"])
        self.assertEqual(self.adapter.get(["r2"])[0]["active_count"], 3)

    def _backend_update(self, payload):
        """The real ``_SingleAccountBackend.update`` over this adapter."""
        import asyncio

        config = types.SimpleNamespace(
            dimension=4,
            distance_metric="cosine",
            sparse_weight=0.0,
            name=COLLECTION,
            index_name="default",
        )
        backend = backend_module._SingleAccountBackend(
            config, "noot", shared_adapter=self.adapter
        )
        return asyncio.run(backend.update(payload))

    def test_backend_update_succeeds(self):
        result = self._backend_update({"id": "r1", "active_count": 7})
        self.assertEqual(
            (result.ok, result.ids, result.error_code), (True, ["r1"], None)
        )
        self.assertEqual(self.adapter.get(["r1"])[0]["active_count"], 7)

    def test_backend_reports_update_failed(self):
        """An unknown id and a malformed 200 both read as UPDATE_FAILED."""
        from openviking.storage.vectordb.collection import http_collection

        malformed = types.SimpleNamespace(status_code=200, text="<html>oops</html>")
        cases = {
            "unknown id": ({"id": "missing", "active_count": 1}, None),
            "malformed response": ({"id": "r1", "active_count": 8}, malformed),
        }
        for name, (payload, response) in cases.items():
            with self.subTest(name):
                if response is None:
                    result = self._backend_update(payload)
                else:
                    with patch.object(
                        http_collection.requests, "post", return_value=response
                    ):
                        result = self._backend_update(payload)
                self.assertEqual(
                    (result.ok, result.ids, result.error_code),
                    (False, [], "UPDATE_FAILED"),
                )


if __name__ == "__main__":
    unittest.main()
