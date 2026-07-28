"""Payload- and result-level tests for concurrency-bench.

These cover the request serialization and response-classification contracts.
`--dry-run` exits before a request is ever constructed, so it cannot verify
either; these tests drive run_request against a stub session instead.

Run: uv run --with aiohttp --with pytest pytest benchmarks/ollama/tools/
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent / "concurrency-bench.py"
_spec = importlib.util.spec_from_file_location("concurrency_bench", _MODULE_PATH)
assert _spec and _spec.loader
bench = importlib.util.module_from_spec(_spec)
# Must be registered before exec_module: @dataclass resolves annotations via
# sys.modules[cls.__module__], which is None for an unregistered module.
sys.modules["concurrency_bench"] = bench
_spec.loader.exec_module(bench)


class _StubResponse:
    """Minimal stand-in for an aiohttp streaming response."""

    def __init__(self, status: int, chunks: list[dict], text: str = ""):
        self.status = status
        self._chunks = chunks
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def text(self):
        return self._text

    @property
    def content(self):
        async def _gen():
            for c in self._chunks:
                yield (json.dumps(c) + "\n").encode()

        return _gen()


class _StubSession:
    """Captures the outgoing payload and replays a canned response."""

    def __init__(self, status: int = 200, chunks: list[dict] | None = None, text=""):
        self.payloads: list[dict] = []
        self._status = status
        self._chunks = chunks if chunks is not None else []
        self._text = text

    def post(self, url, json=None, timeout=None):
        self.payloads.append(json)
        return _StubResponse(self._status, self._chunks, self._text)


def _done_chunk(**over):
    base = {
        "response": "",
        "done": True,
        "done_reason": "stop",
        "load_duration": 1_000_000,
        "prompt_eval_duration": 2_000_000,
        "eval_duration": 1_000_000_000,
        "eval_count": 10,
        "prompt_eval_count": 5,
    }
    base.update(over)
    return base


def _run(session, **kw):
    args = {
        "url": "http://stub",
        "model": "m",
        "prompt": "p",
        "num_ctx": 128,
        "num_predict": 16,
        "temperature": 0.3,
    }
    args.update(kw)
    return asyncio.run(bench.run_request(session, **args))


# --- think serialization -------------------------------------------------


def test_think_omitted_when_none():
    """An unset think must not appear in the payload at all."""
    s = _StubSession(chunks=[_done_chunk(response="hi")])
    _run(s)
    assert "think" not in s.payloads[0]


@pytest.mark.parametrize("value", [True, False, "low", "high"])
def test_think_serialized_when_set(value):
    """Ollama accepts a bool or a level string; both must pass through."""
    s = _StubSession(chunks=[_done_chunk(response="hi")])
    _run(s, think=value)
    assert s.payloads[0]["think"] is value or s.payloads[0]["think"] == value


def test_stream_is_enabled():
    """TTFT is client-observed, which requires a streamed response."""
    s = _StubSession(chunks=[_done_chunk(response="hi")])
    _run(s)
    assert s.payloads[0]["stream"] is True


# --- error handling ------------------------------------------------------


def test_non_200_is_an_error():
    """A rejected request must not score as a zero-metric success."""
    s = _StubSession(status=400, text='{"error":"bad request"}')
    r = _run(s)
    assert r.error is not None
    assert "400" in r.error
    assert r.tokens == 0


def test_error_field_in_200_body_is_an_error():
    """Ollama also reports failure via an `error` key on a 200."""
    s = _StubSession(chunks=[{"error": "model not found"}])
    r = _run(s)
    assert r.error is not None
    assert "model not found" in r.error


# --- reasoning-response classification -----------------------------------


def test_thinking_only_response_is_flagged_empty():
    """All budget spent reasoning => tokens counted, but answer is empty.

    This is the defect that made a laguna row indistinguishable from a real
    answer of the same token count.
    """
    s = _StubSession(
        chunks=[
            {"thinking": "let me think...", "response": "", "done": False},
            _done_chunk(response="", done_reason="length", eval_count=200),
        ]
    )
    r = _run(s)
    assert r.error is None
    assert r.tokens == 200
    assert r.response_text == ""
    assert r.thinking_text == "let me think..."
    assert r.done_reason == "length"
    assert r.empty_answer is True
    assert r.truncated is True


def test_real_answer_is_not_flagged_empty():
    s = _StubSession(
        chunks=[
            {"thinking": "reasoning", "response": "", "done": False},
            {"response": "the answer", "done": False},
            _done_chunk(response="", done_reason="stop", eval_count=42),
        ]
    )
    r = _run(s)
    assert r.response_text == "the answer"
    assert r.thinking_text == "reasoning"
    assert r.empty_answer is False
    assert r.truncated is False


# --- level accounting ----------------------------------------------------


def test_premature_eof_is_an_error():
    """A stream that never sends `done` was cut short.

    Every duration/count field lives on the final chunk, so without it the
    result is all zeros — which previously read as a clean success.
    """
    s = _StubSession(
        chunks=[
            {"response": "partial answ", "done": False},
        ]
    )
    r = _run(s)
    assert r.error is not None
    assert "done chunk" in r.error


def test_malformed_frame_is_an_error():
    """An unparseable frame means the stream isn't what the protocol promises."""

    class _BadResponse(_StubResponse):
        @property
        def content(self):
            async def _gen():
                yield b'{"response": "ok", "done": false}\n'
                yield b"{not json at all\n"

            return _gen()

    class _BadSession(_StubSession):
        def post(self, url, json=None, timeout=None):
            self.payloads.append(json)
            return _BadResponse(200, [])

    r = _run(_BadSession())
    assert r.error is not None
    assert "malformed" in r.error


# --- latency semantics ---------------------------------------------------


def test_ttft_and_ttfa_differ_for_a_thinking_model():
    """Reasoning streams before content, so first-output != first-answer.

    Collapsing them made think-on TTFT look near-zero and rendered think
    on/off latency comparisons meaningless.
    """
    s = _StubSession(
        chunks=[
            {"thinking": "reasoning first", "done": False},
            {"response": "the answer", "done": False},
            _done_chunk(response="", done_reason="stop", eval_count=20),
        ]
    )
    r = _run(s)
    assert r.ttft_s <= r.ttfa_s, "first answer cannot precede first output"
    assert r.ttfa_s > 0


def test_empty_answer_ignores_token_count():
    """Whitespace-only answers are unusable even with eval_count == 0."""
    s = _StubSession(chunks=[_done_chunk(response="   ", eval_count=0)])
    r = _run(s)
    assert r.error is None
    assert r.tokens == 0
    assert r.empty_answer is True


# --- level accounting ----------------------------------------------------


def test_failed_requests_are_counted_not_silently_dropped():
    lr = bench.LevelResult(concurrency=2, requests=2, repeats=1)
    lr.attempted = 2
    lr.errors.append("HTTP 500")
    lr.tokens.append(10)
    q = lr._quality_summary()
    assert q["attempted"] == 2
    assert q["failed"] == 1
    assert q["succeeded"] == 1


def test_usable_rate_excludes_empty_answers():
    lr = bench.LevelResult(concurrency=1, requests=2, repeats=1)
    lr.attempted = 2
    lr.tokens.extend([100, 100])
    lr.empty_answers = 1
    q = lr._quality_summary()
    assert q["usable"] == 1
    assert q["usable_rate"] == 0.5
