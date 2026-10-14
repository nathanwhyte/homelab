"""Make the ``update_working_memory`` tool call parse on v0.4.20 (IMPR-1303).

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

Two modes, chosen by ``OV_WM_UPDATE_MODE``:

``schema`` (default)
    The tool schema the model sees keys ``sections`` by snake_case ids
    (``current_state``, ``task_and_goals``, ...), each carrying the title it stands
    for, so gemma4's unquoted keys parse. The returned tool call is mapped back to the
    title keys before OpenViking's parser sees it, and a ``sections`` value that came
    back as a JSON string (seen 2/24 on Claude Haiku 5.5, IMPR-1311) is decoded. The
    incremental update path then runs as upstream designed it; a call that still
    returns no tool call takes OpenViking's own creation-prompt fallback. One stderr
    line per call, ``ov-wm-update-schema {json}``, names the outcome and how many
    section keys mapped, so the parse rate is countable in Loki.
``skip``
    The 2026-10-13 behaviour: answer the call locally with an empty ``VLMResponse``
    (no tool calls) so Branch 2 takes its fallback at once and the creation prompt is
    the only generation. One stderr line per skip, ``ov-wm-update-skip {json}``.
``off``
    Not applied; the upstream call runs unchanged.

With ``OV_WM_UPDATE_MODE`` unset the older ``OV_WM_UPDATE_SKIP`` switch still reads:
``1`` is ``skip``, ``0`` is ``off``, unset is ``schema``. Every other call, tools or
not, passes through untouched in both modes; the fallback itself carries no tools.
Version/source guarded on ``OpenAIVLM.get_completion_async``. Remove this patch once an
OV release renames the ``sections`` keys or the Ollama parser accepts keys with spaces.
"""

import copy
import functools
import hashlib
import inspect
import json
import logging
import os
import re
import sys
import time

EXPECTED_VERSION = "v0.4.20"
EXPECTED_SHA256 = "2f8d5bd8a8f871b0af1b52c44e5f41a9bf46bed43be87a1fb337264f36f8f1ac"
SKIPPED_TOOL = "update_working_memory"
MODES = ("schema", "skip", "off")
logger = logging.getLogger("ov_wm_update_patch")

_skips = 0
_calls = 0


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


def read_mode(environ=None):
    """The configured mode, or ``None`` for an unrecognised ``OV_WM_UPDATE_MODE``."""
    env = os.environ if environ is None else environ
    mode = env.get("OV_WM_UPDATE_MODE")
    if mode is not None:
        return mode if mode in MODES else None
    legacy = env.get("OV_WM_UPDATE_SKIP")
    if legacy == "0":
        return "off"
    if legacy == "1":
        return "skip"
    return "schema"


# ---------------------------------------------------------------- schema mode


def snake_key(title):
    """``"Key Facts & Decisions"`` -> ``"key_facts_and_decisions"``."""
    text = title.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def _sections_schema(tool):
    fn = tool.get("function") if isinstance(tool, dict) else None
    if not isinstance(fn, dict) or fn.get("name") != SKIPPED_TOOL:
        return None
    params = fn.get("parameters")
    if not isinstance(params, dict):
        return None
    sections = (params.get("properties") or {}).get("sections")
    if not isinstance(sections, dict) or not isinstance(
        sections.get("properties"), dict
    ):
        return None
    return sections


def rewrite_tools(tools):
    """A deep copy of ``tools`` whose ``sections`` keys are snake_case ids.

    Returns ``(new_tools, key_map)`` with ``key_map`` mapping each id back to its
    title. Returns ``(tools, None)`` untouched when the tool's shape is not the one
    this patch knows (no ``sections.properties`` object, a non-string key, or two
    titles that collapse to one id), so the caller forwards the call as it was.
    """
    new_tools = copy.deepcopy(tools)
    key_map = None
    for tool in new_tools:
        sections = _sections_schema(tool)
        if sections is None:
            continue
        props = sections["properties"]
        if not all(isinstance(title, str) and title for title in props):
            return tools, None
        ids = {snake_key(title): title for title in props}
        if len(ids) != len(props) or "" in ids:
            return tools, None
        sections["properties"] = {
            key: dict(schema, description=f"Decision for the '## {title}' section.")
            if isinstance(schema, dict)
            else schema
            for key, title in ids.items()
            for schema in (props[title],)
        }
        required = sections.get("required")
        if isinstance(required, list):
            sections["required"] = [
                snake_key(title) if isinstance(title, str) else title
                for title in required
            ]
        sections["description"] = "Keyed by section id: " + ", ".join(
            f"{key} = '## {title}'" for key, title in ids.items()
        )
        key_map = ids
    return (new_tools, key_map) if key_map else (tools, None)


def _substitute_quoted_keys(text, key_map):
    """Rename ``"<id>"`` to ``"<Title>"`` inside raw JSON text (regex salvage input)."""
    if not key_map:
        return text, 0
    pattern = re.compile(
        r'"(' + "|".join(re.escape(key) for key in key_map) + r')"(\s*:)'
    )
    return pattern.subn(lambda m: f'"{key_map[m.group(1)]}"{m.group(2)}', text)


