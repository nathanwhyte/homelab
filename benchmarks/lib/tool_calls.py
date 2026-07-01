"""Tool-calling prompt schemas and response validators.

Used by the benchmark harness to measure how reliably a model emits
valid, well-formed tool calls when asked. The validator is intentionally
strict: the response must be a single JSON object with a known tool name
and all required arguments present.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional


TOOLS = {
    "get_weather": {
        "description": "Get current weather for a location.",
        "args": {
            "location": {"type": "str", "required": True},
            "unit": {"type": "str", "required": True},
        },
    },
    "search_calendar": {
        "description": "Search calendar events by query and optional date range.",
        "args": {
            "query": {"type": "str", "required": True},
            "date_range": {"type": "str", "required": True},
        },
    },
    "send_email": {
        "description": "Send an email to a recipient.",
        "args": {
            "to": {"type": "str", "required": True},
            "subject": {"type": "str", "required": True},
            "body": {"type": "str", "required": True},
        },
    },
    "create_reminder": {
        "description": "Create a reminder with a title and datetime.",
        "args": {
            "title": {"type": "str", "required": True},
            "datetime": {"type": "str", "required": True},
        },
    },
    "calculate": {
        "description": "Evaluate a mathematical expression and return the result.",
        "args": {
            "expression": {"type": "str", "required": True},
        },
    },
}


TOOL_INSTRUCTION = """You are a helpful assistant with access to the following tools:

- get_weather: Get current weather for a location. Args: location (str), unit (str, "c" or "f").
- search_calendar: Search calendar events. Args: query (str), date_range (str).
- send_email: Send an email. Args: to (str), subject (str), body (str).
- create_reminder: Create a reminder. Args: title (str), datetime (str).
- calculate: Evaluate a math expression. Args: expression (str).

Respond with ONLY a single JSON object in this exact format:
{"tool": "<tool_name>", "arguments": {<args>}}

Do not include markdown, explanations, or any text outside the JSON object."""


TOOL_CALLING_PROMPTS = [
    (
        "get_weather",
        "What's the weather like in Austin, Texas? Give me the temperature in celsius.",
    ),
    ("search_calendar", "Find my meetings about the budget review next week."),
    ("send_email", "Email sarah@example.com to say the deployment is complete."),
    ("create_reminder", "Remind me to call Mom tomorrow at 6pm."),
    ("calculate", "What is 145 times 37 plus 82?"),
    (
        "get_weather",
        "Will it rain in Seattle today? Show the temperature in fahrenheit.",
    ),
    ("search_calendar", "When is my dentist appointment?"),
    (
        "send_email",
        "Send a quick email to team@example.com with the subject 'Standup notes' and body 'Notes are in the shared doc.'",
    ),
]


@dataclass
class ValidationResult:
    valid_json: bool
    has_tool_field: bool
    has_arguments_field: bool
    tool_recognized: bool
    required_args_present: bool
    arg_types_valid: bool
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid_json": self.valid_json,
            "has_tool_field": self.has_tool_field,
            "has_arguments_field": self.has_arguments_field,
            "tool_recognized": self.tool_recognized,
            "required_args_present": self.required_args_present,
            "arg_types_valid": self.arg_types_valid,
            "error": self.error,
        }


def _strip_markdown(text: str) -> str:
    """Remove markdown code fences if present."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop first fence and optional language line
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def validate_tool_call(
    response: str, expected_tool: Optional[str] = None
) -> ValidationResult:
    """Validate a single tool-calling response.

    If expected_tool is provided, tool_recognized also checks that the
    emitted tool name matches.
    """
    cleaned = _strip_markdown(response)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return ValidationResult(
            valid_json=False,
            has_tool_field=False,
            has_arguments_field=False,
            tool_recognized=False,
            required_args_present=False,
            arg_types_valid=False,
            error=f"json_decode_error: {exc}",
        )

    if not isinstance(parsed, dict):
        return ValidationResult(
            valid_json=True,
            has_tool_field=False,
            has_arguments_field=False,
            tool_recognized=False,
            required_args_present=False,
            arg_types_valid=False,
            error="parsed_json_not_object",
        )

    has_tool_field = "tool" in parsed and isinstance(parsed["tool"], str)
    has_arguments_field = "arguments" in parsed and isinstance(
        parsed.get("arguments"), dict
    )

    if not has_tool_field:
        return ValidationResult(
            valid_json=True,
            has_tool_field=False,
            has_arguments_field=has_arguments_field,
            tool_recognized=False,
            required_args_present=False,
            arg_types_valid=False,
            error="missing_tool_field",
        )

    tool_name = parsed["tool"]
    tool_recognized = tool_name in TOOLS
    if expected_tool and tool_recognized:
        tool_recognized = tool_name == expected_tool

    if not has_arguments_field:
        return ValidationResult(
            valid_json=True,
            has_tool_field=True,
            has_arguments_field=False,
            tool_recognized=tool_recognized,
            required_args_present=False,
            arg_types_valid=False,
            error="missing_arguments_field",
        )

    args = parsed["arguments"]
    schema = TOOLS.get(tool_name, {"args": {}})
    required = {k for k, v in schema["args"].items() if v.get("required")}
    required_args_present = required.issubset(args.keys())

    arg_types_valid = True
    type_errors: list[str] = []
    for key, spec in schema["args"].items():
        if key in args:
            expected_type = spec.get("type", "str")
            value = args[key]
            if expected_type == "str" and not isinstance(value, str):
                arg_types_valid = False
                type_errors.append(f"{key} should be str, got {type(value).__name__}")
            elif expected_type == "number" and not isinstance(value, (int, float)):
                arg_types_valid = False
                type_errors.append(
                    f"{key} should be number, got {type(value).__name__}"
                )

    error = None
    if not tool_recognized:
        error = f"unrecognized_tool: {tool_name}"
    elif not required_args_present:
        missing = sorted(required - set(args.keys()))
        error = f"missing_required_args: {missing}"
    elif not arg_types_valid:
        error = f"arg_type_errors: {type_errors}"

    return ValidationResult(
        valid_json=True,
        has_tool_field=True,
        has_arguments_field=True,
        tool_recognized=tool_recognized,
        required_args_present=required_args_present,
        arg_types_valid=arg_types_valid,
        error=error,
    )


def tool_calling_prompts(count: int) -> tuple[list[str], list[Optional[str]]]:
    """Return tool-calling user prompts and expected tool names.

    The returned prompts include the system/tool instruction prefix so the
    model knows the required output format. expected_tools is a parallel
    list used by the validator to check the correct tool was selected.
    """
    prompts: list[str] = []
    expected_tools: list[Optional[str]] = []
    i = 0
    while len(prompts) < count:
        tool, user_prompt = TOOL_CALLING_PROMPTS[i % len(TOOL_CALLING_PROMPTS)]
        full_prompt = f"{TOOL_INSTRUCTION}\n\nUser: {user_prompt}\n\nAssistant:"
        prompts.append(full_prompt)
        expected_tools.append(tool)
        i += 1
    return prompts, expected_tools
