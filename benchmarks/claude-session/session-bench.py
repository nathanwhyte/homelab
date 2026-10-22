#!/usr/bin/env python3
"""Claude Code session-start latency benchmark.

Measures end-to-end first-turn latency of headless Claude Code sessions
running against a local Ollama model (via `ollama launch claude`), across
scenarios that isolate the contributors to session-start delay:

  - injected-context volume (full plugin/hook config vs bare config dir)
  - model load / KV-cache allocation (cold vs warm, context-window size)
  - prompt-cache effects (repeat runs within a scenario share the static
    system-prompt prefix and may hit Ollama's KV prefix cache — first
    iteration of each scenario is the honest cold-prefix number)

Per run it records, from `--output-format stream-json` event timestamps:

  t_init             harness startup -> `system:init` event (CLI boot, hooks)
  t_first_assistant  harness startup -> first assistant event (end-to-end TTFT)
  t_total            harness startup -> `result` event
  input_tokens       prefill volume actually sent to the model
  cache_read/cache_creation tokens when the endpoint reports them
  duration_ms / duration_api_ms from the result event

Usage:
  uv run python benchmarks/claude-session/session-bench.py \
      benchmarks/claude-session/configs/pop-claude-session-qwen36.toml
  # optional: --scenario full-warm --repeats 1 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomllib

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Scenario:
    name: str
    description: str = ""
    cwd: str = ""
    config_dir: str = ""  # CLAUDE_CONFIG_DIR override; "BARE" = materialize minimal dir
    env: dict[str, str] = field(default_factory=dict)
    cold: bool = False  # `ollama stop <model>` before each iteration
    prompt: str = "Reply with exactly: OK"
    extra_args: list[str] = field(default_factory=list)


def load_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def materialize_bare_config(workdir: Path) -> Path:
    """Create a minimal CLAUDE_CONFIG_DIR: no plugins, no hooks, no CLAUDE.md."""
    cfg = workdir / "bare-claude-config"
    cfg.mkdir(parents=True, exist_ok=True)
    settings = cfg / "settings.json"
    if not settings.exists():
        settings.write_text('{"hasCompletedOnboarding": true}\n')
    return cfg


def stop_model(model: str, ollama_host: str | None) -> None:
    env = os.environ.copy()
    if ollama_host:
        env["OLLAMA_HOST"] = ollama_host
    subprocess.run(
        ["ollama", "stop", model],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    time.sleep(2)


def ollama_ps(ollama_host: str | None) -> str:
    env = os.environ.copy()
    if ollama_host:
        env["OLLAMA_HOST"] = ollama_host
    proc = subprocess.run(
        ["ollama", "ps"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc.stdout.strip()


def run_once(
    scenario: Scenario,
    model: str,
    ollama_host: str | None,
    workdir: Path,
    timeout_s: float,
) -> dict[str, Any]:
    cwd = Path(scenario.cwd).expanduser() if scenario.cwd else workdir / "empty-project"
    cwd.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    # Never let the harness's own Claude session leak into the child.
    for var in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION"):
        env.pop(var, None)
    if ollama_host:
        env["OLLAMA_HOST"] = ollama_host
    if scenario.config_dir == "BARE":
        env["CLAUDE_CONFIG_DIR"] = str(materialize_bare_config(workdir))
    elif scenario.config_dir:
        env["CLAUDE_CONFIG_DIR"] = str(Path(scenario.config_dir).expanduser())
    env.update(scenario.env)

    cmd = [
        "ollama",
        "launch",
        "claude",
        "--model",
        model,
        "--",
        "-p",
        scenario.prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        "1",
        "--dangerously-skip-permissions",
        *scenario.extra_args,
    ]

    if scenario.cold:
        stop_model(model, ollama_host)

    t0 = time.monotonic()
    rec: dict[str, Any] = {
        "scenario": scenario.name,
        "cold": scenario.cold,
        "t_init_s": None,
        "t_first_assistant_s": None,
        "t_total_s": None,
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
        "duration_ms": None,
        "duration_api_ms": None,
        "num_turns": None,
        "is_error": False,
        "events": [],
    }

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            now = time.monotonic() - t0
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            rec["events"].append({"t": round(now, 3), "type": etype})
            if etype == "system" and event.get("subtype") == "init":
                rec["t_init_s"] = round(now, 3)
            elif etype == "assistant" and rec["t_first_assistant_s"] is None:
                rec["t_first_assistant_s"] = round(now, 3)
            elif etype == "result":
                rec["t_total_s"] = round(now, 3)
                rec["is_error"] = bool(event.get("is_error"))
                rec["duration_ms"] = event.get("duration_ms")
                rec["duration_api_ms"] = event.get("duration_api_ms")
                rec["num_turns"] = event.get("num_turns")
                usage = event.get("usage") or {}
                rec["input_tokens"] = usage.get("input_tokens")
                rec["output_tokens"] = usage.get("output_tokens")
                rec["cache_read_input_tokens"] = usage.get("cache_read_input_tokens")
                rec["cache_creation_input_tokens"] = usage.get(
                    "cache_creation_input_tokens"
                )
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        rec["is_error"] = True
        rec["error"] = f"timeout after {timeout_s}s"
    if proc.returncode not in (0, None) and not rec.get("t_total_s"):
        rec["is_error"] = True
        stderr = proc.stderr.read() if proc.stderr else ""
        rec["error"] = stderr.strip()[-2000:]
    rec["ollama_ps_after"] = ollama_ps(ollama_host)
    return rec


def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    def stats(key: str) -> dict[str, float] | None:
        vals = [r[key] for r in runs if r.get(key) is not None]
        if not vals:
            return None
        return {
            "min": round(min(vals), 3),
            "p50": round(statistics.median(vals), 3),
            "max": round(max(vals), 3),
            "mean": round(statistics.fmean(vals), 3),
            "n": len(vals),
        }

    return {
        "t_init_s": stats("t_init_s"),
        "t_first_assistant_s": stats("t_first_assistant_s"),
        "t_total_s": stats("t_total_s"),
        "input_tokens": stats("input_tokens"),
        "errors": sum(1 for r in runs if r.get("is_error")),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("config", type=Path)
    ap.add_argument("--scenario", action="append", help="run only these scenario names")
    ap.add_argument("--repeats", type=int, default=None, help="override config repeats")
    ap.add_argument("--dry-run", action="store_true", help="print commands, do not run")
    args = ap.parse_args()

    cfg = load_config(args.config)
    model = cfg["session"]["model"]
    ollama_host = cfg["session"].get("ollama_host") or None
    repeats = args.repeats or cfg["session"].get("repeats", 3)
    timeout_s = cfg["session"].get("timeout_seconds", 600)
    cooldown_s = cfg["session"].get("cooldown_seconds", 5)
    out_dir = REPO_ROOT / cfg["output"]["dir"]
    prefix = cfg["output"]["prefix"]
    workdir = out_dir / f"{prefix}-workdir"
    workdir.mkdir(parents=True, exist_ok=True)

    scenarios = [Scenario(**s) for s in cfg["scenarios"]]
    if args.scenario:
        scenarios = [s for s in scenarios if s.name in set(args.scenario)]
        if not scenarios:
            print(f"no scenarios matched {args.scenario}", file=sys.stderr)
            return 2

    if shutil.which("ollama") is None or shutil.which("claude") is None:
        print("ollama and claude must both be on PATH", file=sys.stderr)
        return 2

    results: dict[str, Any] = {
        "model": model,
        "repeats": repeats,
        "config": str(args.config),
        "scenarios": {},
    }
    for scenario in scenarios:
        print(f"\n=== scenario: {scenario.name} ({scenario.description}) ===")
        if args.dry_run:
            print(
                f"  cold={scenario.cold} cwd={scenario.cwd or '<empty>'} env={scenario.env}"
            )
            continue
        runs = []
        for i in range(repeats):
            print(f"  run {i + 1}/{repeats} ...", end="", flush=True)
            rec = run_once(scenario, model, ollama_host, workdir, timeout_s)
            runs.append(rec)
            print(
                f" init={rec['t_init_s']}s ttft={rec['t_first_assistant_s']}s "
                f"total={rec['t_total_s']}s prefill={rec['input_tokens']}tok"
                + (" ERROR" if rec.get("is_error") else "")
            )
            time.sleep(cooldown_s)
        results["scenarios"][scenario.name] = {
            "description": scenario.description,
            "runs": runs,
            "summary": summarize(runs),
        }

    if not args.dry_run:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_path = out_dir / f"{prefix}-{stamp}.json"
        out_path.write_text(json.dumps(results, indent=2) + "\n")
        print(f"\nresults written to {out_path}")
        for name, data in results["scenarios"].items():
            print(f"  {name}: {json.dumps(data['summary'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
