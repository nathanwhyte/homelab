#!/usr/bin/env python3
"""Ollama Speed Benchmark — local vs remote comparison.

Run on timmy (localhost) and wemby (remote) to measure network overhead.

Usage:
    python3 ollama-speed-bench.py                          # auto-detect localhost vs remote
    python3 ollama-speed-bench.py --host http://timmy:11434
    python3 ollama-speed-bench.py --models qwen3.5:9b-q4_K_M --runs 5
"""

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_MODELS = ["qwen3.5:9b-q8_0", "qwen3.5:9b-q4_K_M"]

TESTS = [
    {
        "name": "Tool call (short)",
        "messages": [
            {"role": "system", "content": "/no_think\nYou are a coding assistant with tools."},
            {"role": "user", "content": "Find all TODO comments in src/"},
        ],
        "tools": [{
            "type": "function",
            "function": {
                "name": "search_code",
                "description": "Search for a regex pattern in files",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "path": {"type": "string"},
                    },
                    "required": ["pattern"],
                },
            },
        }],
    },
    {
        "name": "Code gen (medium)",
        "messages": [
            {"role": "system", "content": "/no_think\nWrite clean code. No explanation."},
            {"role": "user", "content": "Write a Python function that merges two sorted lists into one sorted list."},
        ],
        "tools": None,
    },
    {
        "name": "Explanation (long)",
        "messages": [
            {"role": "system", "content": "/no_think\nBe concise but thorough."},
            {"role": "user", "content": "Explain how a B-tree works and why databases use them instead of binary search trees."},
        ],
        "tools": None,
    },
]


def call_ollama(base_url, model, test):
    """Call Ollama and return timing metrics."""
    payload = {
        "model": model,
        "messages": test["messages"],
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 512},
    }
    if test["tools"]:
        payload["tools"] = test["tools"]

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{base_url}/api/chat",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    wall_start = time.monotonic()
    with urllib.request.urlopen(req, timeout=120) as resp:
        r = json.loads(resp.read())
    wall_ms = int((time.monotonic() - wall_start) * 1000)

    eval_count = r.get("eval_count", 0)
    eval_dur_ns = r.get("eval_duration", 1)
    prompt_dur_ns = r.get("prompt_eval_duration", 1)
    load_dur_ns = r.get("load_duration", 0)
    prompt_count = r.get("prompt_eval_count", 0)

    return {
        "wall_ms": wall_ms,
        "gen_tokens": eval_count,
        "gen_tok_s": round(eval_count / (eval_dur_ns / 1e9), 1) if eval_dur_ns > 0 else 0,
        "prompt_tokens": prompt_count,
        "prompt_tok_s": round(prompt_count / (prompt_dur_ns / 1e9), 1) if prompt_dur_ns > 0 else 0,
        "load_ms": int(load_dur_ns / 1e6),
        "gen_ms": int(eval_dur_ns / 1e6),
        "prompt_ms": int(prompt_dur_ns / 1e6),
        "overhead_ms": wall_ms - int(eval_dur_ns / 1e6) - int(prompt_dur_ns / 1e6) - int(load_dur_ns / 1e6),
    }


def detect_host():
    """Auto-detect: use localhost if Ollama is local, else use timmy's IP."""
    try:
        req = urllib.request.Request("http://localhost:11434/api/version", method="GET")
        with urllib.request.urlopen(req, timeout=2):
            return "http://localhost:11434", "local"
    except Exception:
        pass
    try:
        req = urllib.request.Request("http://192.168.1.19:11434/api/version", method="GET")
        with urllib.request.urlopen(req, timeout=2):
            return "http://192.168.1.19:11434", "remote"
    except Exception:
        pass
    return None, None


def warmup(base_url, model):
    """Warm model into VRAM."""
    try:
        call_ollama(base_url, model, {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": None,
        })
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Ollama Speed Benchmark")
    parser.add_argument("--host", help="Ollama URL (auto-detected if omitted)")
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS), help="Comma-separated model names")
    parser.add_argument("--runs", type=int, default=3, help="Runs per test (default: 3)")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of table")
    args = parser.parse_args()

    if args.host:
        base_url = args.host.rstrip("/")
        location = "custom"
    else:
        base_url, location = detect_host()
        if not base_url:
            print("Error: Ollama not reachable on localhost or 192.168.1.19", file=sys.stderr)
            sys.exit(1)

    models = [m.strip() for m in args.models.split(",")]
    hostname = socket.gethostname()

    print(f"Ollama Speed Benchmark")
    print(f"  Host: {base_url} ({location})")
    print(f"  Machine: {hostname}")
    print(f"  Models: {', '.join(models)}")
    print(f"  Runs: {args.runs} per test\n")

    all_results = []

    for model in models:
        print(f"{'='*70}")
        print(f"  {model}")
        print(f"{'='*70}")
        print(f"  Warming up...", end="", flush=True)
        warmup(base_url, model)
        print(" ready\n")

        print(f"  {'Test':<22} {'Wall':>7} {'Gen t/s':>8} {'Prompt t/s':>10} {'Gen':>5} {'Overhead':>9}")
        print(f"  {'─'*22} {'─'*7} {'─'*8} {'─'*10} {'─'*5} {'─'*9}")

        for test in TESTS:
            run_results = []
            for run in range(args.runs):
                try:
                    r = call_ollama(base_url, model, test)
                    run_results.append(r)
                    all_results.append({
                        "model": model,
                        "test": test["name"],
                        "run": run + 1,
                        "location": location,
                        "hostname": hostname,
                        **r,
                    })
                except Exception as e:
                    print(f"  ERROR: {e}", file=sys.stderr)

            if run_results:
                avg = lambda key: int(sum(r[key] for r in run_results) / len(run_results))
                avg_f = lambda key: round(sum(r[key] for r in run_results) / len(run_results), 1)
                print(
                    f"  {test['name']:<22} {avg('wall_ms'):>6}ms {avg_f('gen_tok_s'):>7} {avg_f('prompt_tok_s'):>9} "
                    f"{avg('gen_tokens'):>5} {avg('overhead_ms'):>8}ms"
                )

        print()

    # Summary
    print(f"{'='*70}")
    print(f"  SUMMARY — {hostname} ({location})")
    print(f"{'='*70}\n")
    print(f"  {'Model':<28} {'Avg Wall':>9} {'Gen t/s':>8} {'Prompt t/s':>10} {'Overhead':>9}")
    print(f"  {'─'*28} {'─'*9} {'─'*8} {'─'*10} {'─'*9}")

    for model in models:
        mr = [r for r in all_results if r["model"] == model]
        if not mr:
            continue
        avg_wall = int(sum(r["wall_ms"] for r in mr) / len(mr))
        avg_gen = round(sum(r["gen_tok_s"] for r in mr) / len(mr), 1)
        avg_prompt = round(sum(r["prompt_tok_s"] for r in mr) / len(mr), 1)
        avg_oh = int(sum(r["overhead_ms"] for r in mr) / len(mr))
        print(f"  {model:<28} {avg_wall:>8}ms {avg_gen:>7} {avg_prompt:>9} {avg_oh:>8}ms")

    print()

    # Save JSON
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    outpath = f"ollama-speed-bench-{hostname}-{ts}.json"
    with open(outpath, "w") as f:
        json.dump({
            "metadata": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "hostname": hostname,
                "location": location,
                "base_url": base_url,
                "models": models,
                "runs_per_test": args.runs,
            },
            "results": all_results,
        }, f, indent=2)
    print(f"  Results saved to: {outpath}")


if __name__ == "__main__":
    main()
