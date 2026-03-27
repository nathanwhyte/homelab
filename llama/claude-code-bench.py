#!/usr/bin/env python3
"""Claude Code Agent Benchmark — 3-model comparison.

Compares Ollama local models against Claude Haiku for coding agent tasks:
tool calling, code generation, code understanding, multi-step reasoning.

Usage:
    ANTHROPIC_API_KEY=sk-... python3 claude-code-bench.py
    python3 claude-code-bench.py --models qwen35-code,haiku-45 --tests T1,T4 --runs 1
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ── Configuration ────────────────────────────────────────────────────────────

MODELS = {
    "qwen35-code": {
        "type": "ollama",
        "base_url": "http://192.168.1.19:11434",
        "model": "qwen3.5:9b-q8_0",
        "label": "Qwen3.5 9B Q8",
    },
    "qwen35-lite": {
        "type": "ollama",
        "base_url": "http://192.168.1.19:11434",
        "model": "qwen3.5:9b-q4_K_M",
        "label": "Qwen3.5 9B Q4",
    },
    "haiku-45": {
        "type": "claude",
        "model": "claude-haiku-4-5-20251001",
        "label": "Claude Haiku 4.5",
    },
    "sonnet-4": {
        "type": "claude",
        "model": "claude-sonnet-4-20250514",
        "label": "Claude Sonnet 4",
    },
    "sonnet-46": {
        "type": "claude",
        "model": "claude-sonnet-4-6",
        "label": "Claude Sonnet 4.6",
    },
}

BENCHMARK_TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file from disk",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "content": {"type": "string", "description": "File content"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "search_code",
        "description": "Search for a regex pattern in files",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern"},
                "path": {"type": "string", "description": "Directory to search"},
                "file_glob": {"type": "string", "description": "File glob filter"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_command",
        "description": "Execute a shell command",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"}
            },
            "required": ["command"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files in a directory",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory path"}},
            "required": ["path"],
        },
    },
]


# ── API Abstraction ──────────────────────────────────────────────────────────


def _strip_think(text):
    """Remove <think>...</think> blocks from Qwen responses."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _parse_text_tool_calls(text):
    """Parse Qwen's text-based <tool_call> fallback format."""
    calls = []
    for m in re.finditer(r"<tool_call>(.*?)</tool_call>", text, re.DOTALL):
        try:
            obj = json.loads(m.group(1))
            calls.append(
                {"name": obj.get("name", ""), "arguments": obj.get("arguments", {})}
            )
        except json.JSONDecodeError:
            pass
    return calls or None


