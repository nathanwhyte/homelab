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
    for, so gemma4's unquoted keys parse. Each section's upstream ``oneOf`` (three
    object shapes, one per op) is flattened to one object with a required ``op`` enum
    plus ``content`` and ``items``: with the ``oneOf`` gemma4 answered every section
    with a bare string (2/2 pairs in the 2026-10-14 replay), which OpenViking's merge
    treats as an unknown op and keeps the old content. The returned tool call is mapped
    back to the title keys before OpenViking's parser sees it; a ``sections`` value that
    came back as a JSON string (seen 2/24 on Claude Haiku 5.5, IMPR-1311) is decoded;
    a section that still arrives as a bare string, a list of strings, or an object
    without ``op`` is coerced to ``UPDATE``/``APPEND``/``KEEP`` and counted, so the
    merge's own guards (append-only sections, loss checks) decide instead of a silent
    keep; and every decision is held to upstream's per-op shape (UPDATE needs a
    non-blank string body, APPEND non-empty string items, else KEEP), because the
    flattened schema cannot express those conditional requirements and the merge
    would clear a section whose body is missing. The incremental update then runs
    when the model returns valid decisions: 8 of 12 pairs in the 2026-10-14 gemma4
    replay, every one merged by OpenViking's own branch logic; the other 4 returned
    no tool call (the op object written as prose) and take OpenViking's own
    creation-prompt fallback after the update call's cost, which is the share to
    watch. One stderr line per call, ``ov-wm-update-schema {json}``, names the
    outcome and how many section keys were mapped, coerced and repaired, so each
    rate is countable in Loki.
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

import contextlib
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


def flatten_op_schema(schema, title):
    """One object schema with a required ``op`` enum instead of upstream's ``oneOf``.

    Upstream describes a section decision as three alternative object shapes. The
    flattened form keeps every property and the union of ``op`` values, says in its
    description which fields each op takes, and names the section title. A schema
    without a usable ``oneOf`` only gains the title description.
    """
    description = f"Decision for the '## {title}' section."
    if not isinstance(schema, dict):
        return schema
    branches = schema.get("oneOf")
    if not isinstance(branches, list) or not branches:
        return dict(schema, description=description)
    ops, properties, rules = [], {}, []
    for branch in branches:
        if not isinstance(branch, dict):
            return dict(schema, description=description)
        branch_props = branch.get("properties") or {}
        branch_ops = [
            op
            for op in ((branch_props.get("op") or {}).get("enum") or [])
            if isinstance(op, str)
        ]
        if not branch_ops:
            return dict(schema, description=description)
        ops.extend(op for op in branch_ops if op not in ops)
        for key, prop in branch_props.items():
            if key != "op":
                properties.setdefault(key, prop)
        extra = [k for k in (branch.get("required") or []) if k != "op"]
        for op in branch_ops:
            rules.append(f"{op} with {', '.join(extra)}" if extra else f"{op} alone")
    return {
        "type": "object",
        "required": ["op"],
        "additionalProperties": False,
        "description": f"{description} Emit {'; '.join(rules)}.",
        "properties": {"op": {"type": "string", "enum": ops}, **properties},
    }


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
            key: flatten_op_schema(props[title], title) for key, title in ids.items()
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


def _substitute_quoted_keys(text, key_map, precedence=True):
    """Rename ``"<id>"`` to ``"<Title>"`` inside raw JSON text (regex salvage input).

    Returns ``(text, renamed, collisions)``. With ``precedence`` an id whose title
    already appears as a quoted key in the text is left as it is, so upstream's
    salvage (which matches titles only) keeps the title's decision whichever came
    first; ``_salvage`` passes ``precedence=False`` and resolves collisions itself.
    """
    if not key_map:
        return text, 0, []
    collisions = [
        title
        for key, title in key_map.items()
        if precedence
        and re.search(rf'"{re.escape(key)}"\s*:', text)
        and re.search(rf'"{re.escape(title)}"\s*:', text)
    ]
    renamable = {k: t for k, t in key_map.items() if t not in collisions}
    if not renamable:
        return text, 0, collisions
    pattern = re.compile(
        r'"(' + "|".join(re.escape(key) for key in renamable) + r')"(\s*:)'
    )
    text, count = pattern.subn(lambda m: f'"{renamable[m.group(1)]}"{m.group(2)}', text)
    return text, count, collisions


