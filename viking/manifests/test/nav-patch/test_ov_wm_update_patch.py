"""Detection, mode selection, schema rewrite, response mapping, skip, and guard checks."""

import asyncio
import copy
import io
import json
import os
import sys
import types
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ov_wm_update_patch as wm_patch

TITLES = [
    "Session Title",
    "Current State",
    "Task & Goals",
    "Key Facts & Decisions",
    "Files & Context",
    "Errors & Corrections",
    "Open Issues",
]
IDS = [
    "session_title",
    "current_state",
    "task_and_goals",
    "key_facts_and_decisions",
    "files_and_context",
    "errors_and_corrections",
    "open_issues",
]
OP_SCHEMA = {  # upstream _WM_SECTION_OP_SCHEMA (session.py 301-342), trimmed
    "oneOf": [
        {
            "type": "object",
            "required": ["op"],
            "additionalProperties": False,
            "properties": {"op": {"type": "string", "enum": ["KEEP"]}},
        },
        {
            "type": "object",
            "required": ["op", "content"],
            "additionalProperties": False,
            "properties": {
                "op": {"type": "string", "enum": ["UPDATE"]},
                "content": {"type": "string", "description": "FULL replacement"},
            },
        },
        {
            "type": "object",
            "required": ["op", "items"],
            "additionalProperties": False,
            "properties": {
                "op": {"type": "string", "enum": ["APPEND"]},
                "items": {"type": "array", "items": {"type": "string"}},
            },
        },
    ]
}
FLAT_DESCRIPTION = (
    "Decision for the '## Current State' section. "
    "Emit KEEP alone; UPDATE with content; APPEND with items."
)