def _tools_for_ollama(tools):
    """Wrap canonical tools in OpenAI-style format for Ollama."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in tools
    ]


def _tools_for_claude(tools):
    """Convert canonical tools to Anthropic format."""
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["parameters"],
        }
        for t in tools
    ]


def _call_ollama(config, messages, tools, max_tokens, temperature):
    """Call Ollama /api/chat and return unified response."""
    # Prepend /no_think to system message to disable thinking
    msgs = []
    for m in messages:
        if m["role"] == "system":
            msgs.append({"role": "system", "content": "/no_think\n" + m["content"]})
        else:
            msgs.append(m)
    if not any(m["role"] == "system" for m in msgs):
        msgs.insert(0, {"role": "system", "content": "/no_think"})

    payload = {
        "model": config["model"],
        "messages": msgs,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    if tools:
        payload["tools"] = _tools_for_ollama(tools)

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{config['base_url']}/api/chat",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    start = time.monotonic()
    with urllib.request.urlopen(req, timeout=120) as resp:
        r = json.loads(resp.read())
    elapsed_ms = int((time.monotonic() - start) * 1000)

    msg = r.get("message", {})
    content = _strip_think(msg.get("content", ""))

    # Normalize tool calls
    tool_calls = None
    if msg.get("tool_calls"):
        tool_calls = [
            {
                "name": tc["function"]["name"],
                "arguments": tc["function"].get("arguments", {}),
            }
            for tc in msg["tool_calls"]
        ]
    elif content:
        tool_calls = _parse_text_tool_calls(content)

    eval_count = r.get("eval_count", 0)
    eval_dur = r.get("eval_duration", 1)
    tps = round(eval_count / (eval_dur / 1e9), 1) if eval_dur > 0 else None

    return {
        "content": content,
        "tool_calls": tool_calls,
        "input_tokens": r.get("prompt_eval_count", 0),
        "output_tokens": eval_count,
        "elapsed_ms": elapsed_ms,
        "tokens_per_sec": tps,
    }


def _call_claude(config, messages, tools, max_tokens, temperature):
    """Call Anthropic Messages API and return unified response."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {
            "content": "ERROR: ANTHROPIC_API_KEY not set",
            "tool_calls": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "elapsed_ms": 0,
            "tokens_per_sec": None,
        }

    # Extract system message
    system_text = ""
    api_messages = []
    for m in messages:
        if m["role"] == "system":
            system_text = m["content"]
        else:
            api_messages.append(m)

    payload = {
        "model": config["model"],
        "max_tokens": max_tokens,
        "messages": api_messages,
        "temperature": temperature,
    }
    if system_text:
        payload["system"] = system_text
    if tools:
        payload["tools"] = _tools_for_claude(tools)

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    start = time.monotonic()
    with urllib.request.urlopen(req, timeout=120) as resp:
        r = json.loads(resp.read())
    elapsed_ms = int((time.monotonic() - start) * 1000)

    # Parse content blocks
    content = ""
    tool_calls = None
    for block in r.get("content", []):
        if block["type"] == "text":
            content += block["text"]
        elif block["type"] == "tool_use":
            if tool_calls is None:
                tool_calls = []
            tool_calls.append(
                {"name": block["name"], "arguments": block.get("input", {})}
            )

    usage = r.get("usage", {})
    return {
        "content": content,
        "tool_calls": tool_calls,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "elapsed_ms": elapsed_ms,
        "tokens_per_sec": None,
    }


def call_model(model_id, messages, tools=None, max_tokens=2048, temperature=0.3):
    """Unified model call returning normalized response dict."""
    config = MODELS[model_id]
    try:
        if config["type"] == "ollama":
            return _call_ollama(config, messages, tools, max_tokens, temperature)
        else:
            return _call_claude(config, messages, tools, max_tokens, temperature)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()
        except Exception:
            pass
        err_msg = f"HTTP {e.code}: {body[:200]}" if body else f"HTTP {e.code}: {e}"
        print(f"    API ERROR: {err_msg}", file=sys.stderr)
        return {
            "content": f"ERROR: {err_msg}",
            "tool_calls": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "elapsed_ms": 0,
            "tokens_per_sec": None,
        }
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"    API ERROR: {e}", file=sys.stderr)
        return {
            "content": f"ERROR: {e}",
            "tool_calls": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "elapsed_ms": 0,
            "tokens_per_sec": None,
        }


# ── Test Definitions ─────────────────────────────────────────────────────────


def _grade_tool(resp, expected_tool, arg_checks):
    """Grade a tool-calling response. Returns (score, reason)."""
    tc = resp.get("tool_calls")
    if not tc:
        return 0.0, "No tool calls in response"
    first = tc[0]
    if first["name"] != expected_tool:
        return 0.0, f"Wrong tool: {first['name']} (expected {expected_tool})"
    args = first.get("arguments", {})
    args_str = json.dumps(args).lower()
    for check in arg_checks:
        if check.lower() not in args_str:
            return 0.5, f"Correct tool but missing '{check}' in args: {args}"
    return 1.0, f"Correct tool and args: {first}"