def restore_arguments(args, key_map):
    """Map a returned tool call's section ids back to titles.

    Returns ``(new_args, info)``; ``info`` carries ``sections`` (how the value
    arrived), ``mapped`` (keys renamed) and ``unmapped`` (keys left as they came, the
    seven titles themselves included). Anything of an unexpected shape is returned as
    it was, so OpenViking's own tolerant parser still gets its chance.
    """
    info = {"sections": None, "mapped": 0, "unmapped": []}

    def rename(ops):
        out = {}
        for key, value in ops.items():
            title = key_map.get(key) if isinstance(key, str) else None
            if title is None:
                info["unmapped"].append(key)
                out[key] = value
            else:
                info["mapped"] += 1
                out[title] = value
        return out

    if isinstance(args, str):
        new_text, count = _substitute_quoted_keys(args, key_map)
        info.update(sections="text", mapped=count)
        return new_text, info
    if not isinstance(args, dict):
        info["sections"] = type(args).__name__
        return args, info
    if list(args.keys()) == ["raw"] and isinstance(args["raw"], str):
        new_text, count = _substitute_quoted_keys(args["raw"], key_map)
        info.update(sections="raw", mapped=count)
        return {"raw": new_text}, info
    new_args = dict(args)
    if "sections" in new_args:
        sections = new_args["sections"]
        info["sections"] = "dict"
        if isinstance(sections, str):
            try:
                decoded = json.loads(sections)
            except ValueError:
                info["sections"] = "str-unparsed"
                return new_args, info
            if not isinstance(decoded, dict):
                info["sections"] = "str-" + type(decoded).__name__
                return new_args, info
            info["sections"] = "str->dict"
            sections = decoded
        if isinstance(sections, dict):
            new_args["sections"] = rename(sections)
        else:
            info["sections"] = type(sections).__name__
        return new_args, info
    # Tolerant upstream shape: the section ops at the top level, no wrapper.
    info["sections"] = "top-level"
    return rename(new_args), info


def _forward_with_tools(args, kwargs, new_tools):
    if "tools" in kwargs:
        kwargs = dict(kwargs, tools=new_tools)
    else:
        args = list(args)
        args[1] = new_tools
        args = tuple(args)
    return args, kwargs


def schema_wm_update(original):
    @functools.wraps(original)
    async def get_completion_async(self, prompt="", *args, **kwargs):
        # Positional order is (prompt, thinking, tools, tool_choice, messages, max_tokens).
        tools = kwargs.get("tools", args[1] if len(args) > 1 else None)
        tool_choice = kwargs.get("tool_choice", args[2] if len(args) > 2 else None)
        if not is_wm_update_call(tools, tool_choice):
            return await original(self, prompt, *args, **kwargs)
        new_tools, key_map = rewrite_tools(tools)
        if key_map is None:
            logger.warning(
                "ov-wm-update-patch: %s schema not in the expected shape; forwarded as is",
                SKIPPED_TOOL,
            )
            return await original(self, prompt, *args, **kwargs)
        args, kwargs = _forward_with_tools(args, kwargs, new_tools)
        global _calls
        _calls += 1
        started = time.perf_counter()
        resp = await original(self, prompt, *args, **kwargs)
        elapsed = round(time.perf_counter() - started, 1)
        record = {
            "mode": "schema",
            "tool": SKIPPED_TOOL,
            "outcome": "text",
            "sections": None,
            "mapped": 0,
            "unmapped": [],
            "finish_reason": getattr(resp, "finish_reason", None),
            "completion_tokens": (getattr(resp, "usage", None) or {}).get(
                "completion_tokens"
            ),
            "elapsed_s": elapsed,
            "prompt_chars": len(prompt) if isinstance(prompt, str) else None,
            "calls": _calls,
            "pid": os.getpid(),
        }
        tool_calls = getattr(resp, "tool_calls", None)
        if isinstance(tool_calls, list):
            record["outcome"] = "no_tool_call"
            for tool_call in tool_calls:
                if getattr(tool_call, "name", None) != SKIPPED_TOOL:
                    continue
                tool_call.arguments, info = restore_arguments(
                    tool_call.arguments, key_map
                )
                record["outcome"] = "tool_call"
                record["sections"] = info["sections"]
                record["mapped"] = info["mapped"]
                record["unmapped"] = [str(k) for k in info["unmapped"]][:10]
                break
        print(
            "ov-wm-update-schema " + json.dumps(record, sort_keys=True),
            file=sys.stderr,
            flush=True,
        )
        return resp

    get_completion_async._ov_wm_update_patch = "schema"
    return get_completion_async


# ------------------------------------------------------------------ skip mode


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

    get_completion_async._ov_wm_update_patch = "skip"
    return get_completion_async


WRAPPERS = {"schema": schema_wm_update, "skip": skipping_wm_update}


def apply(module):
    mode = read_mode()
    if mode is None:
        logger.warning(
            "ov-wm-update-patch: NOT applied (OV_WM_UPDATE_MODE=%r is not one of %s)",
            os.environ.get("OV_WM_UPDATE_MODE"),
            "/".join(MODES),
        )
        return False
    if mode == "off":
        logger.warning("ov-wm-update-patch: disabled (mode off)")
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
    cls.get_completion_async = WRAPPERS[mode](original)
    logger.warning(
        "ov-wm-update-patch: applied in %s mode to %s tool calls (pid %d)",
        mode,
        SKIPPED_TOOL,
        os.getpid(),
    )
    return True
