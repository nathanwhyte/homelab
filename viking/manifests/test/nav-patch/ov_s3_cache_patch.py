"""Disable RAGFS's process-local S3 metadata cache on v0.4.20 (BUG-1174).

A cached negative stat survives another worker's mkdir. The lock manager then
gets AlreadyExists from mkdir and the same stale NotFound from its verification
stat, failing a valid session commit. The cache also has a sliding TTL, so polling
can retain a stale result indefinitely. The Rust plugin supports cache_enabled,
but v0.4.20's Python config serializer does not expose it. Set that existing
plugin option at the serializer boundary; locks and their errors stay intact.

This disables both S3 stat and list caches, trading metadata requests for correct
cross-process visibility. Other storage backends and the separate RAGFS runtime
cache are unchanged. Version/source guarded; OV_S3_CACHE_PATCH=0 rolls back.
"""

import functools
import hashlib
import inspect
import logging
import os

EXPECTED_VERSION = "v0.4.20"
EXPECTED_SHA256 = "3dd278b535bd5059e7eb09d58642560721c41fc8ccde403e1150c6282d0151e9"
logger = logging.getLogger("ov_s3_cache_patch")


def without_metadata_cache(original):
    @functools.wraps(original)
    def serialize(config):
        params = dict(original(config))
        params["cache_enabled"] = False
        return params

    serialize._ov_s3_cache_patch = True
    return serialize


def apply(module):
    if os.environ.get("OV_S3_CACHE_PATCH", "1") == "0":
        logger.warning("ov-s3-cache-patch: disabled by OV_S3_CACHE_PATCH=0")
        return False
    import openviking

    original = getattr(module, "_serialize_s3_plugin_params", None)
    if getattr(original, "_ov_s3_cache_patch", False):
        return True
    try:
        digest = hashlib.sha256(inspect.getsource(original).encode()).hexdigest()
    except (OSError, TypeError):
        digest = None
    version = getattr(openviking, "__version__", None)
    if version != EXPECTED_VERSION or digest != EXPECTED_SHA256:
        logger.warning(
            "ov-s3-cache-patch: NOT applied (version=%r sha256=%s)", version, digest
        )
        return False
    module._serialize_s3_plugin_params = without_metadata_cache(original)
    logger.warning(
        "ov-s3-cache-patch: applied; S3 metadata cache off (pid %d)", os.getpid()
    )
    return True