def _grade_keywords(resp, required, threshold=0.6):
    """Grade by counting keyword matches in content."""
    text = (resp.get("content") or "").lower()
    hits = sum(1 for kw in required if kw.lower() in text)
    score = hits / len(required) if required else 0
    missing = [kw for kw in required if kw.lower() not in text]
    if score >= threshold:
        return score, f"Found {hits}/{len(required)} keywords"
    return score, f"Only {hits}/{len(required)} keywords; missing: {missing}"


def _grade_any_phrase(resp, phrases):
    """Grade by checking if any phrase appears in content."""
    text = (resp.get("content") or "").lower()
    for phrase in phrases:
        if phrase.lower() in text:
            return 1.0, f"Found: '{phrase}'"
    return 0.0, f"None of {phrases} found in response"


TESTS = [
    # ── Category 1: Tool Calling ──
    {
        "id": "T1",
        "name": "Single Tool Selection",
        "category": "tool_calling",
        "messages": [
            {"role": "system", "content": "You are a coding assistant with access to tools. Use the most appropriate tool."},
            {"role": "user", "content": "Find all TODO comments in the src/ directory"},
        ],
        "tools": BENCHMARK_TOOLS,
        "grade": lambda r: _grade_tool(r, "search_code", ["todo", "src"]),
    },
    {
        "id": "T2",
        "name": "Multi-Tool Planning",
        "category": "tool_calling",
        "messages": [
            {"role": "system", "content": "You are a coding assistant with access to tools. Use the most appropriate tool for the first step."},
            {"role": "user", "content": "Read the config file at /etc/app/config.yaml, check if port 8080 is in use, then write a new config with port 9090"},
        ],
        "tools": BENCHMARK_TOOLS,
        "grade": lambda r: _grade_tool(r, "read_file", ["config"]),
    },
    {
        "id": "T3",
        "name": "Argument Precision",
        "category": "tool_calling",
        "messages": [
            {"role": "system", "content": "You are a coding assistant with access to tools. Use the most appropriate tool."},
            {"role": "user", "content": "Search for functions named 'handle_' in all .go files under pkg/"},
        ],
        "tools": BENCHMARK_TOOLS,
        "grade": lambda r: _grade_tool(r, "search_code", ["handle_", "go"]),
    },
    # ── Category 2: Code Generation ──
    {
        "id": "T4",
        "name": "Python Function",
        "category": "code_gen",
        "messages": [
            {"role": "system", "content": "You are a coding assistant. Write clean, correct code. No explanation, just code."},
            {"role": "user", "content": "Write a Python function `parse_cron(expr: str) -> dict` that parses a 5-field cron expression (minute hour dom month dow) into a dict with keys 'minute', 'hour', 'dom', 'month', 'dow', each a sorted list of ints. Handle *, ranges (1-5), steps (*/15), and comma-separated values (1,3,5)."},
        ],
        "tools": None,
        "grade": lambda r: _grade_code_gen(r, ["def parse_cron", "minute", "hour"], python_compile=True),
    },
    {
        "id": "T5",
        "name": "Kubernetes YAML",
        "category": "code_gen",
        "messages": [
            {"role": "system", "content": "You are a Kubernetes expert. Output only YAML, no explanation."},
            {"role": "user", "content": "Create a K8s Deployment for Redis: namespace cache, image redis:7-alpine, 1 replica, memory limit 256Mi, CPU limit 250m, liveness probe on port 6379, PVC mount at /data named redis-data."},
        ],
        "tools": None,
        "grade": lambda r: _grade_keywords(r, ["Deployment", "redis", "livenessProbe", "persistentVolumeClaim", "256Mi"], threshold=0.8),
    },
    {
        "id": "T6",
        "name": "Bash Function",
        "category": "code_gen",
        "messages": [
            {"role": "system", "content": "You are a coding assistant. Write clean, correct code. No explanation, just code."},
            {"role": "user", "content": "Write a bash function `retry_cmd` that takes a command string and retries it up to 3 times with exponential backoff (1s, 2s, 4s). Return 0 on success, 1 on final failure. Print attempt number to stderr."},
        ],
        "tools": None,
        "grade": lambda r: _grade_keywords(r, ["retry", "sleep", "stderr", "return"], threshold=0.75),
    },
    # ── Category 3: Code Understanding ──
    {
        "id": "T7",
        "name": "Bug Detection",
        "category": "code_understanding",
        "messages": [
            {"role": "system", "content": "You are a code reviewer. Identify bugs concisely."},
            {"role": "user", "content": "What is wrong with this Python code?\n\ndef lcs(a, b):\n    m, n = len(a), len(b)\n    dp = [[0] * (n + 1)] * (m + 1)\n    for i in range(1, m + 1):\n        for j in range(1, n + 1):\n            if a[i-1] == b[j-1]:\n                dp[i][j] = dp[i-1][j-1] + 1\n            else:\n                dp[i][j] = max(dp[i-1][j], dp[i][j-1])\n    return dp[m][n]"},
        ],
        "tools": None,
        "grade": lambda r: _grade_any_phrase(r, ["shallow copy", "same reference", "same list", "same row", "same object", "aliased", "all rows are the same", "references to the same"]),
    },
    {
        "id": "T8",
        "name": "Race Condition",
        "category": "code_understanding",
        "messages": [
            {"role": "system", "content": "You are a code reviewer. Identify concurrency issues concisely."},
            {"role": "user", "content": "What could go wrong with this Go code?\n\npackage main\n\nimport \"fmt\"\n\nfunc main() {\n    counter := 0\n    for i := 0; i < 1000; i++ {\n        go func() {\n            counter++\n        }()\n    }\n    fmt.Println(counter)\n}"},
        ],
        "tools": None,
        "grade": lambda r: _grade_race_condition(r),
    },
    {
        "id": "T9",
        "name": "Refactoring",
        "category": "code_understanding",
        "messages": [
            {"role": "system", "content": "You are a code reviewer. Suggest specific refactoring improvements."},
            {"role": "user", "content": "How would you refactor this?\n\ndef process(data):\n    result = []\n    for item in data:\n        if item is not None:\n            if isinstance(item, str):\n                if len(item) > 0:\n                    if len(item) <= 100:\n                        result.append(item.strip().lower())\n                    else:\n                        result.append(item[:100].strip().lower())\n    return result"},
        ],
        "tools": None,
        "grade": lambda r: _grade_any_phrase(r, ["guard clause", "early continue", "flatten", "nested", "simplif", "extract", "combine", "consolidat", "walrus", "ternary", "min(", "[:100]"]),
    },
    # ── Category 4: Multi-Step Reasoning ──
    {
        "id": "T10",
        "name": "Debug Plan",
        "category": "reasoning",
        "messages": [
            {"role": "system", "content": "You are a Kubernetes SRE. Provide a systematic debugging plan."},
            {"role": "user", "content": "A user reports their K8s pod is in CrashLoopBackOff. The pod uses a PVC backed by Longhorn, runs on a node with a GPU, and the container image was just updated. Walk me through your debugging steps."},
        ],
        "tools": None,
        "grade": lambda r: _grade_keywords(r, ["describe", "logs", "events", "volume", "image", "node"], threshold=0.6),
    },
    {
        "id": "T11",
        "name": "Migration Plan",
        "category": "reasoning",
        "messages": [
            {"role": "system", "content": "You are a database migration expert. Provide a concrete migration plan."},
            {"role": "user", "content": "Plan the steps to migrate a PostgreSQL database from a single-node Docker Compose setup to a K8s StatefulSet with Longhorn storage. The database is 20GB and must have zero downtime."},
        ],
        "tools": None,
        "grade": lambda r: _grade_keywords(r, ["StatefulSet", "pg_dump", "PersistentVolume", "service", "test", "rollback"], threshold=0.5),
    },
    {
        "id": "T12",
        "name": "Next Step Reasoning",
        "category": "reasoning",
        "messages": [
            {"role": "system", "content": "You are a coding assistant helping debug an issue."},
            {"role": "user", "content": "I ran `kubectl describe pod myapp-abc123` and saw:\n  State: CrashLoopBackOff\n  Last State: OOMKilled (exit code 137)\n  Limits: memory=512Mi\n  Requests: memory=256Mi\n\nThen I checked `kubectl top pod myapp-abc123` and got:\n  NAME            CPU    MEMORY\n  myapp-abc123    50m    498Mi\n\nWhat should I do next?"},
        ],
        "tools": None,
        "grade": lambda r: _grade_any_phrase(r, ["increase memory", "raise the limit", "memory limit", "1Gi", "1024Mi", "768Mi", "bump", "raise limit"]),
    },
]


