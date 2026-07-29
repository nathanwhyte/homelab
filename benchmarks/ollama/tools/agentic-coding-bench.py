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

⚠ OUTPUT BUDGET. `--num-predict` defaults to 16384, matching the 2026-07-28
matrix, and the reason is the same one that run recorded: at 2048 "the cap, not
the model, was setting the numbers". A first pass here at 4096 reproduced it —
`gemma4:coding-12b` failed all three of T2a/T3a/T3b with exactly one truncated
turn each, because a thinking model spends most of its budget reasoning and then
still has to emit an entire file through `write_file`. Any run where
`truncated_turns` is nonzero on a failed task is suspect: raise the cap and rerun
before reading anything into that model's score.

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
import os
import platform
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from coding_tasks import TASKS, CodingTask, tasks_for_tiers

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
    files_read: list[str] = field(default_factory=list)
    # Files the model wrote without ever having read them — counted once per
    # file (matching files_written semantics), not once per write event. T2 is
    # described as requiring cross-file investigation; without this the claim is
    # unfalsifiable, since a model can solve T2a by writing the right
    # normalize.py from the prompt alone.
    write_without_read: int = 0
    ran_tests: int = 0
    # Server-reported model load time on the first request of the task. Cold load
    # lands entirely on whichever task ran first, so wall_s is not comparable
    # across tiers unless this is separable.
    load_s: float = 0.0
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


# ⚠ WHAT THIS PROFILE DOES AND DOES NOT DO.
#
# It denies network access and denies writes outside the sandbox. It does NOT
# restrict reads: it opens with `allow default`, so model-authored code running
# here can read any file this user can read and hand it back through tool output.
# Call this network/write confinement, not execution isolation.
#
# A deny-by-default profile with reads allowlisted to the sandbox plus CPython's
# own prefix was attempted on 2026-07-28 and abandoned: CPython aborts with
# SIGABRT and no diagnostic under every allowlist tried (including /opt/homebrew,
# /usr, /System, /Library, /private/var/db and the dyld cache). sandbox-exec is
# deprecated and undocumented enough that chasing it further was not worth the
# time. `--strict-sandbox` re-attempts it and refuses to run if it still cannot
# boot, so the option is there when someone wants to finish the job.
#
# The practical exposure: fixtures here are trivial repos with no secrets, and
# the threat is a local model writing something silly rather than an attacker.
# Do not reuse this runner for untrusted third-party code on that basis.
_SANDBOX_PROFILE = """(version 1)
(allow default)
(deny network*)
(deny file-write*)
(allow file-write* (subpath "{sandbox}"))
(allow file-write-data (literal "/dev/null") (literal "/dev/dtracehelper"))
"""

_STRICT_SANDBOX_PROFILE = """(version 1)
(deny default)
(allow process* sysctl-read mach* signal ipc-posix-shm)
(deny network*)
(allow file-read* file-write* (subpath "{sandbox}"))
(allow file-read* (subpath "{python_prefix}"))
(allow file-read* (subpath "/usr") (subpath "/System") (subpath "/Library"))
(allow file-read* (subpath "/private/var/db"))
(allow file-read-metadata (subpath "/"))
(allow file-read* file-write-data (literal "/dev/null") (literal "/dev/urandom")
    (literal "/dev/random") (literal "/dev/dtracehelper"))
"""

# "strict" (reads confined too), "network-write" (the default profile), or
# "none". Recorded in every result so a score can be read alongside the boundary
# it ran under.
_SANDBOX_LEVEL = "none"
_STRICT_REQUESTED = False

# Resolved once by probe_sandbox(): True when sandbox-exec confinement works on
# this host, False when we had to fall back to plain execution.
_SANDBOX_AVAILABLE: bool | None = None


def _sandbox_command(sandbox: Path, argv: list[str]) -> list[str]:
    if not _SANDBOX_AVAILABLE:
        return argv
    template = _STRICT_SANDBOX_PROFILE if _STRICT_REQUESTED else _SANDBOX_PROFILE
    profile = template.format(
        sandbox=sandbox.resolve(),
        python_prefix=Path(sys.base_prefix).resolve(),
    )
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


