#!/usr/bin/env python3
"""Graded agentic-coding benchmark for Ollama `:coding` tags (PROJ-1003).

This is the quality counterpart to `concurrency-bench.py`. That harness measures
how fast tokens come out; this one measures whether the model can actually drive
a tool loop to a working patch.

Scoring is verifiable, not judged: the model works in a throwaway sandbox through
four tools (list_files / read_file / write_file / run_tests), and the task is
scored by running a HIDDEN verifier script the model never sees. `solved` means
`python3 verify.py` exited 0. Nothing about plausibility enters the score.

Secondary metrics, all mechanical:

  * `bad_tool_calls`  — calls naming an unknown tool, or with arguments that miss
    a required field or carry the wrong type. This is the tool-schema adherence
    number, and it is the one that usually separates local models.
  * `text_tool_attempts` — turns where the model emitted a JSON tool call as
    ordinary text instead of using the tool-call channel. Counted separately
    because it is a template/engine failure, not a reasoning failure.
  * `turns`, `wall_s`, token counts, and whether the loop hit its turn cap.

Style is NOT scored here. Run `agentic-coding-judge.py` over this script's JSON
output for that, so the expensive model runs never have to be repeated when the
rubric changes.

Anti-cheat measures, because a scored agent loop invites shortcuts:

  * Fixture-supplied `test_*.py` files are RESTORED to their original contents
    before the verifier runs. A T3 task's verifier imports and runs the existing
    suite, so a model that empties or rewrites it would otherwise pass trivially.
    Tampering is recorded, not silently repaired.
  * `run_tests` executes only the suites the fixture shipped, never a `test_*.py`
    the model invented.
  * Writes are confined to the sandbox by basename, and `verify.py` is reserved.

Reproducibility: every run records the seed, the resolved model digest, the
effective sampling parameters as the server reports them, the Ollama version, and
SHA-256 hashes of this harness and the fixture module. At temperature 0.6-1.0 a
single six-task sample moves between runs, so use --repeats for any comparison
you intend to publish.

Usage:
    python3 agentic-coding-bench.py --model gemma4:coding-12b --tiers 1,2,3
    python3 agentic-coding-bench.py --model qwen3.6:coding --think --repeats 3

One model at a time, on purpose: this drives a locally-served model, and the
homelab rule is at most one local-model consumer in flight (a second lane buys
nothing on one GPU and costs RAM).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from coding_tasks import CodingTask, tasks_for_tiers

DEFAULT_BASE = "http://localhost:11434"

SYSTEM_PROMPT = """You are a coding agent working in a small repository.

Use the provided tools to inspect and edit files. Rules:
- Always read a file before you rewrite it.
- write_file replaces the ENTIRE file, so pass the complete new contents.
- Make the smallest change that satisfies the request; do not restructure code
  that is already correct, and do not rename public functions or classes.
- When the task is done, reply with a short plain-text summary and no tool call.