def _grade_code_gen(resp, keywords, python_compile=False):
    """Grade code generation: keyword checks + optional Python compile check."""
    text = resp.get("content") or ""
    text_lower = text.lower()

    hits = sum(1 for kw in keywords if kw.lower() in text_lower)
    if hits < len(keywords):
        missing = [kw for kw in keywords if kw.lower() not in text_lower]
        return hits / len(keywords) * 0.5, f"Missing keywords: {missing}"

    if python_compile:
        # Extract code from markdown fences
        code = text
        m = re.search(r"```(?:python)?\n(.*?)```", text, re.DOTALL)
        if m:
            code = m.group(1)
        try:
            compile(code, "<test>", "exec")
            return 1.0, "All keywords present + valid Python syntax"
        except SyntaxError:
            return 0.7, "All keywords present but syntax error"

    return 1.0, f"All {len(keywords)} keywords present"


def _grade_race_condition(resp):
    """Grade race condition detection."""
    text = (resp.get("content") or "").lower()
    has_race = any(p in text for p in ["race condition", "data race", "race", "concurrent"])
    has_fix = any(p in text for p in ["mutex", "sync", "channel", "atomic", "lock"])
    if has_race and has_fix:
        return 1.0, "Identified race condition and suggested fix"
    if has_race:
        return 0.5, "Identified race condition but no fix suggested"
    return 0.0, "Did not identify race condition"