def _apply_rlimits() -> None:
    """Bound CPU and file size for model-authored code.

    Applied best-effort and individually: macOS refuses some rlimits outright
    (RLIMIT_AS among them), and a raising preexec_fn kills the whole subprocess
    launch rather than merely skipping a limit.
    """
    for which, limit in (
        (resource.RLIMIT_CPU, (120, 120)),
        (resource.RLIMIT_FSIZE, (256 << 20, 256 << 20)),
        (resource.RLIMIT_NOFILE, (256, 256)),
    ):
        try:
            resource.setrlimit(which, limit)
        except (ValueError, OSError):
            pass


# Per-stream ceiling on how much child output is ever read back into harness
# memory. Output spools to files (RLIMIT_FSIZE bounds those); only these tails
# are loaded. The completion token is the last thing the verifier wrapper
# writes to stdout, so it always lands inside the stdout tail.
_CAPTURE_CEILING = 1 << 20


def _read_tail(fileobj: Any, limit: int) -> str:
    size = os.fstat(fileobj.fileno()).st_size
    fileobj.seek(max(0, size - limit))
    return fileobj.read().decode("utf-8", errors="replace")


def run_python(
    path: Path,
    script: str,
    timeout: int = 60,
    stdin_data: str | None = None,
    capture_limit: int | None = 2000,
) -> tuple[int, str]:
    """Run a python script inside the sandbox and return (returncode, output).

    Every child runs in its own session (`os.setsid`) so that the whole process
    group can be killed rather than only the direct child — code under test can
    spawn workers, and `subprocess.run`'s own timeout leaves those orphaned and
    running after this function returns. The group id is captured BEFORE the
    first wait: once `communicate()` reaps the direct child, `os.getpgid` on its
    pid no longer resolves, and same-group workers surviving a clean exit would
    leak (they did, until this was caught in review). A grandchild that itself
    calls `setsid()` starts a NEW session outside this group and can outlive the
    run; `killpg` cannot reach it, and defending against that is out of the
    non-adversarial threat model here.

    stdout/stderr spool to files in the sandbox rather than pipes, for two
    reasons found in the same review: `communicate()` on pipes buffers the
    ENTIRE output in harness memory before any truncation (an 8 MB print peaked
    ~24 MB in the harness, and nothing rlimits the harness), and a worker
    holding the inherited pipe ends kept `communicate()` blocked past its
    deadline. With files, the wait is on process exit alone and at most
    `_CAPTURE_CEILING` per stream is ever read back. The spool files are
    dot-prefixed, transient, and deleted before the tool loop can observe them.

    `stdin_data`, when set, is piped to the child's stdin (`run_verifier` uses
    this to hand over the completion token without it ever touching the sandbox
    filesystem). `capture_limit` slices the combined tail further; pass None for
    the full (still ceiling-bounded) tail when the caller must search it.
    """
    script_path = path / script
    # -s drops user site-packages; -E ignores inherited PYTHON* variables. NOT -I:
    # isolated mode also removes the script's own directory from sys.path, which
    # makes every `from stats import ...` in a verifier fail with
    # ModuleNotFoundError regardless of what the model wrote.
    argv = _sandbox_command(path, [sys.executable, "-s", "-E", str(script_path)])
    # Named files INSIDE the sandbox on purpose: the child certainly may write
    # there under every profile, whereas seatbelt's treatment of inherited fds
    # pointing outside the sandbox is undocumented.
    cap_out = tempfile.NamedTemporaryFile(dir=path, prefix=".cap-out-", delete=False)
    cap_err = tempfile.NamedTemporaryFile(dir=path, prefix=".cap-err-", delete=False)
    try:
        proc = subprocess.Popen(
            argv,
            cwd=path,
            stdin=subprocess.PIPE if stdin_data is not None else None,
            stdout=cap_out,
            stderr=cap_err,
            text=True,
            env=_minimal_env(path),
            preexec_fn=_apply_rlimits,  # noqa: PLW1509 - the point is to bound the child
            start_new_session=True,
        )
        # start_new_session makes the child the leader of a fresh group whose
        # id equals its pid. Retained here, while the pid is guaranteed live.
        pgid = proc.pid
        timed_out = False
        try:
            proc.communicate(input=stdin_data, timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            _kill_group(pgid)
            proc.communicate()
            code = 124
            timed_out = True
        finally:
            # Even on a clean exit, reap anything the script detached into the
            # group — by pgid, because proc may already be reaped.
            _kill_group(pgid)
        out = (
            _read_tail(cap_out, _CAPTURE_CEILING)
            + _read_tail(cap_err, _CAPTURE_CEILING)
        ).strip()
        if timed_out:
            out = (out + "\ntimeout").strip()
    finally:
        for cap in (cap_out, cap_err):
            cap.close()
            try:
                os.unlink(cap.name)
            except OSError:
                pass
    return code, out if capture_limit is None else out[-capture_limit:]


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_verifier(
    sandbox: Path, verifier_source: str, timeout: int = 120
) -> tuple[bool, str]:
    """Run a hidden verifier and report whether it ran to completion.

    Exit code alone is NOT sufficient evidence that a task was solved. A target
    module containing `raise SystemExit(0)` makes the verifier's very first
    import exit cleanly, before any assertion executes — every task "passes".
    Reproduced on 2026-07-28 against all six fixtures.

    So the verifier body runs under a wrapper that prints a per-run random token
    only after the body returns normally. A pass requires exit 0 AND that token.
    The token is random per invocation precisely because a fixed sentinel (the
    fixtures all end in `print("OK")`) is one the model could simply print
    itself.

    The token never touches the sandbox filesystem: it is piped to the wrapper
    over stdin, so `open('verify.py').read()` from in-sandbox code yields the
    wrapper's source, not the token value. Residual vector, accepted: a target
    that walks stack frames or `gc` for a `VERIFIED-` string while the wrapper
    runs could still recover it — that is out of the non-adversarial threat
    model, consistent with the sandbox honesty note above. The wrapper consumes
    only the first stdin line; a verifier body that reads stdin sees EOF (none
    of the fixtures do).
    """
    token = f"VERIFIED-{uuid.uuid4().hex}"
    (sandbox / VERIFIER_BODY).write_text(verifier_source)
    (sandbox / VERIFIER_ENTRY).write_text(
        "import runpy, sys\n"
        "tok = sys.stdin.readline().strip()\n"
        f"runpy.run_path({VERIFIER_BODY!r}, run_name='__main__')\n"
        "sys.stdout.write(tok)\n"
        "sys.stdout.flush()\n"
    )
    # The token check must run on the UNTRUNCATED output: with the default
    # 2000-char tail, >~2 KB of verifier stderr sliced the token out and flipped
    # a real pass to a FAIL. Truncation is applied to the display text below.
    code, out = run_python(
        sandbox,
        VERIFIER_ENTRY,
        timeout=timeout,
        stdin_data=token + "\n",
        capture_limit=None,
    )
    completed = code == 0 and token in out
    if code == 0 and token not in out:
        out = (
            "verifier exited 0 without completing — the task code interrupted it "
            "(e.g. SystemExit during import); scored as a failure.\n" + out
        )
    return completed, out.replace(token, "").strip()[-1200:]


def probe_sandbox(strict: bool = False) -> str:
    """Measure which confinement boundaries actually hold on this host.

    Returns "strict" (network, outside writes AND outside reads blocked),
    "network-write" (reads are NOT confined), or "none". Each boundary is probed
    rather than assumed, because a profile that boots CPython while leaving reads
    open is worse than no sandbox: it reads as safe in the results.
    """
    global _SANDBOX_AVAILABLE, _SANDBOX_LEVEL, _STRICT_REQUESTED
    _STRICT_REQUESTED = strict
    if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").exists():
        _SANDBOX_AVAILABLE = False
        _SANDBOX_LEVEL = "none"
        return "none"

    _SANDBOX_AVAILABLE = True
    probe_dir = Path(tempfile.mkdtemp(prefix="sandbox-probe-"))
    outside = Path(tempfile.mkdtemp(prefix="sandbox-outside-"))
    secret = outside / "secret.txt"
    secret.write_text("TOP-SECRET-PROBE-VALUE")
    try:
        # Each probe asserts one boundary. A profile that boots CPython but
        # leaves reads open is worse than no sandbox, because it reads as safe.
        (probe_dir / "probe.py").write_text(
            "import socket, pathlib\n"
            "pathlib.Path('wrote.txt').write_text('ok')\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 53), timeout=2)\n"
            "    print('NETWORK-REACHABLE')\n"
            "except Exception:\n"
            "    print('network-blocked')\n"
            # Loopback is probed separately because it is the self-help case
            # this design actually worries about: the Ollama server the harness
            # is scoring listens on localhost:11434, and "external addresses are
            # blocked" does not by itself prove the loopback path is.
            "try:\n"
            "    socket.create_connection(('127.0.0.1', 11434), timeout=2)\n"
            "    print('LOOPBACK-REACHABLE')\n"
            "except Exception:\n"
            "    print('loopback-blocked')\n"
            "try:\n"
            f"    print('READ-ESCAPED:' + pathlib.Path({str(secret)!r}).read_text())\n"
            "except Exception:\n"
            "    print('outside-read-blocked')\n"
            "try:\n"
            f"    pathlib.Path({str(outside / 'escaped.txt')!r}).write_text('x')\n"
            "    print('WRITE-ESCAPED')\n"
            "except Exception:\n"
            "    print('outside-write-blocked')\n"
        )
        code, out = run_python(probe_dir, "probe.py", timeout=30)
        if (
            code != 0
            or "network-blocked" not in out
            or "loopback-blocked" not in out
            or "outside-write-blocked" not in out
        ):
            _SANDBOX_AVAILABLE = False
            _SANDBOX_LEVEL = "none"
            print(f"sandbox probe failed (exit {code}): {out[:300]}")
            return "none"
        _SANDBOX_LEVEL = "strict" if "outside-read-blocked" in out else "network-write"
        return _SANDBOX_LEVEL
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)


