#!/usr/bin/env python3
"""Probe for BUG-1067: qwen3.6 MLX +1-space indentation in tool-call file content.

Sends a single write_file-style tool-call task to an Ollama tag N times and
counts lines whose leading-space indent is not a multiple of 4. On the
2026-07-29 matrix (Ollama 0.32.5, qwen3.6:coding nvfp4 MLX), corrupted lines
were always exactly +1 space (5-vs-4, 9-vs-8), concentrated on docstring
lines; the GGUF twin produced zero such lines.

⚠ SCOPE — this is an UNVALIDATED single-turn reduction, not the known repro.
The reliable matrix failure is a multi-turn (read → edit → test) conversation
over the exact t1a fixture at num_ctx 32768, and the corruption is
context-conditional. If this probe does not reproduce, fall back to
`agentic-coding-bench.py --model qwen3.6:coding --tiers 1 --repeats 3`.
The `--plain` variant changes prompt, rendered template, and output protocol
simultaneously, so tool-dirty/plain-clean is suggestive only — real
localization needs the raw pre-parser stream compared against the parsed
tool_calls arguments (see BUG-1067 fix plan step 2).

GEMMA ARM — tokenizer-confound control. In the 2026-07-29 matrix both gemma4
MLX-path tags were whitespace-clean while both non-gemma MLX tags (qwen,
laguna) corrupted indentation. Two explanations fit: (a) Ollama's MLX path is
simply better exercised for gemma (MTP work, blog examples), or (b) the
defect lives in BPE space-prefix (`Ġ`) detokenization — Qwen-style — which
gemma's SentencePiece-style whitespace encoding never exercises (mlx-lm#1041
is literally about `Ġ` tokens). The gemma arm runs the SAME prompt/protocol/
engine with only the tokenizer family changed:
  * gemma tool-call DIRTY  -> engine-wide defect the multi-turn matrix rows
    masked; refutes both (a) and (b) as stated — big news, re-check matrix.
  * gemma clean + qwen dirty -> the tool-call protocol/parser is NOT mangling
    whitespace model-agnostically, which weakens the parser suspect and
    strengthens token-level detokenization (suspect order in BUG-1067).
    Does not by itself split (a) from (b) — that needs step-2 raw-stream
    evidence — but it pins the defect to token-stream content.

Usage:
    ./mlx-indent-repro.py --model qwen3.6:coding --trials 5 --save-dir /tmp/bug1067
    ./mlx-indent-repro.py --model qwen3.6:coding-gguf --trials 5   # engine control
    ./mlx-indent-repro.py --model gemma4:12b-mlx --trials 5        # tokenizer control
    # each also with --plain; gemma4:12b (GGUF Q4_K_M) closes the square

Exit codes: 0 = all trials valid; 1 = fatal error; 2 = some trials malformed
or failed (inconclusive — do not read the summary as a clean result).

⚠ One local-model consumer at a time: do not run while a matrix run or any
other Ollama workload is in flight.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROMPT = (
    "Create a file stats.py containing exactly one function, moving_average"
    "(values, window), that returns the list of moving averages of `values` "
    "over `window` samples. Give the function a multi-line docstring (summary "
    "line, blank line, two lines of detail) indented with 4 spaces, and use "
    "4-space indentation throughout. "
)

TOOL_SUFFIX = "Write the file with a single write_file call."
PLAIN_SUFFIX = (
    "Reply with the complete file contents in a single fenced python code "
    "block and no tool calls."
)

WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Replace a file's entire contents with new text.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repository-relative filename",
                },
                "content": {
                    "type": "string",
                    "description": "The complete new file contents",
                },
            },
            "required": ["path", "content"],
        },
    },
}


def get_json(url: str, payload: dict | None = None, timeout: int = 600) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"} if data else {}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def odd_indent_lines(text: str) -> list[tuple[int, int, str]]:
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent % 4 != 0:
            out.append((i, indent, line))
    return out


def extract_plain(content: str) -> str:
    if "```" not in content:
        return content
    body = content.split("```", 2)[1]
    return body.split("\n", 1)[1] if "\n" in body else body


def extract_tool_content(message: dict) -> tuple[str | None, str]:
    """Return (content, problem). content is None when the trial is malformed."""
    calls = message.get("tool_calls") or []
    writes = [
        c["function"]["arguments"]
        for c in calls
        if c.get("function", {}).get("name") == "write_file"
    ]
    if not writes:
        return (
            None,
            f"no write_file call ({len(calls)} tool calls, content len={len(message.get('content') or '')})",
        )
    arguments = writes[0]
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as e:
            return None, f"arguments not valid JSON: {e}"
    if not isinstance(arguments, dict) or "content" not in arguments:
        return None, "arguments missing 'content'"
    content = arguments["content"]
    if not isinstance(content, str) or not content.strip():
        return None, "content empty or not a string"
    return content, ""


def positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True)
    ap.add_argument("--base", default="http://localhost:11434")
    ap.add_argument("--trials", type=positive_int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--num-ctx", type=int, default=32768, help="match the matrix run")
    ap.add_argument(
        "--plain", action="store_true", help="fenced-block variant, no tools"
    )
    ap.add_argument(
        "--save-dir", type=Path, default=None, help="save raw responses + provenance"
    )
    args = ap.parse_args()

    if args.save_dir:
        args.save_dir.mkdir(parents=True, exist_ok=True)
        try:
            version = get_json(f"{args.base}/api/version", timeout=10)
        except OSError:
            version = {}
        show = get_json(f"{args.base}/api/show", {"model": args.model}, timeout=60)
        (args.save_dir / "provenance.json").write_text(
            json.dumps(
                {
                    "model": args.model,
                    "base": args.base,
                    "ollama_version": version.get("version"),
                    "model_digest": show.get("modelinfo", {}).get(
                        "general.parameter_count"
                    ),
                    "details": show.get("details"),
                    "args": {
                        k: str(v) for k, v in vars(args).items() if k != "save_dir"
                    },
                },
                indent=2,
            )
        )

    dirty = clean = malformed = failed = 0
    for t in range(args.trials):
        payload = {
            "model": args.model,
            "stream": False,
            "options": {
                "seed": args.seed + t,
                "temperature": 0.6,
                "num_ctx": args.num_ctx,
            },
            "messages": [
                {
                    "role": "user",
                    "content": PROMPT + (PLAIN_SUFFIX if args.plain else TOOL_SUFFIX),
                }
            ],
        }
        if not args.plain:
            payload["tools"] = [WRITE_FILE_TOOL]

        try:
            body = get_json(f"{args.base}/api/chat", payload)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            failed += 1
            print(f"trial {t}: FAILED — {e}")
            continue

        if args.save_dir:
            (args.save_dir / f"trial-{t}.json").write_text(json.dumps(body, indent=2))

        message = body.get("message") or {}
        if args.plain:
            content = extract_plain(message.get("content") or "")
            if not content.strip():
                malformed += 1
                print(f"trial {t}: MALFORMED — empty content")
                continue
        else:
            content, problem = extract_tool_content(message)
            if content is None:
                malformed += 1
                print(f"trial {t}: MALFORMED — {problem}")
                continue

        bad = odd_indent_lines(content)
        if bad:
            dirty += 1
            print(f"trial {t}: DIRTY — {len(bad)} odd-indent lines")
            for lineno, indent, line in bad:
                print(f"    L{lineno} indent={indent}: {line[:70]!r}")
        else:
            clean += 1
            print(f"trial {t}: clean")

    valid = dirty + clean
    print(
        f"\n{args.model}: {dirty} dirty / {valid} valid "
        f"({malformed} malformed, {failed} failed, {args.trials} requested)"
    )
    if valid < args.trials:
        print("⚠ inconclusive: not every trial produced scoreable content")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