# ── Runner ───────────────────────────────────────────────────────────────────


def preload_model(model_id):
    """Send a dummy request to warm Ollama model into VRAM."""
    config = MODELS[model_id]
    if config["type"] != "ollama":
        return
    print(f"  Warming {config['label']}...", end="", flush=True)
    try:
        call_model(model_id, [{"role": "user", "content": "hi"}], max_tokens=1)
        print(" ready")
    except Exception:
        print(" failed (continuing)")


def run_benchmark(model_ids, test_ids, runs_per_test):
    """Run benchmark and return results list."""
    tests = TESTS
    if test_ids:
        tests = [t for t in TESTS if t["id"] in test_ids]

    results = []
    for model_id in model_ids:
        config = MODELS[model_id]
        print(f"\n{'='*60}")
        print(f"  Model: {config['label']} ({model_id})")
        print(f"{'='*60}")
        preload_model(model_id)

        for test in tests:
            for run in range(1, runs_per_test + 1):
                resp = call_model(
                    model_id,
                    test["messages"],
                    tools=test.get("tools"),
                )
                score, reason = test["grade"](resp)
                passed = score >= 0.6

                tag = "PASS" if passed else "FAIL"
                tps = f"{resp['tokens_per_sec']} t/s" if resp["tokens_per_sec"] else "—"
                print(
                    f"  [{test['id']}/R{run}] {tag} ({resp['elapsed_ms']}ms, {tps}) — {reason[:60]}"
                )

                results.append({
                    "test_id": test["id"],
                    "test_name": test["name"],
                    "category": test["category"],
                    "model": model_id,
                    "run": run,
                    "elapsed_ms": resp["elapsed_ms"],
                    "input_tokens": resp["input_tokens"],
                    "output_tokens": resp["output_tokens"],
                    "tokens_per_sec": resp["tokens_per_sec"],
                    "passed": passed,
                    "score": score,
                    "reason": reason,
                    "content_preview": (resp.get("content") or "")[:200],
                    "tool_calls": resp.get("tool_calls"),
                })

                # Rate limit for Claude
                if config["type"] == "claude":
                    time.sleep(0.5)

    return results


