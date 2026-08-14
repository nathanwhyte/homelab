#!/usr/bin/env python3
"""Fixed-prompt coherence gate for a model on a given Ollama backend.

`concurrency-bench.py` measures throughput and counts empty/truncated/error
replies, but it discards the response text during aggregation — so a backend
that emits fluent-looking garbage scores identically to one that emits correct
answers. That gap matters on Vulkan, where llama.cpp's operation-support table
still marks `SSM_SCAN` as only partially supported, and the failure mode for a
hybrid-SSM model like nemotron-3-nano is garbled output rather than an error.

This script closes the gap: it sends a small fixed prompt set, applies
deterministic checks to the answers, and writes the complete transcript
(prompt, answer, thinking text, request parameters, server timings) to JSON so
the evidence survives the run. Exit status is 0 only when every probe passes,
so it can gate a throughput run.

Stdlib only — it is meant to run from a laptop against a port-forwarded
service, with no virtualenv to set up.

    python3 coherence-smoke.py --model nemotron-3-nano:4b-bf16 \\
        --url http://127.0.0.1:11434 --out results/coherence.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

DAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


def _check_fizzbuzz(text: str) -> Optional[str]:
    lowered = text.lower()
    for token in ("fizzbuzz", "fizz", "buzz"):
        if token not in lowered:
            return f"missing {token!r}"
    # 15 is the only FizzBuzz line in 1..15; 3/5 must be classified correctly.
    if not re.search(r"\b1\b", text) or not re.search(r"\b14\b", text):
        return "sequence does not span 1..15"
    return None


def _check_product(text: str) -> Optional[str]:
    # 17 * 23 = 391. Accept it anywhere in the answer, with or without commas.
    if "391" in text.replace(",", ""):
        return None
    return "does not contain 391"


def _check_weekdays(text: str) -> Optional[str]:
    lowered = text.lower()
    positions = []
    for day in DAYS:
        idx = lowered.find(day)
        if idx < 0:
            return f"missing {day}"
        positions.append(idx)
    if positions != sorted(positions):
        return "days are not in order"
    return None


def _check_prose(text: str) -> Optional[str]:
    """Heuristics for fluent-looking garbage in a free-form paragraph."""
    stripped = text.strip()
    if len(stripped) < 80:
        return f"too short ({len(stripped)} chars) to judge coherence"
    printable = sum(1 for ch in stripped if ch.isprintable() or ch in "\n\t")
    if printable / len(stripped) < 0.98:
        return "contains non-printable characters"
    words = re.findall(r"[A-Za-z']+", stripped)
    if len(words) < 20:
        return f"only {len(words)} words"
    unique_ratio = len(set(w.lower() for w in words)) / len(words)
    if unique_ratio < 0.35:
        return f"low lexical diversity ({unique_ratio:.2f}) — likely looping"
    if re.search(r"(.)\1{9,}", stripped):
        return "contains a run of 10+ identical characters"
    # A repeated n-gram filling the reply is the classic degenerate-decode shape.
    for size in (4, 6):
        grams = [
            " ".join(words[i : i + size]).lower()
            for i in range(0, max(0, len(words) - size))
        ]
        if grams:
            most = max(set(grams), key=grams.count)
            if grams.count(most) > max(3, len(grams) * 0.2):
                return f"repeats the phrase {most!r} {grams.count(most)} times"
    if "hash" not in stripped.lower():
        return "answer does not mention the subject of the question"
    return None


@dataclass
class Probe:
    name: str
    prompt: str
    check: Callable[[str], Optional[str]]
    num_predict: int = 512


PROBES: list[Probe] = [
    Probe(
        name="fizzbuzz",
        prompt=(
            "Print the numbers 1 to 15, one per line. Replace multiples of 3 "
            "with Fizz, multiples of 5 with Buzz, and multiples of both with "
            "FizzBuzz. Output only the list."
        ),
        check=_check_fizzbuzz,
    ),
    Probe(
        name="arithmetic",
        prompt="What is 17 multiplied by 23? Reply with only the number.",
        check=_check_product,
        num_predict=128,
    ),
    Probe(
        name="ordered-recall",
        prompt=(
            "List the seven days of the week in order, comma-separated, with "
            "no other text."
        ),
        check=_check_weekdays,
        num_predict=128,
    ),
    Probe(
        name="prose",
        prompt=(
            "In one paragraph of three or four sentences, explain what a hash "
            "table is and why lookups are fast."
        ),
        check=_check_prose,
    ),
]


@dataclass
class ProbeResult:
    name: str
    think: Any
    request: dict
    response: str = ""
    thinking: Optional[str] = None
    done_reason: Optional[str] = None
    server: dict = field(default_factory=dict)
    wall_s: float = 0.0
    failure: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.failure is None


def call_generate(url: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def run_probe(
    url: str,
    model: str,
    probe: Probe,
    think: Any,
    num_ctx: int,
    temperature: float,
    top_p: float,
    timeout: float,
) -> ProbeResult:
    payload: dict[str, Any] = {
        "model": model,
        "prompt": probe.prompt,
        # Unary, not streamed: this gate cares about the finished answer, and a
        # single response body keeps the saved transcript byte-exact.
        "stream": False,
        "options": {
            "num_ctx": num_ctx,
            "num_predict": probe.num_predict,
            "temperature": temperature,
            "top_p": top_p,
        },
    }
    if think is not None:
        payload["think"] = think

    result = ProbeResult(name=probe.name, think=think, request=payload)
    started = time.monotonic()
    try:
        data = call_generate(url, payload, timeout)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        result.wall_s = time.monotonic() - started
        result.failure = f"request failed: {exc}"
        return result
    result.wall_s = time.monotonic() - started

    if data.get("error"):
        result.failure = f"server error: {data['error']}"
        return result

    result.response = data.get("response", "") or ""
    result.thinking = data.get("thinking") or None
    result.done_reason = data.get("done_reason")
    result.server = {
        key: data.get(key)
        for key in (
            "total_duration",
            "load_duration",
            "prompt_eval_count",
            "prompt_eval_duration",
            "eval_count",
            "eval_duration",
        )
    }

    if not result.response.strip():
        # A reasoning model that burns its whole budget thinking lands here.
        result.failure = "empty answer"
        return result
    result.failure = probe.check(result.response)
    return result


def parse_think(value: str) -> Any:
    lowered = value.lower()
    if lowered in ("true", "on", "yes"):
        return True
    if lowered in ("false", "off", "no"):
        return False
    if lowered in ("none", "unset", "default"):
        return None
    # Ollama also accepts a level string.
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fixed-prompt coherence gate for an Ollama-served model.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--model", required=True, help="Ollama model tag")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:11434",
        help="Ollama base URL (port-forward the cluster service to use this)",
    )
    parser.add_argument(
        "--think",
        action="append",
        default=None,
        metavar="MODE",
        help=(
            "Thinking mode to probe: true/false/none or a level string. "
            "Repeat to probe several; defaults to both false and true."
        ),
    )
    parser.add_argument("--num-ctx", type=int, default=16384)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Per-request timeout in seconds (first call includes model load)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Path for the JSON transcript (parent directories are created)",
    )
    args = parser.parse_args()

    think_modes = [parse_think(t) for t in args.think] if args.think else [False, True]

    results: list[ProbeResult] = []
    for think in think_modes:
        for probe in PROBES:
            res = run_probe(
                args.url,
                args.model,
                probe,
                think,
                args.num_ctx,
                args.temperature,
                args.top_p,
                args.timeout,
            )
            results.append(res)
            status = "PASS" if res.passed else f"FAIL ({res.failure})"
            print(
                f"[coherence] think={think!r} {probe.name}: {status} "
                f"({res.wall_s:.1f}s)",
                flush=True,
            )

    passed = sum(1 for r in results if r.passed)
    payload = {
        "model": args.model,
        "url": args.url,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sampling": {
            "num_ctx": args.num_ctx,
            "temperature": args.temperature,
            "top_p": args.top_p,
        },
        "think_modes": think_modes,
        "summary": {"total": len(results), "passed": passed},
        # Full transcripts — the whole point of this gate is that the text is
        # preserved rather than reduced to a counter.
        "probes": [
            {
                "name": r.name,
                "think": r.think,
                "passed": r.passed,
                "failure": r.failure,
                "request": r.request,
                "response": r.response,
                "thinking": r.thinking,
                "done_reason": r.done_reason,
                "server": r.server,
                "wall_s": round(r.wall_s, 3),
            }
            for r in results
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"[coherence] transcript written to {args.out}")

    if passed != len(results):
        print(
            f"[coherence] GATE FAILED — {len(results) - passed}/{len(results)} "
            "probes did not pass; do not start the throughput run",
            file=sys.stderr,
        )
        return 1
    print(f"[coherence] GATE PASSED — {passed}/{passed} probes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
