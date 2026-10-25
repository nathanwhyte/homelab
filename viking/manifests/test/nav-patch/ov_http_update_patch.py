"""Give ``HttpCollectionAdapter`` the ``update_data`` the server calls (BUG-1269).

A field-only vector update (an ``UPDATE_FIELDS`` index action: a scalar patch on an
existing record whose content needs no re-embed, ``content_write.py`` ~1516) runs
``_SingleAccountBackend.update`` -> ``_async_adapter.call("update_data", [payload])``.
Every other adapter defines ``update_data``; on v0.5.1.1 ``HttpCollectionAdapter``
(the ``vectordb.backend: http`` client both stacks use) does not, so each such
update fails with ``'HttpCollectionAdapter' object has no attribute 'update_data'``
and the record keeps its old scalar fields. The transport already exists:
``HttpCollection.update_data`` POSTs to the service's ``/api/vikingdb/data/update``.

This adds the method, shaped like the other adapters' and like ``upsert``: each
record goes through ``_normalize_record_for_write`` (the stored URI form; the
service merges the patch over the stored fields, so an un-encoded ``uri`` would
overwrite the encoded one), batches follow ``_DATA_BATCH_SIZE``, and the result is
the updated ids as strings. The service answers ``{"ids": [...]}`` on success and
HTTP 500 for an unknown id, which ``HttpCollection.update_data`` turns into ``[]``;
any batch that comes back without every one of its ids, or whose request raises
(a connection error, a 200 whose body is not JSON), yields no ids at all, so the
backend reports ``UPDATE_FAILED`` instead of a partial success. The raise is
caught here because the backend reads any ``ValueError`` (``JSONDecodeError``
is one) as ``INVALID_ARGUMENT``, the caller's fault.

Loaded by sitecustomize.py in the openviking pod (PYTHONPATH=/ov-nav-patch), on
``openviking.storage.viking_vector_index_backend``: the caller's module, which
imports the adapters itself, so hooking it (not ``http_adapter``) never re-enters a
half-imported module. Guarded on the version, the sha256 of the transport and caller it is written
against (``EXPECTED_SHA256``), and the method being absent, so an upstream fix
makes it stand down. OV_HTTP_UPDATE_PATCH=0 rolls back.
"""

import hashlib
import inspect
import logging
import os

EXPECTED_VERSION = "v0.5.1.1"
# Source sha256s on v0.5.1.1. client: the request and its ``data`` unwrap.
# collection: the Collection wrapper's delegation to it. normalize: the write
# normalization applied per record. caller: the backend method that calls
# update_data and reads its return as a list of ids.
EXPECTED_SHA256 = {
    "client": "5e00546ec3fbb00925f2f17568270c7cc2f92416ce73ce85ef6c94d729d27b9d",
    "collection": "fc6a7981371782d52281280a8737af1abddc60860a0d461c48e1b83118555935",
    "normalize": "2926675e498ad7456c2902e9c1127e730eff3e35abac1b31edf6815b37c8607b",
    "caller": "08a641361ea709a71ac5d5a859ec570c5060f06e9bb801a8a79dee9bcdf762a1",
}
logger = logging.getLogger("ov_http_update_patch")


def _source_hash(fn):
    try:
        return hashlib.sha256(inspect.getsource(fn).encode()).hexdigest()
    except (OSError, TypeError):
        return None


def _source_hashes(backend_module):
    from openviking.storage.vectordb.collection.collection import Collection
    from openviking.storage.vectordb.collection.http_collection import HttpCollection
    from openviking.storage.vectordb_adapters.base import CollectionAdapter

    caller = getattr(backend_module, "_SingleAccountBackend", None)
    return {
        "client": _source_hash(HttpCollection.update_data),
        "collection": _source_hash(Collection.update_data),
        "normalize": _source_hash(CollectionAdapter._normalize_record_for_write),
        "caller": _source_hash(caller.update) if caller is not None else None,
    }


def result_ids(result):
    """The ids a ``HttpCollection.update_data`` result names, as strings.

    ``{"ids": [...]}`` is the service's success shape; a list is taken as ids
    directly; anything else (``[]`` after an HTTP error, ``{}``) names none.
    """
    if isinstance(result, dict):
        values = result.get("ids")
        if values is None:
            values = result.get("primary_keys")
    else:
        values = result
    if not isinstance(values, list):
        return []
    return [str(item) for item in values if item is not None]


def update_data(self, data_list):
    """Update existing records with only the fields given; return the updated ids."""
    records = [self._normalize_record_for_write(item) for item in data_list or []]
    if not records:
        return []
    coll = self.get_collection()
    batch_size = getattr(self, "_DATA_BATCH_SIZE", None) or len(records)
    ids = []
    for start in range(0, len(records), batch_size):
        batch = records[start : start + batch_size]
        try:
            got = result_ids(coll.update_data(batch))
        except Exception as exc:  # noqa: BLE001 — a ValueError would read as INVALID_ARGUMENT
            logger.warning(
                "ov-http-update-patch: update request failed (%r); reporting none", exc
            )
            return []
        wanted = [str(r.get("id")) for r in batch]
        if sorted(got) != sorted(wanted):
            logger.warning(
                "ov-http-update-patch: update returned %d of %d ids; reporting none",
                len(got),
                len(wanted),
            )
            return []
        ids.extend(got)
    return ids


def apply(module):
    if os.environ.get("OV_HTTP_UPDATE_PATCH", "1") == "0":
        logger.warning("ov-http-update-patch: disabled by OV_HTTP_UPDATE_PATCH=0")
        return False
    if getattr(module, "_ov_http_update_patch", False):
        return True
    import openviking
    from openviking.storage.vectordb_adapters import http_adapter

    adapter = getattr(http_adapter, "HttpCollectionAdapter", None)
    if adapter is None:
        logger.warning("ov-http-update-patch: NOT applied (no HttpCollectionAdapter)")
        return False
    if hasattr(adapter, "update_data"):
        logger.warning(
            "ov-http-update-patch: NOT applied (update_data already defined)"
        )
        return False
    version = getattr(openviking, "__version__", None)
    hashes = _source_hashes(module) if version == EXPECTED_VERSION else {}
    if version != EXPECTED_VERSION or hashes != EXPECTED_SHA256:
        logger.warning(
            "ov-http-update-patch: NOT applied (version=%r sha256=%s)", version, hashes
        )
        return False
    adapter.update_data = update_data
    module._ov_http_update_patch = True
    logger.warning(
        "ov-http-update-patch: applied to HttpCollectionAdapter.update_data (pid %d)",
        os.getpid(),
    )
    return True