VERIFIER_ENTRY = "verify.py"
VERIFIER_BODY = "_verify_body.py"
# write_file content cap. The tool runs in the harness's own process, which is
# not rlimited, so an adversarial or malformed call could otherwise hand over an
# unbounded string. No legitimate fixture file is within orders of magnitude.
MAX_WRITE_CHARS = 8 << 20

# Names the scoring machinery owns. A model that could write these could rewrite
# its own grader.
RESERVED_FILENAMES = {
    VERIFIER_ENTRY,
    VERIFIER_BODY,
    "sitecustomize.py",
    "usercustomize.py",
}


def resolve_repo_path(sandbox: Path, raw: str) -> tuple[Path | None, str | None]:
    """Resolve a model-supplied path, or explain why it is not usable.

    Rejects rather than silently collapses. An earlier version took the basename
    of whatever arrived, so `../../stats.py` quietly became `stats.py` and
    "succeeded" — which hides a real mistake from the model and from the score.
    """
    if not raw or raw != raw.strip():
        return None, "path must be a non-empty repository-relative filename"
    if "/" in raw or "\\" in raw or raw in {".", ".."}:
        return None, f"{raw!r} is not a repository-relative filename (no directories)"
    if raw in RESERVED_FILENAMES:
        return None, f"{raw} is reserved by the harness and cannot be accessed"
    return sandbox / raw, None


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
        target, problem = resolve_repo_path(sandbox, args["path"])
        if problem is not None:
            return f"error: {problem}"
        if not target.exists():
            return f"error: no such file: {args['path']}"
        return target.read_text()
    if name == "write_file":
        target, problem = resolve_repo_path(sandbox, args["path"])
        if problem is not None:
            return f"error: {problem}"
        if len(args["content"]) > MAX_WRITE_CHARS:
            return (
                f"error: content is {len(args['content'])} characters; "
                f"the limit is {MAX_WRITE_CHARS}"
            )
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
            # First nonzero wins: the cold load lands on the task's first
            # request, and a later warm turn's small load_duration must not
            # overwrite it. (The previous or-order did exactly that.)
            result.load_s = result.load_s or round(
                (body.get("load_duration") or 0) / 1e9, 2
            )
            message = body.get("message") or {}
            calls = message.get("tool_calls") or []
            content = message.get("content") or ""

            if not calls:
                if looks_like_text_tool_call(content):
                    result.text_tool_attempts += 1
                break

            # Append the assistant message AS RETURNED, not a reconstruction.
            # Rebuilding it from content+tool_calls silently drops `thinking`,
            # which Ollama's own agent loop preserves across turns. For the
            # native-thinking primary matrix that changes the multi-turn context
            # every model sees, and it need not change it equally for all of them.
            messages.append(message)

            for call in calls:
                # A malformed envelope (non-dict call, or a `function` that is
                # not an object) is a bad tool call to record, not an
                # AttributeError to crash the task on.
                fn = call.get("function") if isinstance(call, dict) else None
                if not isinstance(fn, dict):
                    result.tool_calls += 1
                    result.bad_tool_calls += 1
                    if len(result.bad_tool_detail) < 10:
                        result.bad_tool_detail.append(
                            f"malformed tool-call envelope: {str(call)[:60]}"
                        )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_name": "",
                            "content": "error: malformed tool-call envelope",
                        }
                    )
                    continue
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

                output = execute_tool(sandbox, name, args, fixture_suites)
                rejected = output.startswith("error:")

                # Accounting AFTER execution: a write that the harness refused
                # (reserved name, directory component) is not a write, and
                # recording it as one overstates what the model accomplished.
                if not rejected:
                    if name == "write_file":
                        written = Path(args["path"]).name
                        result.wrote_any_file = True
                        if written not in result.files_written:
                            result.files_written.append(written)
                            # T2 claims to require investigation; this is what
                            # makes that claim checkable rather than assumed.
                            # Inside the uniqueness guard on purpose: repeated
                            # blind writes to one target count once.
                            if written not in result.files_read:
                                result.write_without_read += 1
                    elif name == "read_file":
                        read = Path(args["path"]).name
                        if read not in result.files_read:
                            result.files_read.append(read)
                    elif name == "run_tests":
                        result.ran_tests += 1

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

        result.solved, result.verifier_output = run_verifier(sandbox, task.verifier)

        for name in task.target_files:
            path = sandbox / name
            if path.exists():
                result.final_sources[name] = path.read_text()
    finally:
        if keep_sandbox is not None:
            dest = keep_sandbox / f"{_fs_name(model)}-{task.task_id}-r{repeat}"
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(sandbox, dest)
        shutil.rmtree(sandbox, ignore_errors=True)

    return result