def _salvage(text, key_map):
    """Upstream's regex salvage over undecodable raw text, with title precedence.

    Runs ``Session._wm_recover_ops_from_raw`` (the same function Branch 2 would run)
    twice: over the text as it came, which finds decisions keyed by title, and over
    the text with the titles masked and every id renamed, which finds the ones keyed
    by id; a title's decision wins a collision. Returns ``(ops, counts)``, the
    decisions still un-normalized and ``counts`` holding ``mapped`` (id decisions),
    ``unmapped`` (title decisions) and ``collisions`` (both), or ``None`` when
    openviking is not importable here or salvage finds nothing (the caller then
    forwards the text for upstream to handle).
    """
    try:
        from openviking.session.session import Session
    except Exception:  # noqa: BLE001 — outside the OV process there is no salvage
        return None
    recover = getattr(Session, "_wm_recover_ops_from_raw", None)
    if recover is None:
        return None
    by_title = recover(text) or {}
    # Mask the title keys before renaming the ids, so this pass recovers only the
    # decisions that arrived under an id (otherwise every title decision would be
    # counted again as an alias).
    masked = text
    for title in key_map.values():
        masked = re.sub(
            rf'"{re.escape(title)}"(\s*:)', lambda m: f'"\u0000"{m.group(1)}', masked
        )
    renamed, _, _ = _substitute_quoted_keys(masked, key_map, precedence=False)
    by_alias = recover(renamed) or {}
    collisions = sorted(title for title in by_alias if title in by_title)
    ops = dict(by_alias)
    ops.update(by_title)
    if not ops:
        return None
    return ops, {
        "collisions": collisions,
        "mapped": len(by_alias),
        "unmapped": [title for title in by_title],
    }


def _decode_raw(text):
    """Upstream's recovery of a raw tool-call string (session.py 4627-4648): a JSON
    object, or a truncated one closed with the missing braces; ``None`` otherwise."""
    if not isinstance(text, str):
        return None
    try:
        decoded = json.loads(text)
    except ValueError:
        decoded = None
        opens = text.count("{") - text.count("}")
        if opens > 0:
            with contextlib.suppress(ValueError):
                decoded = json.loads(text.rstrip().rstrip(",") + "}" * opens)
    return decoded if isinstance(decoded, dict) else None


OP_KEEP, OP_UPDATE, OP_APPEND = "KEEP", "UPDATE", "APPEND"
OPS = (OP_KEEP, OP_UPDATE, OP_APPEND)


def _text_items(items):
    """The non-blank strings of a list, or ``None`` when ``items`` is not a list."""
    if not isinstance(items, list):
        return None
    return [item for item in items if isinstance(item, str) and item.strip()]


def coerce_op(value):
    """``(decision, coerced)``: a bare string, string list or op-less object as an op.

    A non-blank string that is not itself an op name is the section's replacement
    body (UPDATE); a bare op name is that op with no payload (which
    ``normalize_decision`` then turns into KEEP unless it is KEEP); a list of strings
    is new items (APPEND); an empty string or list, or an op-less object whose
    ``content``/``items`` is empty or of the wrong type, is KEEP, so an ambiguous
    value never replaces a section. A decision that carries ``op`` passes through
    untouched, and so does anything else, for OpenViking's merge to judge.
    """
    if isinstance(value, dict):
        if "op" in value:
            return value, False
        content = value.get("content")
        if isinstance(content, str) and content.strip():
            return {"op": OP_UPDATE, **value}, True
        if _text_items(value.get("items")):
            return {"op": OP_APPEND, **value}, True
        if "content" in value or "items" in value:
            return {"op": OP_KEEP}, True
        return value, False
    if isinstance(value, str):
        token = value.strip().upper()
        if token in OPS:
            return {"op": token}, True
        if token:
            return {"op": OP_UPDATE, "content": value}, True
        return {"op": OP_KEEP}, True
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        if _text_items(value):
            return {"op": OP_APPEND, "items": value}, True
        return {"op": OP_KEEP}, True
    return value, False


def normalize_decision(value):
    """``(decision, repaired)``: hold a decision to upstream's per-op shape.

    The flattened schema only requires ``op``, so the model can answer ``UPDATE``
    without ``content``: upstream's merge turns the missing body into an empty one and
    clears the section (an APPEND without items is a no-op there, but a list body or
    non-string items raise outside Branch 2's exception handler and fail the commit).
    Here UPDATE needs a non-blank string ``content`` and APPEND a non-empty list of
    strings (non-string items dropped, as the merge drops them), or the decision
    becomes KEEP; KEEP carries nothing else; a non-string op (the merge calls
    ``.upper()`` on it) becomes KEEP. An unknown op string passes through for the
    merge to judge (it keeps the old content and logs).
    """
    if not isinstance(value, dict) or "op" not in value:
        return value, False
    if not isinstance(value["op"], str):
        return {"op": OP_KEEP}, True
    op = value["op"].strip().upper()
    if op == OP_UPDATE:
        content = value.get("content")
        if isinstance(content, str) and content.strip():
            fixed = {"op": OP_UPDATE, "content": content}
        else:
            fixed = {"op": OP_KEEP}
    elif op == OP_APPEND:
        items = _text_items(value.get("items"))
        fixed = {"op": OP_APPEND, "items": items} if items else {"op": OP_KEEP}
    elif op == OP_KEEP:
        fixed = {"op": OP_KEEP}
    else:
        return value, False
    return fixed, fixed != value


