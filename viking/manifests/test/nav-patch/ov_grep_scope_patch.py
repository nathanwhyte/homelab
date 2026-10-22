"""Scope a user-root grep away from session archives, and bound every grep (BUG-1189).

A grep over ``viking://user/<uid>/`` walks ``sessions/*/history/archive_*`` too. A
URI that can hold memory content takes the Python walker: v0.5.1.1 ``FSService.grep``
sets a ``content_transform`` when ``_may_include_memory_content(uri)``
(``service/fs_service.py:71-80,1199-1200``), which forces the ``fs`` engine and,
past ``_grep_fs``'s native fast path, ``_grep_encrypted`` →
``_collect_grep_files`` (``storage/viking_fs/_grep.py:85-89,294-336,775-841``). That
walker lists every directory and reads every file, skipping only ``exclude_uri``,
and a pattern that matches nothing never stops early: ``node_limit`` only counts
matches. Upstream #5271 sends a URI that is itself a session down the native path,
but a user root is not a session URI, so it still walks ``sessions/``. Measured
2026-09-28 on the pilot user (v0.4.20): ``sessions/`` alone ran past the CLI's 60 s
timeout, while the root with ``sessions/`` excluded returned in 10.9 s. The MCP
``grep`` tool still has no ``exclude_uri`` parameter (``server/mcp_endpoint.py:2076``),
so an agent cannot route around it; its call timed out at 15 s and recall lost its
exact-match check.

Two changes at ``FSService.grep``, the boundary the MCP tool and the REST route share
(internal ``VikingFS.grep`` callers are untouched; every other argument, including
v0.5.1.1's ``before_context``/``after_context`` from #5168, passes through as given):

  * a grep whose URI is exactly a user root, with no ``exclude_uri`` of its own, gets
    ``viking://user/<uid>/sessions`` as its exclusion, and the result carries
    ``excluded_by_default``. A grep that names ``sessions/…`` still searches it.
  * every grep is bounded by ``OV_GREP_TIMEOUT_S`` (default 12 s, inside the MCP
    client's 15 s; ``0`` turns the bound off). A timeout returns one marked result line
    and ``timed_out: true`` rather than an error: the line tells the agent the result
    is incomplete and how to narrow the URI, where a bare ``TimeoutError`` reaches the
    MCP tool as a failure with an empty message and the REST route as a 500.

Version/source guarded; ``OV_GREP_SCOPE_PATCH=0`` rolls back.
"""

import asyncio
import functools
import hashlib
import inspect
import logging
import os
import re

EXPECTED_VERSION = "v0.5.1.1"
EXPECTED_SHA256 = "be962811fbded40c92ff7227b490ed4cc109b5cab4f07a588b9b187158546d23"
DEFAULT_TIMEOUT_S = 12.0
USER_ROOT = re.compile(r"^viking://user/([^/]+)/?$")
logger = logging.getLogger("ov_grep_scope_patch")


def default_exclude(uri, exclude_uri):
    """The exclusion a grep runs with: the caller's, else a user root's ``sessions/``."""
    if exclude_uri:
        return exclude_uri
    m = USER_ROOT.match(uri or "")
    return f"viking://user/{m.group(1)}/sessions" if m else None


def timeout_s():
    """The grep time bound in seconds, or None when ``OV_GREP_TIMEOUT_S`` <= 0."""
    try:
        value = float(os.environ.get("OV_GREP_TIMEOUT_S", DEFAULT_TIMEOUT_S))
    except ValueError:
        value = DEFAULT_TIMEOUT_S
    return value if value > 0 else None


def timed_out_result(uri, limit):
    note = (
        f"[grep timed out after {limit:g} s: results incomplete, not a confirmed absence; "
        "narrow the uri, e.g. <user>/memories/ or a dated memories/events/<yyyy>/<mm> subtree]"
    )
    return {
        "matches": [{"uri": uri, "line": 0, "content": note}],
        "count": 1,
        "match_count": 0,
        "files_scanned": 0,
        "timed_out": True,
    }


def scoped_grep(original):
    signature = inspect.signature(original)

    @functools.wraps(original)
    async def grep(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        uri = bound.arguments.get("uri")
        given = bound.arguments.get("exclude_uri")
        exclude = default_exclude(uri, given)
        if exclude != given:
            bound.arguments["exclude_uri"] = exclude
        limit = timeout_s()
        try:
            result = await asyncio.wait_for(
                original(*bound.args, **bound.kwargs), limit
            )
        except asyncio.TimeoutError:
            logger.warning(
                "ov-grep-scope: grep over %s timed out after %gs (pattern %r)",
                uri,
                limit,
                bound.arguments.get("pattern"),
            )
            return timed_out_result(uri, limit)
        if exclude != given:
            result = dict(result)
            result["excluded_by_default"] = exclude
        return result

    grep._ov_grep_scope_patch = True
    return grep


def apply(module):
    if os.environ.get("OV_GREP_SCOPE_PATCH", "1") == "0":
        logger.warning("ov-grep-scope: disabled by OV_GREP_SCOPE_PATCH=0")
        return False
    import openviking

    service = getattr(module, "FSService", None)
    original = getattr(service, "grep", None)
    if getattr(original, "_ov_grep_scope_patch", False):
        return True
    try:
        digest = hashlib.sha256(inspect.getsource(original).encode()).hexdigest()
    except (OSError, TypeError):
        digest = None
    version = getattr(openviking, "__version__", None)
    if version != EXPECTED_VERSION or digest != EXPECTED_SHA256:
        logger.warning(
            "ov-grep-scope: NOT applied (version=%r sha256=%s)", version, digest
        )
        return False
    service.grep = scoped_grep(original)
    logger.warning(
        "ov-grep-scope: applied; user-root grep skips sessions/, bound %ss (pid %d)",
        timeout_s(),
        os.getpid(),
    )
    return True