def print_summary(results, model_ids):
    """Print summary table to stdout."""
    categories = ["tool_calling", "code_gen", "code_understanding", "reasoning"]
    cat_short = {"tool_calling": "Tool", "code_gen": "Code", "code_understanding": "Edit", "reasoning": "Plan"}

    print(f"\n{'='*80}")
    print("  CLAUDE CODE AGENT BENCHMARK — RESULTS")
    print(f"{'='*80}\n")

    header = f"  {'Model':<22} {'Pass':>6} {'Avg ms':>8} {'Avg t/s':>9}"
    for cat in categories:
        header += f" {cat_short[cat]:>6}"
    print(header)
    print(f"  {'─'*22} {'─'*6} {'─'*8} {'─'*9}" + " ".join(f"{'─'*6}" for _ in categories))

    for mid in model_ids:
        mr = [r for r in results if r["model"] == mid]
        if not mr:
            continue
        total = len(mr)
        passed = sum(1 for r in mr if r["passed"])
        avg_ms = int(sum(r["elapsed_ms"] for r in mr) / total) if total else 0
        tps_vals = [r["tokens_per_sec"] for r in mr if r["tokens_per_sec"]]
        avg_tps = f"{sum(tps_vals) / len(tps_vals):.0f}" if tps_vals else "—"

        row = f"  {MODELS[mid]['label']:<22} {passed}/{total:>3} {avg_ms:>7}ms {avg_tps:>8}"
        for cat in categories:
            cr = [r for r in mr if r["category"] == cat]
            cp = sum(1 for r in cr if r["passed"])
            row += f" {cp}/{len(cr):>3}"
        print(row)

    print()


def save_results(results, model_ids, output_dir):
    """Save JSON results to file."""
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(output_dir, f"results-{ts}.json")

    out = {
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "models": {mid: MODELS[mid] for mid in model_ids},
            "total_tests": len(set(r["test_id"] for r in results)),
            "runs_per_test": max((r["run"] for r in results), default=1),
        },
        "results": results,
    }
    with open(path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"Results saved to: {path}")
    return path


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Claude Code Agent Benchmark")
    parser.add_argument(
        "--models",
        default=",".join(MODELS.keys()),
        help="Comma-separated model IDs (default: all)",
    )
    parser.add_argument("--tests", default="", help="Comma-separated test IDs (default: all)")
    parser.add_argument("--runs", type=int, default=3, help="Runs per test (default: 3)")
    parser.add_argument(
        "--output",
        default="./claude-code-bench-results",
        help="Output directory",
    )
    args = parser.parse_args()

    model_ids = [m.strip() for m in args.models.split(",")]
    test_ids = [t.strip() for t in args.tests.split(",") if t.strip()] or None

    # Validate
    for mid in model_ids:
        if mid not in MODELS:
            print(f"Unknown model: {mid}. Available: {list(MODELS.keys())}")
            sys.exit(1)

    if "haiku-45" in model_ids and not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY not set (required for haiku-45)")
        sys.exit(1)

    print("Claude Code Agent Benchmark")
    print(f"Models: {', '.join(MODELS[m]['label'] for m in model_ids)}")
    print(f"Tests: {test_ids or 'all 12'} × {args.runs} runs each")

    results = run_benchmark(model_ids, test_ids, args.runs)
    print_summary(results, model_ids)
    save_results(results, model_ids, args.output)


if __name__ == "__main__":
    main()