Do not ask the user questions; you are running unattended."""

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List the files in the repository.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full contents of one file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repository-relative file path, e.g. stats.py",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Replace a file's entire contents with new text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repository-relative file path",
                    },
                    "content": {
                        "type": "string",
                        "description": "The complete new file contents",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Run the repository's existing test suite and return its output.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

_TOOL_SCHEMAS = {t["function"]["name"]: t["function"]["parameters"] for t in TOOLS}

_JSON_TYPES = {"string": str, "object": dict, "array": list, "boolean": bool}


@dataclass
class TaskResult:
    task_id: str
    tier: int
    title: str
    solved: bool
    verifier_output: str
    turns: int
    hit_turn_cap: bool
    tool_calls: int
    bad_tool_calls: int
    repeat: int = 0
    bad_tool_detail: list[str] = field(default_factory=list)
    text_tool_attempts: int = 0
    wrote_any_file: bool = False
    files_written: list[str] = field(default_factory=list)
    ran_tests: int = 0
    # Turns cut off by num_predict. A model that rambles past the cap on every
    # turn is not being scored on the same footing as one that answers, so this
    # has to be visible rather than folded into a pass/fail.
    truncated_turns: int = 0
    # Fixture test suites the model modified. Restored before scoring, but
    # recorded here: rewriting the suite a verifier runs is a cheat attempt, and
    # it should be visible in the results rather than quietly undone.
    tampered_suites: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    output_tokens: int = 0
    wall_s: float = 0.0
    error: str | None = None
    final_sources: dict[str, str] = field(default_factory=dict)


def post_chat(
    base: str, payload: dict[str, Any], timeout: int
) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        f"{base}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read() or b"{}"
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"error": body.decode(errors="replace")[:400]}
    except Exception as e:  # noqa: BLE001 - a dead runner must be recorded, not raised
        return 0, {"error": f"{type(e).__name__}: {e}"}


def validate_tool_call(name: str, args: Any) -> str | None:
    """Return None when the call is well-formed, else a short reason."""
    schema = _TOOL_SCHEMAS.get(name)
    if schema is None:
        return f"unknown tool {name!r}"
    if not isinstance(args, dict):
        return f"{name}: arguments must be an object, got {type(args).__name__}"
    for required in schema.get("required", []):
        if required not in args:
            return f"{name}: missing required argument {required!r}"
    props = schema.get("properties", {})
    for key, value in args.items():
        spec = props.get(key)
        if spec is None:
            return f"{name}: unexpected argument {key!r}"
        expected = _JSON_TYPES.get(spec.get("type"))
        if expected is not None and not isinstance(value, expected):
            return f"{name}: {key} must be {spec['type']}, got {type(value).__name__}"
    return None


def looks_like_text_tool_call(content: str) -> bool:
    """Heuristic: the model described a tool call in prose instead of calling it."""
    if not content:
        return False
    lowered = content.lower()
    if '"name"' in lowered and '"arguments"' in lowered:
        return True
    return any(
        f'"{tool}"' in lowered or f"<{tool}>" in lowered for tool in _TOOL_SCHEMAS
    )


_SANDBOX_PROFILE = """(version 1)
(allow default)
(deny network*)
(deny file-write*)
(allow file-write* (subpath "{sandbox}"))
(allow file-write-data (literal "/dev/null") (literal "/dev/dtracehelper"))
"""

# Resolved once by probe_sandbox(): True when sandbox-exec confinement works on
# this host, False when we had to fall back to plain execution.
_SANDBOX_AVAILABLE: bool | None = None


def _sandbox_command(sandbox: Path, argv: list[str]) -> list[str]:
    if not _SANDBOX_AVAILABLE:
        return argv
    profile = _SANDBOX_PROFILE.format(sandbox=sandbox.resolve())
    return ["/usr/bin/sandbox-exec", "-p", profile, *argv]


def _minimal_env(sandbox: Path) -> dict[str, str]:
    """Strip the inherited environment down to what CPython needs to start.

    Model-authored code runs here. It cannot reach credentials, tokens, or proxy
    settings through the environment, and with sandbox-exec active it cannot
    reach the network or write outside the sandbox either.
    """
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(sandbox),
        "TMPDIR": str(sandbox),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "LC_ALL": "C.UTF-8",
    }


def run_python(path: Path, script: str, timeout: int = 60) -> tuple[int, str]:
    """Run a python script inside the sandbox and return (returncode, output)."""
    script_path = path / script
    # -s drops user site-packages; -E ignores inherited PYTHON* variables. NOT -I:
    # isolated mode also removes the script's own directory from sys.path, which
    # makes every `from stats import ...` in a verifier fail with
    # ModuleNotFoundError regardless of what the model wrote.
    argv = _sandbox_command(path, [sys.executable, "-s", "-E", str(script_path)])
    try:
        proc = subprocess.run(
            argv,
            cwd=path,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_minimal_env(path),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()[-2000:]


def probe_sandbox() -> bool:
    """Check whether sandbox-exec confinement actually runs CPython on this host.

    Returns True when confinement is active. A False result is reported loudly
    rather than silently downgraded: without it, model-authored code executes
    with this user's filesystem and network access.
    """
    global _SANDBOX_AVAILABLE
    if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").exists():
        _SANDBOX_AVAILABLE = False
        return False

    _SANDBOX_AVAILABLE = True
    probe_dir = Path(tempfile.mkdtemp(prefix="sandbox-probe-"))
    try:
        (probe_dir / "probe.py").write_text(
            "import socket, pathlib\n"
            "pathlib.Path('wrote.txt').write_text('ok')\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 53), timeout=2)\n"
            "    print('NETWORK-REACHABLE')\n"
            "except Exception:\n"
            "    print('network-blocked')\n"
        )
        code, out = run_python(probe_dir, "probe.py", timeout=30)
        confined = code == 0 and "network-blocked" in out
        if not confined:
            _SANDBOX_AVAILABLE = False
        return confined
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


RESERVED_FILENAMES = {"verify.py"}


def execute_tool(
    sandbox: Path,
    name: str,
    args: dict[str, Any],
    fixture_suites: list[str],
) -> str:
    if name == "list_files":
        names = sorted(
            p.name
            for p in sandbox.iterdir()
            if p.is_file() and p.name not in RESERVED_FILENAMES
        )
        return "\n".join(names)
    if name == "read_file":
        target = sandbox / Path(args["path"]).name
        if target.name in RESERVED_FILENAMES or not target.exists():
            return f"error: no such file: {args['path']}"
        return target.read_text()
    if name == "write_file":
        # Basename-only: a path like ../../x.py collapses to x.py and stays in
        # the sandbox. verify.py is reserved so the scoring script cannot be
        # overwritten by the thing being scored.
        target = sandbox / Path(args["path"]).name
        if target.name in RESERVED_FILENAMES:
            return f"error: {target.name} is reserved and cannot be written"
        target.write_text(args["content"])
        return f"wrote {target.name} ({len(args['content'])} bytes)"
    if name == "run_tests":
        # Only the suites the fixture shipped. A model-authored test_*.py is not
        # evidence of anything, and running it would just be executing whatever
        # the model felt like writing.
        if not fixture_suites:
            return "no test suite in this repository"
        chunks = []
        for suite in fixture_suites:
            if not (sandbox / suite).exists():
                chunks.append(f"$ python3 {suite}\nerror: file is missing")
                continue
            code, out = run_python(sandbox, suite)
            chunks.append(f"$ python3 {suite}\nexit={code}\n{out}")
        return "\n\n".join(chunks)
    return f"error: unknown tool {name}"


def run_task(
    base: str,
    model: str,
    task: CodingTask,
    think: bool | None,
    num_ctx: int,
    timeout: int,
    keep_sandbox: Path | None,
    seed: int | None,
    num_predict: int,
    repeat: int = 0,
) -> TaskResult:
    sandbox = Path(tempfile.mkdtemp(prefix=f"agentic-{task.task_id}-"))
    for name, content in task.files.items():
        (sandbox / name).write_text(content)

    fixture_suites = sorted(
        n for n in task.files if n.startswith("test_") and n.endswith(".py")
    )

    result = TaskResult(
        task_id=task.task_id,
        tier=task.tier,
        title=task.title,
        solved=False,
        verifier_output="",
        turns=0,
        hit_turn_cap=False,
        tool_calls=0,
        bad_tool_calls=0,
        repeat=repeat,
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"{task.prompt}\n\nFiles in the repository: "
                f"{', '.join(sorted(task.files))}"
            ),
        },
    ]

    started = time.time()
    try:
        for _ in range(task.max_turns):
            result.turns += 1
            options: dict[str, Any] = {"num_ctx": num_ctx, "num_predict": num_predict}
            if seed is not None:
                options["seed"] = seed
            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "tools": TOOLS,
                "stream": False,
                "keep_alive": "30m",
                "options": options,
            }
            # `think` belongs at the request-body top level. Nesting it inside
            # options is ERR-1004: the model burns its whole budget on reasoning
            # and returns empty content.
            if think is not None:
                payload["think"] = think

            status, body = post_chat(base, payload, timeout)
            if status != 200:
                result.error = f"http {status}: {str(body.get('error'))[:200]}"
                break

            result.prompt_tokens += body.get("prompt_eval_count") or 0
            result.output_tokens += body.get("eval_count") or 0
            if body.get("done_reason") == "length":
                result.truncated_turns += 1
            message = body.get("message") or {}
            calls = message.get("tool_calls") or []
            content = message.get("content") or ""

            if not calls:
                if looks_like_text_tool_call(content):
                    result.text_tool_attempts += 1
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": calls,
                }
            )

            for call in calls:
                fn = call.get("function") or {}
                name = fn.get("name") or ""
                args = fn.get("arguments")
                if isinstance(args, str):
                    # Some templates hand back arguments as a JSON string rather
                    # than an object. Parse it when we can; when we cannot, leave
                    # the string in place so validate_tool_call scores it as the
                    # malformed call it is.
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        pass
                result.tool_calls += 1
                problem = validate_tool_call(name, args)
                if problem is not None:
                    result.bad_tool_calls += 1
                    if len(result.bad_tool_detail) < 10:
                        result.bad_tool_detail.append(problem)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_name": name,
                            "content": f"error: {problem}",
                        }
                    )
                    continue

                if name == "write_file":
                    result.wrote_any_file = True
                    written = Path(args["path"]).name
                    if written not in result.files_written:
                        result.files_written.append(written)
                if name == "run_tests":
                    result.ran_tests += 1

                output = execute_tool(sandbox, name, args, fixture_suites)
                messages.append({"role": "tool", "tool_name": name, "content": output})
        else:
            result.hit_turn_cap = True

        result.wall_s = round(time.time() - started, 1)

        # Restore the fixture's own test suites before scoring. A T3 verifier
        # imports and runs the shipped suite, so a model that emptied or
        # weakened it would otherwise score a pass for deleting the evidence.
        for suite in fixture_suites:
            path = sandbox / suite
            original = task.files[suite]
            if not path.exists() or path.read_text() != original:
                result.tampered_suites.append(suite)
                path.write_text(original)

        (sandbox / "verify.py").write_text(task.verifier)
        code, out = run_python(sandbox, "verify.py")
        result.solved = code == 0
        result.verifier_output = out[-1200:]

        for name in task.target_files:
            path = sandbox / name
            if path.exists():
                result.final_sources[name] = path.read_text()
    finally:
        if keep_sandbox is not None:
            dest = keep_sandbox / f"{model.replace(':', '_')}-{task.task_id}"
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(sandbox, dest)
        shutil.rmtree(sandbox, ignore_errors=True)

    return result


def unload(base: str, model: str) -> None:
    post_chat(base, {"model": model, "messages": [], "keep_alive": 0}, timeout=120)


def _get_json(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"} if data else {}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:  # noqa: BLE001 - provenance is best-effort, never fatal
        return {"error": f"{type(e).__name__}: {e}"}


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "unavailable"


def collect_provenance(base: str, model: str) -> dict[str, Any]:
    """Everything needed to say what, exactly, produced a given score.

    A tag name is mutable — `ollama create` can repoint it in seconds — so a
    result identified only by "gemma4:coding-12b" is not attributable. The digest
    and the server-reported parameters are what make a row reproducible.
    """
    show = _get_json(f"{base}/api/show", {"model": model})
    details = show.get("details") or {}
    params_raw = show.get("parameters") or ""
    effective = {}
    for line in params_raw.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            effective[parts[0]] = parts[1].strip()

    # The manifest digest lives on /api/tags, not /api/show.
    digest = None
    modified_at = None
    for entry in _get_json(f"{base}/api/tags").get("models") or []:
        if entry.get("name") == model:
            digest = entry.get("digest")
            modified_at = entry.get("modified_at")
            break

    here = Path(__file__).resolve()
    return {
        "model": model,
        "model_digest": digest,
        "model_modified_at": modified_at,
        "details": {
            "family": details.get("family"),
            "parameter_size": details.get("parameter_size"),
            "quantization_level": details.get("quantization_level"),
        },
        "capabilities": show.get("capabilities"),
        "effective_parameters": effective,
        "ollama_version": _get_json(f"{base}/api/version").get("version"),
        "harness_sha256": _sha256(here),
        "fixtures_sha256": _sha256(here.parent / "coding_tasks" / "__init__.py"),
        "host": platform.node(),
        "python": platform.python_version(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True, help="Ollama tag to score")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--tiers", default="1,2,3", help="comma-separated tiers, e.g. 1,2")
    ap.add_argument("--num-ctx", type=int, default=32768)
    ap.add_argument(
        "--num-predict",
        type=int,
        default=4096,
        help=(
            "per-turn output cap. Unbounded generation lets one rambling model "
            "consume the whole matrix; 4096 is ~10x a normal tool-call turn here"
        ),
    )
    ap.add_argument("--timeout", type=int, default=900, help="per-request seconds")
    ap.add_argument(
        "--think",
        dest="think",
        action="store_true",
        default=None,
        help="send think:true (top level, never inside options -- see ERR-1004)",
    )
    ap.add_argument("--no-think", dest="think", action="store_false")
    ap.add_argument(
        "--out", default="benchmarks/results", help="directory for the JSON result"
    )
    ap.add_argument(
        "--keep-sandboxes", default=None, help="copy finished sandboxes here"
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=42,
        help="sampling seed sent with every request; --seed -1 disables (non-reproducible)",
    )
    ap.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="independent passes over the task set; seed is offset per pass",
    )
    ap.add_argument(
        "--allow-unsandboxed",
        action="store_true",
        help="proceed even if sandbox-exec confinement is unavailable",
    )
    args = ap.parse_args()

    tiers = [int(t) for t in args.tiers.split(",") if t.strip()]
    tasks = tasks_for_tiers(tiers)
    keep = Path(args.keep_sandboxes).expanduser() if args.keep_sandboxes else None
    if keep is not None:
        keep.mkdir(parents=True, exist_ok=True)

    confined = probe_sandbox()
    if confined:
        print("sandbox: sandbox-exec active (no network, writes confined)")
    else:
        print(
            "sandbox: UNAVAILABLE — model-authored code would run with this "
            "user's filesystem and network access"
        )
        if not args.allow_unsandboxed:
            print("refusing to run; pass --allow-unsandboxed to override")
            return 2

    base_seed = None if args.seed is not None and args.seed < 0 else args.seed
    print(
        f"model={args.model} tiers={tiers} tasks={len(tasks)} "
        f"think={args.think} seed={base_seed} repeats={args.repeats}"
    )
    provenance = collect_provenance(args.base, args.model)
    print(
        f"digest={str(provenance.get('model_digest'))[:19]} "
        f"ollama={provenance.get('ollama_version')} "
        f"params={provenance.get('effective_parameters')}"
    )

    unload(args.base, args.model)

    results: list[TaskResult] = []
    for repeat in range(args.repeats):
        seed = None if base_seed is None else base_seed + repeat
        if args.repeats > 1:
            print(f"\n--- pass {repeat + 1}/{args.repeats} (seed={seed}) ---")
        for task in tasks:
            print(f"\n[T{task.tier}] {task.task_id} — {task.title}")
            res = run_task(
                args.base,
                args.model,
                task,
                args.think,
                args.num_ctx,
                args.timeout,
                keep,
                seed,
                args.num_predict,
                repeat,
            )
            results.append(res)
            flag = "PASS" if res.solved else "FAIL"
            print(
                f"   {flag}  turns={res.turns}{'(cap)' if res.hit_turn_cap else ''} "
                f"calls={res.tool_calls} bad={res.bad_tool_calls} "
                f"tests_run={res.ran_tests} {res.wall_s}s"
            )
            if res.error:
                print(f"   error: {res.error}")
            if res.truncated_turns:
                print(
                    f"   ⚠ {res.truncated_turns} turn(s) hit the {args.num_predict}-token cap"
                )
            if res.tampered_suites:
                print(
                    f"   ⚠ rewrote fixture suite(s): {', '.join(res.tampered_suites)}"
                )
            if res.bad_tool_detail:
                print(f"   schema: {'; '.join(res.bad_tool_detail[:3])}")
            if not res.solved and res.verifier_output:
                print(f"   verifier: {res.verifier_output.splitlines()[-1][:160]}")

    unload(args.base, args.model)

    solved = sum(1 for r in results if r.solved)
    by_tier: dict[int, list[TaskResult]] = {}
    for r in results:
        by_tier.setdefault(r.tier, []).append(r)

    print(f"\n=== {args.model} ===")
    print(f"solved {solved}/{len(results)}")
    for tier in sorted(by_tier):
        rows = by_tier[tier]
        print(
            f"  T{tier}: {sum(1 for r in rows if r.solved)}/{len(rows)} solved, "
            f"{sum(r.bad_tool_calls for r in rows)}/{sum(r.tool_calls for r in rows)} bad calls, "
            f"{round(sum(r.wall_s for r in rows), 1)}s"
        )

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = out_dir / f"agentic-coding-{args.model.replace(':', '_')}-{stamp}.json"
    out_path.write_text(
        json.dumps(
            {
                "model": args.model,
                "tiers": tiers,
                "think": args.think,
                "num_ctx": args.num_ctx,
                "num_predict": args.num_predict,
                "seed": base_seed,
                "repeats": args.repeats,
                "sandboxed": confined,
                "provenance": provenance,
                "solved": solved,
                "total": len(results),
                "results": [asdict(r) for r in results],
            },
            indent=2,
        )
    )
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
