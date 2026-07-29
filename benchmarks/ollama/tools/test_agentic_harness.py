#!/usr/bin/env python3
"""Unit tests for the agentic-coding harness itself, using scripted responses.

`test_coding_tasks.py` proves the FIXTURES are sound. This proves the HARNESS is,
without a model or a GPU: `post_chat` is replaced with a queue of canned
responses, so every branch that a live run would exercise only by luck — a
malformed tool call, a reserved write, a dead server, a verifier that exits early
— is covered deterministically in about a second.

Every test here corresponds to a defect that was actually present at some point:

  * assistant `thinking` was dropped when rebuilding history between turns
  * a rejected write to a reserved name still counted as a write
  * `../../stats.py` silently collapsed to `stats.py` and "succeeded"
  * a totally unreachable server produced a result file and exit code 0
  * `raise SystemExit(0)` in the file under test scored every task as solved

Run: python3 test_agentic_harness.py
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from coding_tasks import CodingTask


def _load_bench():
    path = Path(__file__).resolve().parent / "agentic-coding-bench.py"
    spec = importlib.util.spec_from_file_location("agentic_coding_bench", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["agentic_coding_bench"] = module
    spec.loader.exec_module(module)
    return module


BENCH = _load_bench()

TRIVIAL_TASK = CodingTask(
    task_id="unit-task",
    tier=1,
    title="unit test fixture",
    prompt="Fix it.",
    files={"target.py": "VALUE = 1\n"},
    verifier="from target import VALUE\nassert VALUE == 2, VALUE\nprint('OK')\n",
    target_files=["target.py"],
    max_turns=4,
)

FIXED_SOURCE = "VALUE = 2\n"


def _tool_call(name: str, arguments: Any) -> dict[str, Any]:
    return {"function": {"name": name, "arguments": arguments}}


def _response(
    *,
    content: str = "",
    calls: list[dict[str, Any]] | None = None,
    thinking: str | None = None,
    done_reason: str = "stop",
) -> tuple[int, dict[str, Any]]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if thinking is not None:
        message["thinking"] = thinking
    if calls:
        message["tool_calls"] = calls
    return 200, {
        "message": message,
        "done_reason": done_reason,
        "prompt_eval_count": 10,
        "eval_count": 5,
    }


class ScriptedServer:
    """Replaces post_chat with a fixed sequence, recording what it was sent."""

    def __init__(self, responses: list[tuple[int, dict[str, Any]]]):
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, base: str, payload: dict[str, Any], timeout: int):
        self.requests.append(payload)
        if not self.responses:
            return _response(content="done")
        return self.responses.pop(0)


def run_scripted(
    responses: list[tuple[int, dict[str, Any]]],
    task: CodingTask = TRIVIAL_TASK,
) -> tuple[Any, ScriptedServer]:
    server = ScriptedServer(responses)
    original = BENCH.post_chat
    BENCH.post_chat = server
    try:
        result = BENCH.run_task(
            "http://scripted",
            "unit:model",
            task,
            None,
            4096,
            30,
            None,
            42,
            1024,
        )
    finally:
        BENCH.post_chat = original
    return result, server


CHECKS: list[tuple[str, Any]] = []


def check(name):
    def register(fn):
        CHECKS.append((name, fn))
        return fn

    return register


@check("assistant thinking survives into the next turn")
def test_thinking_preserved():
    result, server = run_scripted(
        [
            _response(
                thinking="deliberating",
                calls=[_tool_call("read_file", {"path": "target.py"})],
            ),
            _response(content="finished"),
        ]
    )
    second = server.requests[1]["messages"]
    assistant = [m for m in second if m.get("role") == "assistant"]
    assert assistant, "assistant turn was not appended to history"
    assert assistant[0].get("thinking") == "deliberating", (
        f"thinking dropped between turns: {assistant[0]!r}"
    )
    assert result.tool_calls == 1


@check("malformed tool calls are counted, not executed")
def test_malformed_calls_counted():
    result, _ = run_scripted(
        [
            _response(calls=[_tool_call("no_such_tool", {})]),
            _response(calls=[_tool_call("read_file", {})]),
            _response(
                calls=[_tool_call("write_file", {"path": "target.py", "content": 5})]
            ),
            _response(content="giving up"),
        ]
    )
    assert result.tool_calls == 3, result.tool_calls
    assert result.bad_tool_calls == 3, result.bad_tool_detail
    assert result.wrote_any_file is False


@check("a rejected reserved write is not recorded as a write")
def test_reserved_write_not_counted():
    result, _ = run_scripted(
        [
            _response(
                calls=[_tool_call("write_file", {"path": "verify.py", "content": "x"})]
            ),
            _response(content="done"),
        ]
    )
    assert result.wrote_any_file is False, "refused write counted as a write"
    assert result.files_written == [], result.files_written


@check("directory components are rejected rather than collapsed")
def test_path_traversal_rejected():
    result, _ = run_scripted(
        [
            _response(
                calls=[
                    _tool_call(
                        "write_file",
                        {"path": "../../target.py", "content": FIXED_SOURCE},
                    )
                ]
            ),
            _response(content="done"),
        ]
    )
    assert result.wrote_any_file is False, "../../target.py silently became target.py"
    assert result.solved is False


@check("a solved task is scored from the verifier, and reads are tracked")
def test_solved_and_read_tracking():
    result, _ = run_scripted(
        [
            _response(calls=[_tool_call("read_file", {"path": "target.py"})]),
            _response(
                calls=[
                    _tool_call(
                        "write_file", {"path": "target.py", "content": FIXED_SOURCE}
                    )
                ]
            ),
            _response(content="fixed"),
        ]
    )
    assert result.solved is True, result.verifier_output
    assert result.files_read == ["target.py"], result.files_read
    assert result.write_without_read == 0, result.write_without_read


@check("writing a file that was never read is flagged")
def test_write_without_read_flagged():
    result, _ = run_scripted(
        [
            _response(
                calls=[
                    _tool_call(
                        "write_file", {"path": "target.py", "content": FIXED_SOURCE}
                    )
                ]
            ),
            _response(content="fixed blind"),
        ]
    )
    assert result.solved is True
    assert result.write_without_read == 1, result.write_without_read


@check("SystemExit(0) in the target does not score as solved")
def test_early_exit_bypass():
    result, _ = run_scripted(
        [
            _response(
                calls=[
                    _tool_call(
                        "write_file",
                        {"path": "target.py", "content": "raise SystemExit(0)\n"},
                    )
                ]
            ),
            _response(content="cheated"),
        ]
    )
    assert result.solved is False, "verifier early-exit scored as a pass"
    assert "without completing" in result.verifier_output


@check("truncated turns are recorded")
def test_truncation_recorded():
    result, _ = run_scripted([_response(content="x" * 100, done_reason="length")])
    assert result.truncated_turns == 1, result.truncated_turns


@check("transport failure is recorded as an error, not a score")
def test_transport_failure():
    server = ScriptedServer([(0, {"error": "ConnectionRefusedError"})])
    original = BENCH.post_chat
    BENCH.post_chat = server
    try:
        result = BENCH.run_task(
            "http://scripted",
            "unit:model",
            TRIVIAL_TASK,
            None,
            4096,
            30,
            None,
            42,
            1024,
        )
    finally:
        BENCH.post_chat = original
    assert result.error is not None, "transport failure left error unset"
    assert result.solved is False
    assert result.tool_calls == 0


@check("preflight rejects an unreachable server")
def test_preflight_unreachable():
    problems = BENCH.preflight("http://127.0.0.1:9", "unit:model")
    assert problems, "preflight passed against a refused port"
    assert "unreachable" in problems[0]


@check("the turn cap is reported")
def test_turn_cap():
    responses = [
        _response(calls=[_tool_call("read_file", {"path": "target.py"})])
        for _ in range(TRIVIAL_TASK.max_turns)
    ]
    result, _ = run_scripted(responses)
    assert result.hit_turn_cap is True
    assert result.turns == TRIVIAL_TASK.max_turns


@check("run_tests only runs fixture-shipped suites")
def test_run_tests_scoped_to_fixture():
    sandbox = Path(tempfile.mkdtemp(prefix="unit-tools-"))
    try:
        (sandbox / "test_real.py").write_text("print('fixture suite ran')\n")
        (sandbox / "test_invented.py").write_text("print('MODEL SUITE RAN')\n")
        out = BENCH.execute_tool(sandbox, "run_tests", {}, ["test_real.py"])
        assert "fixture suite ran" in out, out
        assert "MODEL SUITE RAN" not in out, "a model-authored suite was executed"
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


@check("timeouts kill the whole process group")
def test_timeout_kills_group():
    sandbox = Path(tempfile.mkdtemp(prefix="unit-timeout-"))
    try:
        (sandbox / "spin.py").write_text(
            "import time\nwhile True:\n    time.sleep(0.1)\n"
        )
        code, out = BENCH.run_python(sandbox, "spin.py", timeout=3)
        assert code == 124, (code, out)
        assert "timeout" in out
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def main() -> int:
    failures = 0
    for name, fn in CHECKS:
        try:
            fn()
        except AssertionError as e:
            print(f"FAIL  {name}\n      {e}")
            failures += 1
        except Exception as e:  # noqa: BLE001 - a crashing test is a failing test
            print(f"ERROR {name}\n      {type(e).__name__}: {e}")
            failures += 1
        else:
            print(f"ok    {name}")
    print(f"\n{len(CHECKS)} checks, {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