def restore_arguments(args, key_map):
    """Map a returned tool call's section ids back to titles.

    Returns ``(new_args, info)``; ``info`` carries ``sections`` (how the value
    arrived), ``mapped`` (keys renamed), ``unmapped`` (keys left as they came, the
    seven titles themselves included), ``coerced`` (section values rewritten by
    ``coerce_op``), ``repaired`` (decisions reshaped by ``normalize_decision``) and
    ``collisions`` (titles that arrived under both their id and their title; the
    title's decision wins). Anything of an unexpected shape is returned as it was,
    so OpenViking's own tolerant parser still gets its chance.
    """
    info = {
        "sections": None,
        "mapped": 0,
        "unmapped": [],
        "coerced": 0,
        "repaired": 0,
        "collisions": [],
    }
    titles = set(key_map.values())

    def rename(ops):
        out = {}
        from_title = set()
        for key, value in ops.items():
            title = key_map.get(key) if isinstance(key, str) else None
            is_alias = title is not None
            if is_alias:
                info["mapped"] += 1
            else:
                info["unmapped"].append(key)
                title = key
            if title in titles:
                value, coerced = coerce_op(value)
                value, repaired = normalize_decision(value)
                info["coerced"] += coerced
                info["repaired"] += repaired
                if title in out:
                    info["collisions"].append(title)
                    if is_alias and title in from_title:
                        continue
                if not is_alias:
                    from_title.add(title)
            out[title] = value
        return out

    raw_text = None
    if isinstance(args, str):
        raw_text, label = args, "text"
    elif (
        isinstance(args, dict)
        and list(args.keys()) == ["raw"]
        and isinstance(args["raw"], str)
    ):
        raw_text, label = args["raw"], "raw"
    if raw_text is not None:
        # Decode what upstream would recover (a whole or brace-closed object) and
        # take the structured path, so its decisions are normalized too; only text
        # that stays undecodable goes to upstream's regex salvage, titles renamed.
        decoded = _decode_raw(raw_text)
        if decoded is not None:
            new_args, info = restore_arguments(decoded, key_map)
            info["sections"] = f"{label}->{info['sections']}"
            return new_args, info
        # Undecodable: run upstream's own salvage here so its decisions are
        # normalized too, and hand Branch 2 a structured object.
        salvaged = _salvage(raw_text, key_map)
        if salvaged is not None:
            ops, counts = salvaged
            sections = {}
            for title, decision in ops.items():
                decision, repaired = normalize_decision(decision)
                info["repaired"] += repaired
                sections[title] = decision
            info.update(sections=f"{label}->salvaged", **counts)
            return {"sections": sections}, info
        # No openviking here (tests) or nothing to salvage: titles renamed for
        # upstream's own salvage, which then finds the same nothing or the same ops.
        new_text, count, collisions = _substitute_quoted_keys(raw_text, key_map)
        info.update(sections=label, mapped=count, collisions=collisions)
        return (new_text if label == "text" else {"raw": new_text}), info
    if not isinstance(args, dict):
        info["sections"] = type(args).__name__
        return args, info
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


def _emit(prefix, record):
    """One stderr line, best effort: a failing stderr never fails the VLM call."""
    with contextlib.suppress(Exception):
        sys.stderr.write(prefix + json.dumps(record, sort_keys=True) + "\n")
        sys.stderr.flush()


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
            "coerced": 0,
            "repaired": 0,
            "collisions": [],
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
                record["coerced"] = info["coerced"]
                record["repaired"] = info["repaired"]
                record["collisions"] = info["collisions"]
                break
        _emit("ov-wm-update-schema ", record)
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

        _emit(
            "ov-wm-update-skip ",
            {
                "tool": SKIPPED_TOOL,
                "prompt_chars": len(prompt) if isinstance(prompt, str) else None,
                "skips": _skips,
                "pid": os.getpid(),
            },
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
