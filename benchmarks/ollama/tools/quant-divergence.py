#!/usr/bin/env python3
"""Quantization fidelity by divergence from a higher-precision reference.

WHY NOT A TASK BENCHMARK. 4-bit vs 8-bit quality gaps are typically 1-3%. The
agentic-coding harness has 6 tasks; harbor's TB-2.1 subset has 10 tasks at
n_attempts 3. The standard error on a pass rate at n=6 is ~20%, and ~9% at
n=30. Neither can resolve a 3% effect, so a "quality comparison" built on them
reports noise with a straight face. (Live example: harbor measured
gemma4:coding-26b at 0.667 and coding-26b-nvfp4 at 0.333 on n=3/n=2 — a 2x
"difference" from five trials.)

WHAT THIS MEASURES INSTEAD. With temperature 0 the model is a deterministic
function from prompt to text. Quantization perturbs the weights, which
perturbs the logits, which eventually flips an argmax and changes the output.
Running the same prompts through reference and candidate and measuring where
they diverge is a direct, high-n measurement of how far quantization moved the
model — every prompt is a data point, not every task.

  exact_match     fraction of prompts where output is byte-identical
  first_div_frac  mean position of the first differing character, as a
                  fraction of the reference length (1.0 = never diverged)
  edit_ratio      mean normalized Levenshtein distance (0 = identical)

INTERPRETATION LIMIT — READ BEFORE QUOTING. Divergence is FIDELITY, not
quality. A diverged continuation may be equally good or better; this cannot
tell you which. It is a valid quality proxy only against a genuine
higher-precision reference, where the reference is by construction the less
perturbed model. For gemma4:12b that reference exists (12b-mlx-bf16). For the
26b family it does NOT — there is no bf16 tag locally — so an nvfp4-vs-mxfp8
comparison there measures only that the two differ, and cannot say which is
closer to the truth. Report it that way.

MANDATORY SELF-CONSISTENCY CONTROL. If a model is not deterministic across two
identical requests, cross-model divergence is meaningless. Gemma 4 ships MTP
draft tensors and Ollama enables speculative decoding by default; speculation
is meant to be output-equivalent, but that is an assumption this script
verifies rather than trusts. Every model is run twice on a subset first, and
the script REFUSES to report divergence for any model that fails.

Usage:
    python3 quant-divergence.py --reference gemma4:12b-mlx-bf16 \\
        --candidates gemma4:12b-mxfp8,gemma4:12b-mlx
    python3 quant-divergence.py --reference gemma4:coding-26b \\
        --candidates gemma4:coding-26b-nvfp4 --no-reference-is-truth
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

# Diverse fixed prompt set: code generation, code reasoning, structured output,
# instruction following, and factual recall. Kept short so each row is cheap
# and n can be large; quantization damage shows up early in a continuation.
PROMPTS = [
    "Write a Python function to reverse a singly linked list. Code only.",
    "Write a Python function that returns the nth Fibonacci number iteratively. Code only.",
    "Explain in two sentences why binary search requires a sorted array.",
    "Write a SQL query returning the second-highest salary from a table `emp(salary)`.",
    "What is the time complexity of heapsort, and why? Answer in one sentence.",
    "Write a bash one-liner that finds all files over 100MB under the current directory.",
    "Convert this to a list comprehension: result = []\\nfor x in nums:\\n    if x % 2: result.append(x*x)",
    "Write a Python dataclass named Point with x and y floats and a method dist_to_origin.",
    "In one sentence, what is the difference between a process and a thread?",
    "Write a regex matching an ISO-8601 date (YYYY-MM-DD). Pattern only.",
    "Write a Python context manager that times the enclosed block. Code only.",
    "Name the four ACID properties and give a three-word gloss for each.",
    "Write a JavaScript function to debounce a callback with a delay. Code only.",
    "Explain what a race condition is, in two sentences.",
    "Write a Python function merging two sorted lists into one sorted list. Code only.",
    "What does the CAP theorem state? One sentence.",
    "Write a Dockerfile for a Python 3.13 app whose entrypoint is main.py.",
    "Write a Python function validating balanced brackets in a string. Code only.",
    "In two sentences, when would you pick a B-tree over a hash index?",
    "Write a Go function that reverses a string, handling multibyte runes. Code only.",
    "Write a Python generator yielding primes under n via a sieve. Code only.",
    "Explain memoization in two sentences, with one concrete example.",
    "Write a git command sequence that squashes the last 3 commits into one.",
    "Write a Python function computing Levenshtein distance. Code only.",
]


def generate(
    url: str, model: str, prompt: str, num_predict: int, timeout: int
) -> str | None:
    body = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0,
                "top_k": 1,
                "top_p": 1,
                "seed": 42,
                "num_predict": num_predict,
            },
        }
    ).encode()
    req = urllib.request.Request(
        f"{url}/api/generate", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        d = json.load(urllib.request.urlopen(req, timeout=timeout))
    except Exception as e:
        print(f"    ! request failed for {model}: {e}", file=sys.stderr)
        return None
    if "error" in d:
        print(f"    ! error from {model}: {d['error']}", file=sys.stderr)
        return None
    return d.get("response", "")


def levenshtein_ratio(a: str, b: str) -> float:
    """Normalized edit distance in O(len(a)*len(b)) time, O(min) space."""
    if a == b:
        return 0.0
    if not a or not b:
        return 1.0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] / max(len(a), len(b))


def first_divergence_frac(ref: str, cand: str) -> float:
    if not ref:
        return 1.0
    n = min(len(ref), len(cand))
    for i in range(n):
        if ref[i] != cand[i]:
            return i / len(ref)
    return 1.0 if len(ref) == len(cand) else n / len(ref)


def check_determinism(
    url, model, prompts, num_predict, timeout
) -> tuple[bool, list[dict]]:
    """Run each prompt twice; every pair must match byte-for-byte.

    Each mismatch retains the prompt and both outputs so a repeatability
    failure is auditable afterwards, not just countable.
    """
    mismatches = []
    for i, p in enumerate(prompts):
        a = generate(url, model, p, num_predict, timeout)
        b = generate(url, model, p, num_predict, timeout)
        if a is None or b is None or a != b:
            mismatches.append(
                {
                    "prompt_index": i,
                    "prompt": p,
                    "request_failed": a is None or b is None,
                    "first_divergence_frac": (
                        first_divergence_frac(a, b)
                        if a is not None and b is not None
                        else None
                    ),
                    "output_a": a,
                    "output_b": b,
                }
            )
    return not mismatches, mismatches


def write_output(path: str | None, payload: dict) -> None:
    if not path:
        return
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nwrote {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:11434")
    ap.add_argument("--reference", required=True)
    ap.add_argument("--candidates", required=True, help="comma-separated tags")
    ap.add_argument("--num-predict", type=int, default=256)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--determinism-probes", type=int, default=4)
    ap.add_argument("--output", default=None)
    ap.add_argument(
        "--no-reference-is-truth",
        action="store_true",
        help="reference is a peer, not higher precision; report as difference only",
    )
    args = ap.parse_args()

    cands = [c.strip() for c in args.candidates.split(",") if c.strip()]
    models = [args.reference] + cands

    print(f"reference : {args.reference}")
    print(f"candidates: {', '.join(cands)}")
    print(f"prompts   : {len(PROMPTS)}   num_predict={args.num_predict}\n")

    print("=== determinism control (each prompt run twice) ===")
    probes = PROMPTS[: args.determinism_probes]
    ok_models = []
    determinism = {}
    for m in models:
        t0 = time.time()
        ok, bad = check_determinism(args.url, m, probes, args.num_predict, args.timeout)
        determinism[m] = {"probes": len(probes), "mismatches": bad}
        flag = "OK" if ok else f"NON-DETERMINISTIC ({len(bad)}/{len(probes)} differed)"
        print(f"  {m:26s} {flag}   [{time.time() - t0:.0f}s]")
        for mm in bad:
            where = (
                "a request failed"
                if mm["request_failed"]
                else f"first divergence at {mm['first_divergence_frac'] * 100:.0f}% of output"
            )
            print(f"      probe {mm['prompt_index']:2d}: {where}")
        if ok:
            ok_models.append(m)
    print()

    def refusal_payload(reason: str) -> dict:
        return {
            "reference": args.reference,
            "candidates": cands,
            "num_predict": args.num_predict,
            "refused": reason,
            "determinism": determinism,
        }

    if args.reference not in ok_models:
        reason = "reference model is not deterministic; divergence is meaningless"
        print(f"REFUSING: {reason}.")
        write_output(args.output, refusal_payload(reason))
        sys.exit(1)
    usable = [c for c in cands if c in ok_models]
    if not usable:
        reason = "no candidate passed the determinism control"
        print(f"REFUSING: {reason}.")
        write_output(args.output, refusal_payload(reason))
        sys.exit(1)

    print("=== collecting reference outputs ===")
    ref_out = {
        p: generate(args.url, args.reference, p, args.num_predict, args.timeout)
        for p in PROMPTS
    }
    ref_out = {p: v for p, v in ref_out.items() if v is not None}
    ref_missing = len(PROMPTS) - len(ref_out)
    print(f"  {len(ref_out)}/{len(PROMPTS)} prompts collected\n")
    if ref_missing:
        print(
            f"  ! INCOMPLETE: {ref_missing} reference request(s) failed; "
            "the comparison will be marked incomplete.\n"
        )

    # A comparison is publishable only with complete coverage: every prompt has
    # a reference output AND a candidate output. Dropped requests would silently
    # bias the metrics toward whatever subset happened to succeed.
    incomplete = ref_missing > 0
    results = {}
    for c in usable:
        print(f"=== {c} ===")
        exact, divs, edits = 0, [], []
        for p, r in ref_out.items():
            o = generate(args.url, c, p, args.num_predict, args.timeout)
            if o is None:
                continue
            if o == r:
                exact += 1
            divs.append(first_divergence_frac(r, o))
            edits.append(levenshtein_ratio(r, o))
        n = len(edits)
        dropped = len(ref_out) - n
        complete = dropped == 0 and ref_missing == 0
        if not complete:
            incomplete = True
        res = {
            "n": n,
            "complete": complete,
            "exact_match": exact / n if n else None,
            "first_div_frac": statistics.mean(divs) if divs else None,
            "edit_ratio": statistics.mean(edits) if edits else None,
        }
        results[c] = res
        if n == 0:
            print("  ! INCOMPLETE: no candidate request succeeded; no metrics\n")
            continue
        if dropped:
            print(f"  ! INCOMPLETE: {dropped} candidate request(s) failed")
        print(f"  exact match    {res['exact_match'] * 100:5.1f}%  ({exact}/{n})")
        print(
            f"  first divergence at {res['first_div_frac'] * 100:5.1f}% of reference length"
        )
        print(f"  mean edit ratio {res['edit_ratio']:.4f}  (0 = identical)\n")

    print("=== SUMMARY ===")
    kind = (
        "difference from peer"
        if args.no_reference_is_truth
        else "distance from reference"
    )
    print(f"({kind}; lower edit_ratio = closer to {args.reference})")
    for c, r in sorted(
        results.items(),
        key=lambda kv: (
            kv[1]["edit_ratio"] if kv[1]["edit_ratio"] is not None else float("inf")
        ),
    ):
        if r["edit_ratio"] is None:
            print(f"  {c:26s} INCOMPLETE — no successful candidate requests")
            continue
        mark = "" if r["complete"] else "  [INCOMPLETE]"
        print(
            f"  {c:26s} edit_ratio={r['edit_ratio']:.4f}  exact={r['exact_match'] * 100:.1f}%{mark}"
        )
    if args.no_reference_is_truth:
        print(
            "\n  NOTE: no higher-precision reference in this pair. This shows THAT they\n"
            "  differ, not which is more faithful. Do not rank quality from it."
        )

    write_output(
        args.output,
        {
            "reference": args.reference,
            "reference_is_truth": not args.no_reference_is_truth,
            "num_predict": args.num_predict,
            "n_prompts": len(ref_out),
            "n_prompts_total": len(PROMPTS),
            "complete": not incomplete,
            "determinism": determinism,
            "results": results,
        },
    )

    if incomplete:
        print(
            "\nINCOMPLETE: one or more requests failed, so prompt coverage is "
            "partial and the metrics above are not publishable as-is.",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