def _update_tool():
    """The v0.4.20 ``WM_UPDATE_TOOL`` shape (session.py 344-375), trimmed."""
    return {
        "type": "function",
        "function": {
            "name": "update_working_memory",
            "description": "Emit a per-section decision.",
            "parameters": {
                "type": "object",
                "required": ["sections"],
                "additionalProperties": False,
                "properties": {
                    "sections": {
                        "type": "object",
                        "required": list(TITLES),
                        "additionalProperties": False,
                        "properties": dict.fromkeys(TITLES, OP_SCHEMA),
                    },
                    "checkpoint_summaries": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
    }


UPDATE_TOOL = _update_tool()
CREATE_TOOL = {"type": "function", "function": {"name": "create_working_memory"}}
CHOICE = {"type": "function", "function": {"name": "update_working_memory"}}


class _FakeToolCall:
    def __init__(self, name, arguments, id="call_1"):
        self.id = id
        self.name = name
        self.arguments = arguments


class _FakeVLMResponse:
    def __init__(self, content=None, tool_calls=None, finish_reason="stop", usage=None):
        self.content = content
        self.tool_calls = list(tool_calls or [])
        self.finish_reason = finish_reason
        self.usage = usage or {}

    @property
    def has_tool_calls(self):
        return bool(self.tool_calls)


def _fake_base_module():
    return types.SimpleNamespace(VLMResponse=_FakeVLMResponse)


def _run(coro):
    return asyncio.run(coro)


def _ops(keys, op="KEEP"):
    return {k: {"op": op} for k in keys}


class DetectionTests(unittest.TestCase):
    def test_tools_list_names_the_update_tool(self):
        self.assertTrue(wm_patch.is_wm_update_call([UPDATE_TOOL]))
        self.assertTrue(wm_patch.is_wm_update_call([CREATE_TOOL, UPDATE_TOOL]))

    def test_other_tools_and_no_tools_pass(self):
        self.assertFalse(wm_patch.is_wm_update_call([CREATE_TOOL]))
        self.assertFalse(wm_patch.is_wm_update_call(None))
        self.assertFalse(wm_patch.is_wm_update_call([]))
        self.assertFalse(wm_patch.is_wm_update_call(["not a dict", {"function": "x"}]))

    def test_tool_choice_alone_never_matches(self):
        self.assertFalse(wm_patch.is_wm_update_call(None, CHOICE))
        self.assertFalse(wm_patch.is_wm_update_call([CREATE_TOOL], CHOICE))
        self.assertFalse(wm_patch.is_wm_update_call([], CHOICE))
        self.assertTrue(wm_patch.is_wm_update_call([UPDATE_TOOL], "auto"))


class ModeTests(unittest.TestCase):
    def test_default_is_schema(self):
        self.assertEqual(wm_patch.read_mode({}), "schema")

    def test_explicit_modes(self):
        for mode in ("schema", "skip", "off"):
            self.assertEqual(wm_patch.read_mode({"OV_WM_UPDATE_MODE": mode}), mode)

    def test_unknown_mode_is_rejected(self):
        self.assertIsNone(wm_patch.read_mode({"OV_WM_UPDATE_MODE": "snake"}))
        self.assertIsNone(wm_patch.read_mode({"OV_WM_UPDATE_MODE": ""}))

    def test_legacy_skip_switch(self):
        self.assertEqual(wm_patch.read_mode({"OV_WM_UPDATE_SKIP": "1"}), "skip")
        self.assertEqual(wm_patch.read_mode({"OV_WM_UPDATE_SKIP": "0"}), "off")
        self.assertEqual(wm_patch.read_mode({"OV_WM_UPDATE_SKIP": "maybe"}), "schema")

    def test_mode_wins_over_legacy_switch(self):
        env = {"OV_WM_UPDATE_MODE": "schema", "OV_WM_UPDATE_SKIP": "1"}
        self.assertEqual(wm_patch.read_mode(env), "schema")


class SchemaRewriteTests(unittest.TestCase):
    def test_seven_titles_map_to_ids(self):
        self.assertEqual([wm_patch.snake_key(t) for t in TITLES], IDS)

    def test_sections_keys_become_ids_and_originals_are_untouched(self):
        tools = [CREATE_TOOL, _update_tool()]
        before = copy.deepcopy(tools)
        new_tools, key_map = wm_patch.rewrite_tools(tools)
        self.assertEqual(tools, before)
        self.assertEqual(key_map, dict(zip(IDS, TITLES)))
        self.assertEqual(new_tools[0], CREATE_TOOL)
        sections = new_tools[1]["function"]["parameters"]["properties"]["sections"]
        self.assertEqual(list(sections["properties"]), IDS)
        self.assertEqual(sections["required"], IDS)
        self.assertFalse(sections["additionalProperties"])
        self.assertIn("current_state = '## Current State'", sections["description"])
        state = sections["properties"]["current_state"]
        self.assertEqual(
            state,
            {
                "type": "object",
                "required": ["op"],
                "additionalProperties": False,
                "description": FLAT_DESCRIPTION,
                "properties": {
                    "op": {"type": "string", "enum": ["KEEP", "UPDATE", "APPEND"]},
                    "content": {"type": "string", "description": "FULL replacement"},
                    "items": {"type": "array", "items": {"type": "string"}},
                },
            },
        )
        params = new_tools[1]["function"]["parameters"]
        self.assertEqual(params["required"], ["sections"])
        self.assertIn("checkpoint_summaries", params["properties"])

    def test_flatten_keeps_unknown_op_schemas_and_adds_the_title(self):
        plain = {"type": "object", "properties": {"op": {"type": "string"}}}
        self.assertEqual(
            wm_patch.flatten_op_schema(plain, "Open Issues"),
            dict(plain, description="Decision for the '## Open Issues' section."),
        )
        no_enum = {"oneOf": [{"type": "object", "properties": {"op": {}}}]}
        self.assertEqual(
            wm_patch.flatten_op_schema(no_enum, "Open Issues")["oneOf"],
            no_enum["oneOf"],
        )
        self.assertEqual(wm_patch.flatten_op_schema("x", "Open Issues"), "x")

    def test_unknown_shapes_are_forwarded_as_is(self):
        flat = {"type": "function", "function": {"name": "update_working_memory"}}
        self.assertEqual(wm_patch.rewrite_tools([flat]), ([flat], None))
        colliding = _update_tool()
        colliding["function"]["parameters"]["properties"]["sections"]["properties"] = {
            "Open Issues": OP_SCHEMA,
            "Open  Issues": OP_SCHEMA,
        }
        self.assertEqual(wm_patch.rewrite_tools([colliding]), ([colliding], None))
        self.assertEqual(wm_patch.rewrite_tools([CREATE_TOOL]), ([CREATE_TOOL], None))


class RestoreArgumentsTests(unittest.TestCase):
    def setUp(self):
        self.key_map = dict(zip(IDS, TITLES))

    def test_ids_map_back_to_titles_in_order(self):
        args = {"sections": _ops(IDS), "checkpoint_summaries": ["x"]}
        new_args, info = wm_patch.restore_arguments(args, self.key_map)
        self.assertEqual(list(new_args["sections"]), TITLES)
        self.assertEqual(new_args["checkpoint_summaries"], ["x"])
        self.assertEqual(
            info,
            {
                "sections": "dict",
                "mapped": 7,
                "unmapped": [],
                "coerced": 0,
                "repaired": 0,
                "collisions": [],
            },
        )
        self.assertEqual(list(args["sections"]), IDS)  # input not mutated

    def test_op_names_empty_and_mistyped_values_never_replace_a_section(self):
        self.assertEqual(wm_patch.coerce_op("KEEP"), ({"op": "KEEP"}, True))
        self.assertEqual(wm_patch.coerce_op(" update "), ({"op": "UPDATE"}, True))
        self.assertEqual(wm_patch.coerce_op("   "), ({"op": "KEEP"}, True))
        self.assertEqual(wm_patch.coerce_op({"content": ""}), ({"op": "KEEP"}, True))
        self.assertEqual(
            wm_patch.coerce_op({"content": ["new state"]}), ({"op": "KEEP"}, True)
        )
        self.assertEqual(wm_patch.coerce_op({"items": 7}), ({"op": "KEEP"}, True))
        self.assertEqual(wm_patch.coerce_op({"items": [" "]}), ({"op": "KEEP"}, True))
        # Through restore: a bare op name and an empty implicit body both end as KEEP.
        args = {
            "sections": {"current_state": "KEEP", "task_and_goals": {"content": ""}}
        }
        new_args, info = wm_patch.restore_arguments(args, self.key_map)
        self.assertEqual(
            new_args["sections"],
            {"Current State": {"op": "KEEP"}, "Task & Goals": {"op": "KEEP"}},
        )
        self.assertEqual((info["coerced"], info["repaired"]), (2, 0))

    def test_decisions_are_held_to_the_per_op_shape(self):
        cases = [
            ({"op": "UPDATE"}, {"op": "KEEP"}, True),
            ({"op": "UPDATE", "content": " "}, {"op": "KEEP"}, True),
            ({"op": "UPDATE", "content": ["x"]}, {"op": "KEEP"}, True),
            (
                {"op": "update", "content": "body"},
                {"op": "UPDATE", "content": "body"},
                True,
            ),
            (
                {"op": "UPDATE", "content": "body", "items": []},
                {"op": "UPDATE", "content": "body"},
                True,
            ),
            ({"op": "APPEND"}, {"op": "KEEP"}, True),
            ({"op": "APPEND", "items": "one"}, {"op": "KEEP"}, True),
            (
                {"op": "APPEND", "items": ["a", 3, " ", "b"]},
                {"op": "APPEND", "items": ["a", "b"]},
                True,
            ),
            ({"op": "APPEND", "items": ["a"]}, {"op": "APPEND", "items": ["a"]}, False),
            ({"op": "KEEP", "items": []}, {"op": "KEEP"}, True),
            ({"op": "KEEP"}, {"op": "KEEP"}, False),
            ({"op": "BOGUS"}, {"op": "BOGUS"}, False),
            ({"op": 5}, {"op": "KEEP"}, True),
            ({"op": True, "content": "x"}, {"op": "KEEP"}, True),
            ({"op": ["UPDATE"], "content": "x"}, {"op": "KEEP"}, True),
            ({"op": None}, {"op": "KEEP"}, True),
            ("text", "text", False),
        ]
        for value, expected, repaired in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    wm_patch.normalize_decision(value), (expected, repaired)
                )
        args = {
            "sections": {
                "current_state": {"op": "UPDATE"},
                "open_issues": {"op": "KEEP", "items": []},
            }
        }
        new_args, info = wm_patch.restore_arguments(args, self.key_map)
        self.assertEqual(
            new_args["sections"],
            {"Current State": {"op": "KEEP"}, "Open Issues": {"op": "KEEP"}},
        )
        self.assertEqual((info["coerced"], info["repaired"]), (0, 2))

    def test_title_and_id_for_one_section_keep_the_title_decision(self):
        append = {"op": "APPEND", "items": ["new task"]}
        for ops in (
            {"Current State": append, "current_state": {"op": "KEEP"}},
            {"current_state": {"op": "KEEP"}, "Current State": append},
        ):
            with self.subTest(order=list(ops)):
                new_args, info = wm_patch.restore_arguments(
                    {"sections": ops}, self.key_map
                )
                self.assertEqual(new_args["sections"], {"Current State": append})
                self.assertEqual(info["collisions"], ["Current State"])
                self.assertEqual(info["mapped"], 1)
                self.assertEqual(info["unmapped"], ["Current State"])

    def test_restored_decisions_merge_in_the_installed_openviking(self):
        try:
            from openviking.session.session import Session
        except Exception:  # noqa: BLE001 — the system Python has no openviking
            self.skipTest("openviking not importable here")
        prior = "\n".join(
            f"## {title}\n\nold {title.lower()} body\n" for title in TITLES
        )
        sections = {
            "session_title": "KEEP",
            "current_state": {"op": "UPDATE"},
            "task_and_goals": {"content": ["not a string"]},
            "key_facts_and_decisions": {"op": "APPEND", "items": ["a fact", 3]},
            "files_and_context": {"items": 7},
            "errors_and_corrections": "",
            "open_issues": {"op": "KEEP", "items": []},
        }
        new_args, _info = wm_patch.restore_arguments(
            {"sections": sections}, self.key_map
        )
        merged = Session._merge_wm_sections(prior, new_args["sections"])
        parsed = Session._parse_wm_sections(merged)
        for title in TITLES:
            if title != "Key Facts & Decisions":
                self.assertIn(
                    "old " + title.lower() + " body", parsed[f"## {title}"], title
                )
        self.assertIn("a fact", parsed["## Key Facts & Decisions"])
        self.assertNotIn("KEEP", parsed["## Session Title"])

    def test_bare_values_are_coerced_into_decisions_and_counted(self):
        args = {
            "sections": {
                "session_title": "New title",
                "current_state": "",
                "open_issues": ["one", "two"],
                "errors_and_corrections": [],
                "files_and_context": {"content": "paths"},
                "key_facts_and_decisions": {"items": ["fact"]},
                "task_and_goals": {"op": "KEEP"},
                "Extra": "left alone",
            }
        }
        new_args, info = wm_patch.restore_arguments(args, self.key_map)
        self.assertEqual(
            new_args["sections"],
            {
                "Session Title": {"op": "UPDATE", "content": "New title"},
                "Current State": {"op": "KEEP"},
                "Open Issues": {"op": "APPEND", "items": ["one", "two"]},
                "Errors & Corrections": {"op": "KEEP"},
                "Files & Context": {"op": "UPDATE", "content": "paths"},
                "Key Facts & Decisions": {"op": "APPEND", "items": ["fact"]},
                "Task & Goals": {"op": "KEEP"},
                "Extra": "left alone",
            },
        )
        self.assertEqual((info["mapped"], info["coerced"]), (7, 6))
        self.assertEqual(info["unmapped"], ["Extra"])
        self.assertEqual(wm_patch.coerce_op(5), (5, False))
        self.assertEqual(wm_patch.coerce_op([1, "a"]), ([1, "a"], False))
        self.assertEqual(wm_patch.coerce_op({"other": 1}), ({"other": 1}, False))

    def test_titles_and_unknown_keys_pass_through(self):
        args = {"sections": _ops(["Current State", "open_issues", "Extra"])}
        new_args, info = wm_patch.restore_arguments(args, self.key_map)
        self.assertEqual(
            list(new_args["sections"]), ["Current State", "Open Issues", "Extra"]
        )
        self.assertEqual(info["mapped"], 1)
        self.assertEqual(info["unmapped"], ["Current State", "Extra"])

    def test_sections_as_json_string_is_decoded(self):
        args = {"sections": json.dumps(_ops(IDS[:2], "UPDATE"))}
        new_args, info = wm_patch.restore_arguments(args, self.key_map)
        self.assertEqual(list(new_args["sections"]), TITLES[:2])
        self.assertEqual(info["sections"], "str->dict")
        self.assertEqual(info["mapped"], 2)

    def test_undecodable_or_non_object_string_is_left_alone(self):
        for value, label in (("{not json", "str-unparsed"), ("[1]", "str-list")):
            new_args, info = wm_patch.restore_arguments(
                {"sections": value}, self.key_map
            )
            self.assertEqual(new_args, {"sections": value})
            self.assertEqual(info["sections"], label)
            self.assertEqual(info["mapped"], 0)

    def test_top_level_ops_are_renamed(self):
        new_args, info = wm_patch.restore_arguments(_ops(IDS), self.key_map)
        self.assertEqual(list(new_args), TITLES)
        self.assertEqual(info["sections"], "top-level")
        self.assertEqual(info["mapped"], 7)

    def test_decodable_raw_takes_the_structured_path_with_normalization(self):
        # Truncated as upstream's brace-close recovery would see it: two braces short.
        raw = '{"sections": {"current_state": {"op": "UPDATE"}, "open_issues":{"op": "UPDATE", "content": "see current_state"}'
        for args, label in (({"raw": raw}, "raw->dict"), (raw, "text->dict")):
            with self.subTest(label=label):
                new_args, info = wm_patch.restore_arguments(args, self.key_map)
                self.assertEqual(
                    new_args,
                    {
                        "sections": {
                            "Current State": {"op": "KEEP"},
                            "Open Issues": {
                                "op": "UPDATE",
                                "content": "see current_state",
                            },
                        }
                    },
                )
                self.assertEqual(info["sections"], label)
                self.assertEqual((info["mapped"], info["repaired"]), (2, 1))
        listy = '{"sections": {"current_state": {"op": "UPDATE", "content": ["x"]}}}'
        new_args, info = wm_patch.restore_arguments({"raw": listy}, self.key_map)
        self.assertEqual(new_args["sections"], {"Current State": {"op": "KEEP"}})

    def test_undecodable_raw_gets_quoted_key_substitution_with_title_precedence(
        self,
    ):
        raw = '{"sections": {"current_state": {"op": "KEEP"}, "open_issues": {"op": "UPDATE", "content": "see current_state'
        new_args, info = wm_patch.restore_arguments({"raw": raw}, self.key_map)
        self.assertEqual(
            new_args["raw"],
            '{"sections": {"Current State": {"op": "KEEP"}, "Open Issues": {"op": "UPDATE", "content": "see current_state',
        )
        self.assertEqual(
            info,
            {
                "sections": "raw",
                "mapped": 2,
                "unmapped": [],
                "coerced": 0,
                "repaired": 0,
                "collisions": [],
            },
        )
        new_text, info = wm_patch.restore_arguments(raw, self.key_map)
        self.assertEqual(new_text, new_args["raw"])
        self.assertEqual(info["sections"], "text")
        # Title and id both present: the id stays unrenamed, so upstream's salvage
        # (titles only) keeps the title's decision whichever came first.
        for ordered in (
            '{"sections": {"Current State": {"op": "KEEP"}, "current_state": {"op": "UPDATE", "content": "x',
            '{"sections": {"current_state": {"op": "UPDATE", "content": "x"}, "Current State": {"op": "KEEP"}, "open_issues": {"op": "UPDATE", "content": "y',
        ):
            with self.subTest(ordered=ordered[:40]):
                new_args, info = wm_patch.restore_arguments(
                    {"raw": ordered}, self.key_map
                )
                self.assertIn('"current_state":', new_args["raw"])
                self.assertEqual(info["collisions"], ["Current State"])
                self.assertNotIn('"Open Issues"', ordered)
                if "open_issues" in ordered:
                    self.assertIn('"Open Issues":', new_args["raw"])

    def test_other_values_are_returned_unchanged(self):
        self.assertEqual(wm_patch.restore_arguments(7, self.key_map)[0], 7)
        self.assertEqual(wm_patch.restore_arguments(None, self.key_map)[0], None)
        new_args, info = wm_patch.restore_arguments({"sections": ["a"]}, self.key_map)
        self.assertEqual(new_args, {"sections": ["a"]})
        self.assertEqual(info["sections"], "list")


class SchemaWrapperTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.response = _FakeVLMResponse(
            tool_calls=[
                _FakeToolCall("update_working_memory", {"sections": _ops(IDS)})
            ],
            finish_reason="tool_calls",
            usage={"completion_tokens": 321},
        )

        async def original(self_, prompt="", *args, **kwargs):
            self.calls.append((prompt, args, kwargs))
            if wm_patch.is_wm_update_call(
                kwargs.get("tools", args[1] if len(args) > 1 else None)
            ):
                return self.response
            return "model text"

        self.wrapped = wm_patch.schema_wm_update(original)
        self.vlm = object()

    def _forwarded_tools(self):
        _prompt, args, kwargs = self.calls[-1]
        return kwargs["tools"] if "tools" in kwargs else args[1]

    def test_update_call_goes_out_snake_cased_and_comes_back_titled(self):
        err = io.StringIO()
        with redirect_stderr(err):
            resp = _run(
                self.wrapped(
                    self.vlm, "prompt", tools=[UPDATE_TOOL], tool_choice=CHOICE
                )
            )
        self.assertIs(resp, self.response)
        sent = self._forwarded_tools()
        self.assertEqual(
            list(
                sent[0]["function"]["parameters"]["properties"]["sections"][
                    "properties"
                ]
            ),
            IDS,
        )
        self.assertEqual(self.calls[-1][2]["tool_choice"], CHOICE)
        self.assertEqual(list(resp.tool_calls[0].arguments["sections"]), TITLES)
        line = err.getvalue().strip()
        self.assertTrue(line.startswith("ov-wm-update-schema {"), line)
        record = json.loads(line[len("ov-wm-update-schema ") :])
        self.assertEqual(record["outcome"], "tool_call")
        self.assertEqual(record["mode"], "schema")
        self.assertEqual(record["sections"], "dict")
        self.assertEqual(record["mapped"], 7)
        self.assertEqual(record["unmapped"], [])
        self.assertEqual((record["coerced"], record["repaired"]), (0, 0))
        self.assertEqual(record["collisions"], [])
        self.assertEqual(record["finish_reason"], "tool_calls")
        self.assertEqual(record["completion_tokens"], 321)
        self.assertEqual(record["prompt_chars"], 6)
        self.assertIsInstance(record["elapsed_s"], float)

    def test_a_failing_stderr_never_fails_the_call(self):
        class Broken(io.StringIO):
            def write(self, s):
                raise BrokenPipeError("stderr closed")

        with redirect_stderr(Broken()):
            resp = _run(self.wrapped(self.vlm, "p", tools=[UPDATE_TOOL]))
        self.assertIs(resp, self.response)
        self.assertEqual(list(resp.tool_calls[0].arguments["sections"]), TITLES)
        skip = wm_patch.skipping_wm_update(self.wrapped)
        with (
            patch.dict(
                sys.modules, {"openviking.models.vlm.base": _fake_base_module()}
            ),
            redirect_stderr(Broken()),
        ):
            resp = _run(skip(self.vlm, "p", tools=[UPDATE_TOOL]))
        self.assertFalse(resp.has_tool_calls)

    def test_positional_tools_are_rewritten_in_place(self):
        with redirect_stderr(io.StringIO()):
            resp = _run(self.wrapped(self.vlm, "p", None, [UPDATE_TOOL], CHOICE))
        self.assertIs(resp, self.response)
        _prompt, args, kwargs = self.calls[-1]
        self.assertEqual(kwargs, {})
        self.assertIsNone(args[0])
        self.assertEqual(args[2], CHOICE)
        sections = args[1][0]["function"]["parameters"]["properties"]["sections"]
        self.assertEqual(sections["required"], IDS)
        self.assertEqual(
            UPDATE_TOOL["function"]["parameters"]["properties"]["sections"]["required"],
            TITLES,
        )

    def test_no_tool_call_is_logged_and_returned_untouched(self):
        self.response = _FakeVLMResponse(content="prose", finish_reason="stop")
        err = io.StringIO()
        with redirect_stderr(err):
            resp = _run(self.wrapped(self.vlm, "p", tools=[UPDATE_TOOL]))
        self.assertIs(resp, self.response)
        record = json.loads(err.getvalue().split(" ", 1)[1])
        self.assertEqual(record["outcome"], "no_tool_call")
        self.assertIsNone(record["sections"])

    def test_other_tool_calls_in_the_response_are_left_alone(self):
        other = _FakeToolCall("something_else", {"sections": _ops(IDS)})
        self.response = _FakeVLMResponse(tool_calls=[other], finish_reason="tool_calls")
        with redirect_stderr(io.StringIO()):
            resp = _run(self.wrapped(self.vlm, "p", tools=[UPDATE_TOOL]))
        self.assertEqual(list(resp.tool_calls[0].arguments["sections"]), IDS)

    def test_unknown_schema_shape_is_forwarded_as_is(self):
        flat = {"type": "function", "function": {"name": "update_working_memory"}}
        err = io.StringIO()
        with redirect_stderr(err):
            resp = _run(self.wrapped(self.vlm, "p", tools=[flat]))
        self.assertIs(resp, self.response)
        self.assertIs(self._forwarded_tools()[0], flat)
        self.assertNotIn("ov-wm-update-schema", err.getvalue())
        self.assertIn("forwarded as is", err.getvalue())

    def test_other_calls_pass_through_with_arguments_intact(self):
        self.assertEqual(_run(self.wrapped(self.vlm, "plain prompt")), "model text")
        resp = _run(
            self.wrapped(
                self.vlm,
                "create",
                tools=[CREATE_TOOL],
                tool_choice="auto",
                max_tokens=5,
            )
        )
        self.assertEqual(resp, "model text")
        msgs = [{"role": "user", "content": "hi"}]
        self.assertEqual(
            _run(self.wrapped(self.vlm, "", messages=msgs, max_tokens=64)), "model text"
        )
        self.assertEqual(
            self.calls,
            [
                ("plain prompt", (), {}),
                (
                    "create",
                    (),
                    {"tools": [CREATE_TOOL], "tool_choice": "auto", "max_tokens": 5},
                ),
                ("", (), {"messages": msgs, "max_tokens": 64}),
            ],
        )

    def test_original_errors_propagate(self):
        async def fail(self_, prompt="", *args, **kwargs):
            raise RuntimeError("upstream down")

        with self.assertRaisesRegex(RuntimeError, "upstream down"):
            _run(wm_patch.schema_wm_update(fail)(self.vlm, "p", tools=[UPDATE_TOOL]))


class SkipWrapperTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        async def original(self_, prompt="", *args, **kwargs):
            self.calls.append((prompt, args, kwargs))
            return "model text"

        self.original = original
        self.wrapped = wm_patch.skipping_wm_update(original)
        self.vlm = object()

    def test_update_call_is_answered_locally(self):
        err = io.StringIO()
        with (
            patch.dict(
                sys.modules, {"openviking.models.vlm.base": _fake_base_module()}
            ),
            redirect_stderr(err),
        ):
            resp = _run(
                self.wrapped(
                    self.vlm, "prompt", tools=[UPDATE_TOOL], tool_choice=CHOICE
                )
            )
        self.assertEqual(self.calls, [])
        self.assertIsInstance(resp, _FakeVLMResponse)
        self.assertFalse(resp.has_tool_calls)
        self.assertEqual(resp.tool_calls, [])
        self.assertEqual(resp.finish_reason, "stop")
        line = err.getvalue().strip()
        self.assertTrue(line.startswith("ov-wm-update-skip {"), line)
        self.assertIn('"prompt_chars": 6', line)
        self.assertIn('"tool": "update_working_memory"', line)

    def test_choice_without_the_tool_passes_through(self):
        resp = _run(
            self.wrapped(self.vlm, "p", tools=[CREATE_TOOL], tool_choice=CHOICE)
        )
        self.assertEqual(resp, "model text")
        resp = _run(self.wrapped(self.vlm, "p", tool_choice=CHOICE))
        self.assertEqual(resp, "model text")
        self.assertEqual(len(self.calls), 2)

    def test_messages_path_passes_through(self):
        msgs = [{"role": "user", "content": "hi"}]
        resp = _run(self.wrapped(self.vlm, "", messages=msgs, max_tokens=64))
        self.assertEqual(resp, "model text")
        self.assertEqual(self.calls, [("", (), {"messages": msgs, "max_tokens": 64})])

    def test_positional_tools_are_detected(self):
        with (
            patch.dict(
                sys.modules, {"openviking.models.vlm.base": _fake_base_module()}
            ),
            redirect_stderr(io.StringIO()),
        ):
            resp = _run(self.wrapped(self.vlm, "p", None, [UPDATE_TOOL], CHOICE))
        self.assertEqual(self.calls, [])
        self.assertFalse(resp.has_tool_calls)

    def test_original_errors_propagate(self):
        async def fail(self_, prompt="", *args, **kwargs):
            raise RuntimeError("upstream down")

        with self.assertRaisesRegex(RuntimeError, "upstream down"):
            _run(wm_patch.skipping_wm_update(fail)(self.vlm, "p"))


class GuardTests(unittest.TestCase):
    def _module(self):
        async def original(self, prompt="", *args, **kwargs):
            return "text"

        cls = type("OpenAIVLM", (), {"get_completion_async": original})
        digest = wm_patch.hashlib.sha256(
            wm_patch.inspect.getsource(original).encode()
        ).hexdigest()
        return cls, types.SimpleNamespace(OpenAIVLM=cls), digest

    def test_guards_and_idempotence(self):
        cls, module, digest = self._module()
        ov = types.SimpleNamespace(__version__="v0.4.99")
        with (
            patch.dict(sys.modules, {"openviking": ov}),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertFalse(wm_patch.apply(module))
            ov.__version__ = wm_patch.EXPECTED_VERSION
            self.assertFalse(wm_patch.apply(module))
            with patch.object(wm_patch, "EXPECTED_SHA256", digest):
                with patch.dict(os.environ, {"OV_WM_UPDATE_SKIP": "0"}):
                    self.assertFalse(wm_patch.apply(module))
                with patch.dict(os.environ, {"OV_WM_UPDATE_MODE": "off"}):
                    self.assertFalse(wm_patch.apply(module))
                with patch.dict(os.environ, {"OV_WM_UPDATE_MODE": "bogus"}):
                    self.assertFalse(wm_patch.apply(module))
                self.assertTrue(wm_patch.apply(module))
                installed = cls.get_completion_async
                self.assertEqual(installed._ov_wm_update_patch, "schema")
                self.assertTrue(wm_patch.apply(module))
                self.assertIs(cls.get_completion_async, installed)
                with patch.dict(os.environ, {"OV_WM_UPDATE_MODE": "skip"}):
                    self.assertTrue(
                        wm_patch.apply(module)
                    )  # already patched: no re-wrap
                self.assertIs(cls.get_completion_async, installed)

    def test_skip_mode_installs_the_skip_wrapper(self):
        cls, module, digest = self._module()
        ov = types.SimpleNamespace(__version__=wm_patch.EXPECTED_VERSION)
        with (
            patch.dict(sys.modules, {"openviking": ov}),
            patch.object(wm_patch, "EXPECTED_SHA256", digest),
        ):
            for env in ({"OV_WM_UPDATE_MODE": "skip"}, {"OV_WM_UPDATE_SKIP": "1"}):
                cls, module, _ = self._module()
                with patch.dict(os.environ, env, clear=True):
                    self.assertTrue(wm_patch.apply(module))
                self.assertEqual(cls.get_completion_async._ov_wm_update_patch, "skip")

    def test_missing_class_is_not_applied(self):
        ov = types.SimpleNamespace(__version__=wm_patch.EXPECTED_VERSION)
        with (
            patch.dict(sys.modules, {"openviking": ov}),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertFalse(wm_patch.apply(types.SimpleNamespace()))


if __name__ == "__main__":
    unittest.main()