def _fs_name(model: str) -> str:
    """A model tag flattened to a single path component.

    Replacing only `:` was not enough: registry-style ids like
    `namespace/model:tag` (already used elsewhere in this repo) turned the
    output filename into a nested, nonexistent path — raising only AFTER the
    whole run had completed, losing the result.
    """
    return model.replace("/", "_").replace(":", "_")


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


def preflight(base: str, model: str) -> list[str]:
    """Refuse to produce a "result" the server never participated in.

    Against a refused port the loop happily records a transport error per task,
    writes a JSON file, and exits 0 — so a wrapper script reports "matrix
    complete" for rows that never ran. Reachability, the tag's existence, and its
    tool-calling capability are all cheap to check up front, and all three are
    fatal.
    """
    problems: list[str] = []
    version = _get_json(f"{base}/api/version")
    if not version.get("version"):
        return [f"{base} is unreachable: {version.get('error', 'no version returned')}"]

    show = _get_json(f"{base}/api/show", {"model": model})
    if show.get("error"):
        problems.append(f"model {model!r} not available: {show['error']}")
        return problems
    if "tools" not in (show.get("capabilities") or []):
        problems.append(
            f"model {model!r} does not advertise the `tools` capability; "
            "an agentic-coding score would measure nothing"
        )
    return problems


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
        default=16384,
        help=(
            "per-turn output cap. Must be generous enough that the cap never "
            "sets the score: a thinking model spends most of a turn reasoning "
            "and then still has to emit a whole file through write_file"
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
        "--strict-sandbox",
        action="store_true",
        help="require read confinement too, and refuse to run without it",
    )
    ap.add_argument(
        "--allow-unsandboxed",
        action="store_true",
        help="proceed even if sandbox-exec confinement is unavailable",
    )
    args = ap.parse_args()

    tiers = [int(t) for t in args.tiers.split(",") if t.strip()]
    tasks = tasks_for_tiers(tiers)
    # An empty selection must refuse, not exit 0: `--tiers 4` (or `--tiers ""`)
    # otherwise produces a clean 0/0 run that a wrapper reads as "complete" —
    # the same row-that-never-ran class the exit codes 2/3/4 exist to close.
    if not tasks:
        valid = ",".join(str(t) for t in sorted({t.tier for t in TASKS}))
        print(
            f"--tiers {args.tiers!r} selects no tasks; valid tiers are {valid}. "
            "refusing to run — an empty run is not a result"
        )
        return 2
    # Same class of hole from the other direction: --repeats 0 ran nothing,
    # wrote an empty result, and exited 0 — which the matrix wrapper reads as
    # every row "ok".
    if args.repeats < 1:
        print(
            f"--repeats {args.repeats} runs nothing; it must be >= 1. "
            "refusing to run — an empty run is not a result"
        )
        return 2
    keep = Path(args.keep_sandboxes).expanduser() if args.keep_sandboxes else None
    if keep is not None:
        keep.mkdir(parents=True, exist_ok=True)

    sandbox_level = probe_sandbox(strict=args.strict_sandbox)
    # An explicit strict ask is checked FIRST: --allow-unsandboxed must never
    # downgrade it. (Previously the "none" branch let that combination run with
    # no confinement at all, silently.)
    if args.strict_sandbox and sandbox_level != "strict":
        print(
            f"--strict-sandbox requested but only {sandbox_level!r} is achievable; "
            "refusing (--allow-unsandboxed does not override an explicit strict ask)"
        )
        return 2
    if sandbox_level == "strict":
        print("sandbox: strict (network, outside writes AND outside reads blocked)")
    elif sandbox_level == "network-write":
        print(
            "sandbox: network + outside writes blocked; READS ARE NOT CONFINED — "
            "code under test can read any file this user can read"
        )
    else:
        print(
            "sandbox: UNAVAILABLE — model-authored code would run with this "
            "user's filesystem and network access"
        )
        if not args.allow_unsandboxed:
            print("refusing to run; pass --allow-unsandboxed to override")
            return 2

    problems = preflight(args.base, args.model)
    if problems:
        for problem in problems:
            print(f"preflight: {problem}")
        print(
            "refusing to run — a benchmark against an unreachable server is not a result"
        )
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
            try:
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
            except Exception as e:  # noqa: BLE001 - one task's crash must cost one row, not the batch
                # Results are only written after the full loop, so an uncaught
                # exception here would lose every completed row of this tag's
                # run. Record the crash the same way a transport error is
                # recorded and keep going.
                res = TaskResult(
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
                    error=f"harness crash: {type(e).__name__}: {e}"[:300],
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
    out_path = out_dir / f"agentic-coding-{_fs_name(args.model)}-{stamp}.json"
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
                "sandbox_level": sandbox_level,
                "provenance": provenance,
                "solved": solved,
                "total": len(results),
                "results": [asdict(r) for r in results],
            },
            indent=2,
        )
    )
    print(f"\nwrote {out_path}")

    # A run where every task died in transport is infrastructure failure, not a
    # score of zero. Exiting 0 there is what let the matrix wrapper claim
    # "complete" for rows that never ran.
    transport_failures = sum(1 for r in results if r.error)
    if transport_failures == len(results) and results:
        print(
            f"ALL {len(results)} task(s) failed at transport; this run is not a result"
        )
        return 3
    if transport_failures:
        print(f"⚠ {transport_failures}/{len(results)} task(s) failed at transport")
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
