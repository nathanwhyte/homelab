"""Skip the doomed ``update_working_memory`` tool call on v0.4.20 (IMPR-1303).

Session commit with a prior Working Memory runs ``Session._generate_archive_summary_async``
Branch 2: one VLM call with ``tools=[WM_UPDATE_TOOL]``, whose ``sections`` argument is
an object keyed by the seven WM section titles ("Current State", "Task & Goals", ...).
gemma4 answers in its native tool syntax with unquoted keys
(``call:update_working_memory{sections:{Current State:<|"|>...``) and Ollama's gemma4
parser cannot repair a key with a space: every attempt logs ``gemma4 tool call parsing
failed ... invalid character 'C' looking for beginning of object key string`` on timmy
(173 of 173 on 2026-10-13) and comes back as plain content. OpenViking then logs
``WM update: LLM returned no tool_call; falling back to creation prompt`` and runs
``_fallback_generate_wm_creation``, a second full generation (152 fallbacks against 120
session commits in 3 h). The first call is 60-250 s of a decode-bound GPU (~100 t/s for
this model) spent on output that is always discarded.

This wrapper answers that one call locally with an empty ``VLMResponse`` (no tool
calls), so Branch 2 takes its existing fallback at once and the creation prompt is the
only generation. Every other call, tools or not, passes through untouched; the fallback
itself carries no tools. A call that carries ``checkpoint_requests`` raised after the
wasted call before and raises at once now; that path is unchanged in outcome.

One stderr line per skip, ``ov-wm-update-skip {json}``, so the rate is countable in
Loki. Version/source guarded on ``OpenAIVLM.get_completion_async``;
``OV_WM_UPDATE_SKIP=0`` rolls back. Remove this patch once the tool schema or the
Ollama parser makes the update call parse (an Ollama release note naming gemma4
tool-call keys, or an OV release that renames the ``sections`` keys).
"""

import functools
import hashlib
import inspect
import json
import logging
import os
import sys

EXPECTED_VERSION = "v0.4.20"
EXPECTED_SHA256 = "2f8d5bd8a8f871b0af1b52c44e5f41a9bf46bed43be87a1fb337264f36f8f1ac"
SKIPPED_TOOL = "update_working_memory"
logger = logging.getLogger("ov_wm_update_patch")

_skips = 0


def _tool_names(tools):
    """Function names declared in an OpenAI-style ``tools`` list; never raises."""
    names = []
    for tool in tools or ():
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        name = fn.get("name") if isinstance(fn, dict) else tool.get("name")
        if isinstance(name, str):
            names.append(name)
    return names


def is_wm_update_call(tools, tool_choice=None):
    """True only when ``tools`` declares the ``update_working_memory`` tool.

    ``tool_choice`` alone never matches: upstream ignores a choice with no tools and
    returns text, so a forwarding caller that kept the choice must keep its generation.
    The session passes both (``tools=[WM_UPDATE_TOOL]`` plus a dict ``tool_choice``).
    """
    del tool_choice  # kept in the signature for the wrapper's positional mapping
    return SKIPPED_TOOL in _tool_names(tools)


def skipping_wm_update(original):
    @functools.wraps(original)
    async def get_completion_async(self, prompt="", *args, **kwargs):
        # Positional order is (prompt, thinking, tools, tool_choice, messages, max_tokens).
        tools = kwargs.get("tools", args[1] if len(args) > 1 else None)
        tool_choice = kwargs.get("tool_choice", args[2] if len(args) > 2 else None)
        if not is_wm_update_call(tools, tool_choice):
            return await original(self, prompt, *args, **kwargs)
        global _skips
        _skips += 1
        from openviking.models.vlm.base import VLMResponse

        print(
            "ov-wm-update-skip "
            + json.dumps(
                {
                    "tool": SKIPPED_TOOL,
                    "prompt_chars": len(prompt) if isinstance(prompt, str) else None,
                    "skips": _skips,
                    "pid": os.getpid(),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
        return VLMResponse(content="", tool_calls=[], finish_reason="stop", usage={})

    get_completion_async._ov_wm_update_patch = True
    return get_completion_async


def apply(module):
    if os.environ.get("OV_WM_UPDATE_SKIP", "1") == "0":
        logger.warning("ov-wm-update-patch: disabled by OV_WM_UPDATE_SKIP=0")
        return False
    import openviking

    cls = getattr(module, "OpenAIVLM", None)
    original = getattr(cls, "get_completion_async", None)
    if original is None:
        logger.warning(
            "ov-wm-update-patch: NOT applied (OpenAIVLM.get_completion_async missing)"
        )
        return False
    if getattr(original, "_ov_wm_update_patch", False):
        return True
    try:
        digest = hashlib.sha256(inspect.getsource(original).encode()).hexdigest()
    except (OSError, TypeError):
        digest = None
    version = getattr(openviking, "__version__", None)
    if version != EXPECTED_VERSION or digest != EXPECTED_SHA256:
        logger.warning(
            "ov-wm-update-patch: NOT applied (version=%r sha256=%s)", version, digest
        )
        return False
    cls.get_completion_async = skipping_wm_update(original)
    logger.warning(
        "ov-wm-update-patch: applied; %s tool calls answered locally (pid %d)",
        SKIPPED_TOOL,
        os.getpid(),
    )
    return True
