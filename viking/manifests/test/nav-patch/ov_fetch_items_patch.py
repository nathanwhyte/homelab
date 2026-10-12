"""Read ``ov-vectordb``'s fetch-by-id response shape on v0.4.20 (BUG-1218).

``HttpCollection.fetch_data`` parses ``data.fetch`` only, but the HTTP vectordb
service answers ``fetch_in_collection`` with ``data.items``. Every fetch by id
therefore comes back empty, and everything built on it is a silent no-op:

  * ``set_tags`` (``update_search_tags``) finds the record by URI, loads nothing
    by id, and reports the URI as skipped;
  * an account-bound ``delete(ids)`` keeps only the ids ``get`` returned, so it
    deletes nothing;
  * ``exists(id)`` is always false, ``increment_active_count`` never counts, and
    the directory vector writer re-embeds L0/L1 records that already exist.

This patch replaces ``fetch_data`` with the same request, parsed from either
key. ``ids_not_exist`` and the non-200 behaviour are unchanged. The records
carry ``vector`` and ``sparse_vector``, so a fetch-modify-upsert keeps the
embedding. Version/source guarded; OV_FETCH_ITEMS_PATCH=0 rolls back.
"""

import hashlib
import inspect
import json
import logging
import os

EXPECTED_VERSION = "v0.4.20"
EXPECTED_SHA256 = "e18a7e93f03fd0cea62f1ae87cc82a89d36fd0ae2885b3cc324ba3204703434e"
logger = logging.getLogger("ov_fetch_items_patch")


def parse_fetch_response(data, result_cls, item_cls):
    """A ``FetchDataInCollectionResult`` from a response's ``data`` object.

    Upstream reads ``fetch``; ``ov-vectordb`` writes ``items``. Either list is
    accepted, ``fetch`` first when both are present.
    """
    result = result_cls()
    if not isinstance(data, dict):
        return result
    rows = data.get("fetch")
    if rows is None:
        rows = data.get("items")
    if rows:
        result.items = [
            item_cls(id=row.get("id"), fields=row.get("fields")) for row in rows
        ]
    if "ids_not_exist" in data:
        result.ids_not_exist = data.get("ids_not_exist", [])
    return result


def make_fetch_data(module):
    def fetch_data(self, primary_keys):
        url = self.url_prefix + "api/vikingdb/data/fetch_in_collection"
        response = module.requests.get(
            url,
            headers=module.headers,
            params={
                "project": self.project_name,
                "collection_name": self.collection_name,
                "ids": json.dumps(primary_keys),
            },
            timeout=module.DEFAULT_TIMEOUT,
        )
        if response.status_code != 200:
            return module.FetchDataInCollectionResult()
        data = json.loads(response.text).get("data", {})
        return parse_fetch_response(
            data, module.FetchDataInCollectionResult, module.DataItem
        )

    fetch_data._ov_fetch_items_patch = True
    return fetch_data


def apply(module):
    if os.environ.get("OV_FETCH_ITEMS_PATCH", "1") == "0":
        logger.warning("ov-fetch-items-patch: disabled by OV_FETCH_ITEMS_PATCH=0")
        return False
    import openviking

    cls = getattr(module, "HttpCollection", None)
    original = getattr(cls, "fetch_data", None)
    if getattr(original, "_ov_fetch_items_patch", False):
        return True
    try:
        digest = hashlib.sha256(inspect.getsource(original).encode()).hexdigest()
    except (OSError, TypeError):
        digest = None
    version = getattr(openviking, "__version__", None)
    if version != EXPECTED_VERSION or digest != EXPECTED_SHA256:
        logger.warning(
            "ov-fetch-items-patch: NOT applied (version=%r sha256=%s)", version, digest
        )
        return False
    cls.fetch_data = make_fetch_data(module)
    logger.warning(
        "ov-fetch-items-patch: applied; fetch_data reads data.items (pid %d)",
        os.getpid(),
    )
    return True
