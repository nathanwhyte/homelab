"""Replay OpenViking's Working Memory (WM) generation against a Claude model (IMPR-1311).

Picks real sessions with at least two archives, takes archive N-1's stored WM as the
prior and archive N's messages as the new turns, and renders the prompt with
OpenViking 0.4.20's own formatter:

* default: `compression.ov_wm_v2_update` with the `update_working_memory` tool forced,
  as the session commit's update branch does. Records whether the tool call validates
  against the schema and against OV's tolerant parser.
* `--create`: `compression.ov_wm_v2` as a plain completion with the prior WM inlined,
  the path production runs while homelab's `ov_wm_update_patch.py` skips the update
  call. Checks all seven WM sections and writes each reply beside the stored WM.

Records tokens (with `thinking_tokens`), latency and cost. Read-only against
OpenViking; the key is read from a file and never printed. Outputs carry real session
content: write them outside any repo.
"""

import argparse
import asyncio
import itertools
import json
import random
import statistics
import subprocess
import time
from collections import Counter
from pathlib import Path

import httpx
from openviking.message.message import Message
from openviking.prompts.manager import render_prompt
from openviking.session.session import WM_SEVEN_SECTIONS, WM_UPDATE_TOOL, Session
from pricing import run_cost  # sibling module; the script's directory is on sys.path

try:
    import jsonschema
except ImportError:
    jsonschema = None

API = "https://api.anthropic.com/v1/messages"
MODEL, SESSIONS = (
    "claude-haiku-5-5",
    "viking://user/noot/sessions/",
)  # --model, --sessions
MAX_TOKENS, NO_THINKING = 8192, False  # set from --max-tokens / --no-thinking


def ov(*args: str, json_out: bool = True):
    cmd = ["ov", *args] + (["-o", "json"] if json_out else [])
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return json.loads(out[out.index("{") :])["result"]


def candidates(scan: int, rng: random.Random) -> list[tuple[str, str, str]]:
    sessions = [s["uri"] for s in ov("ls", SESSIONS) if s["isDir"]]
    rng.shuffle(sessions)
    found = []
    for s in sessions[:scan]:
        try:
            archives = sorted(a["uri"] for a in ov("ls", f"{s}/history") if a["isDir"])
        except subprocess.CalledProcessError:
            continue
        for prev, cur in itertools.pairwise(archives):
            found.append((s, prev, cur))
    return found


def build(prev: str, cur: str, create: bool) -> dict | None:
    prior = ov("overview", prev)
    if not any(f"## {s}" in prior for s in WM_SEVEN_SECTIONS):
        return None  # legacy or missing WM: OpenViking would take the creation branch
    raw = ov("read", f"{cur}/messages.jsonl")
    msgs = [
        Message.from_dict(json.loads(line)) for line in raw.splitlines() if line.strip()
    ]
    formatted = Session._format_messages_for_wm(msgs, [])
    if create:
        # What production runs while ov_wm_update_patch.py skips the update call:
        # _fallback_generate_wm_creation, a plain completion with the prior WM.
        prompt = render_prompt(
            "compression.ov_wm_v2",
            {
                "messages": formatted,
                "latest_archive_overview": prior,
                "checkpoint_instructions": "",
                "output_language": "en",
            },
        )
        return {
            "archive": cur,
            "prompt": prompt,
            "messages": len(msgs),
            "baseline": ov("overview", cur),
        }
    prompt = render_prompt(
        "compression.ov_wm_v2_update",
        {
            "messages": formatted,
            "latest_archive_overview": prior,
            "wm_section_reminders": Session._build_wm_section_reminders(prior),
            "checkpoint_instructions": "",
            "output_language": "en",
        },
    )
    return {"archive": cur, "prompt": prompt, "messages": len(msgs)}


def sections_missing(text: str) -> list[str]:
    """Canonical WM sections OV's own parser does not find as exact `## <title>` headers."""
    headers = Session._parse_wm_sections(text)
    return [s for s in WM_SEVEN_SECTIONS if f"## {s}" not in headers]


def ov_update_outcome(args) -> str:
    """What OV 0.4.20's update branch does with these tool arguments.

    Mirrors `_generate_archive_summary_async`: a `sections` object or all seven keys
    at the top level are merged as ops; anything else goes to the regex recovery,
    which merges whatever sections it salvages; with nothing salvaged OV falls back
    to the creation prompt (a second full generation).
    """
    if isinstance(args, dict):
        if isinstance(args.get("sections"), dict):
            return "merged"
        if all(k in args for k in WM_SEVEN_SECTIONS):
            return "merged"
    raw = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
    salvaged = Session._wm_recover_ops_from_raw(raw)
    if salvaged:
        return f"salvaged {len(salvaged)}/{len(WM_SEVEN_SECTIONS)}"
    return "creation fallback"


def validate(args) -> str:
    if not isinstance(args, dict):
        return "not an object"
    schema = WM_UPDATE_TOOL["function"]["parameters"]
    if jsonschema:
        try:
            jsonschema.validate(args, schema)
        except jsonschema.ValidationError as e:
            return f"schema: {e.message[:120]}"
        return "valid"
    missing = [s for s in WM_SEVEN_SECTIONS if s not in args.get("sections", {})]
    return f"missing {missing}" if missing else "valid (required keys only)"


async def call(
    client, headers, job, sem, strict: bool = False, create: bool = False
) -> dict:
    body = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": job["prompt"]}],
    }
    if NO_THINKING:
        body["thinking"] = {"type": "disabled"}
    if not create:
        fn = WM_UPDATE_TOOL["function"]
        tool = {
            "name": fn["name"],
            "description": fn["description"],
            "input_schema": fn["parameters"],
        }
        if strict:
            tool["strict"] = True
        body |= {"tools": [tool], "tool_choice": {"type": "tool", "name": fn["name"]}}
    async with sem:
        t0 = time.monotonic()
        r = await client.post(API, headers=headers, json=body)
        dt = time.monotonic() - t0
    rec = {
        "archive": job["archive"],
        "messages": job["messages"],
        "status": r.status_code,
        "latency_s": round(dt, 2),
    }
    data = r.json()
    if r.status_code != 200:
        rec["error"] = data.get("error")
        return rec
    rec |= {
        "input_tokens": data["usage"]["input_tokens"],
        "output_tokens": data["usage"]["output_tokens"],
        "stop_reason": data["stop_reason"],
        # output_tokens_details may be absent or null.
        "thinking_tokens": (data["usage"].get("output_tokens_details") or {}).get(
            "thinking_tokens", 0
        ),
    }
    if create:
        text = "".join(b.get("text", "") for b in data["content"]).strip()
        missing = sections_missing(text)
        rec |= {
            "verdict": f"missing {missing}" if missing else "all seven sections",
            "text": text,
            "baseline": job["baseline"],
            "baseline_missing": sections_missing(job["baseline"]),
        }
        return rec
    tool = next((b for b in data["content"] if b.get("type") == "tool_use"), None)
    rec |= {
        "verdict": validate(tool["input"]) if tool else "no tool_use block",
        "ov_outcome": ov_update_outcome(tool["input"]) if tool else "creation fallback",
        "tool_input": tool["input"] if tool else None,
    }
    return rec


async def main():
    global MAX_TOKENS, NO_THINKING, MODEL, SESSIONS
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-file", required=True, type=Path)
    ap.add_argument("--workspace-id", required=True)
    ap.add_argument("--samples", type=int, default=12)
    ap.add_argument("--scan", type=int, default=80)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--seed", type=int, default=1311)
    ap.add_argument(
        "--strict", action="store_true", help="send the tool with strict: true"
    )
    ap.add_argument(
        "--create",
        action="store_true",
        help="replay the creation prompt (the path the skip patch forces)",
    )
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument(
        "--no-thinking", action="store_true", help="send thinking: disabled"
    )
    ap.add_argument("--model", default=MODEL)
    ap.add_argument(
        "--sessions", default=SESSIONS, help="OV sessions root to sample from"
    )
    ap.add_argument("--tag", default="", help="suffix for the output files")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    MAX_TOKENS, NO_THINKING, MODEL, SESSIONS = (
        a.max_tokens,
        a.no_thinking,
        a.model,
        a.sessions,
    )
    a.out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(a.seed)

    pairs = candidates(a.scan, rng)
    rng.shuffle(pairs)
    jobs, legacy = [], 0
    for _, prev, cur in pairs:
        if len(jobs) == a.samples:
            break
        job = build(prev, cur, a.create)
        if job is None:
            legacy += 1
        else:
            jobs.append(job)
    print(
        f"archive pairs found {len(pairs)}; built {len(jobs)} update prompts; skipped {legacy} without a v2 prior"
    )

    headers = {
        "x-api-key": a.key_file.read_text().strip(),
        "anthropic-workspace-id": a.workspace_id,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    sem = asyncio.Semaphore(a.concurrency)
    async with httpx.AsyncClient(timeout=300) as client:
        recs = await asyncio.gather(
            *(call(client, headers, j, sem, a.strict, a.create) for j in jobs)
        )
    suffix = ("_create" if a.create else "_strict" if a.strict else "") + a.tag
    with (a.out / f"memory_results{suffix}.jsonl").open("w") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    if a.create:
        with (a.out / f"memory_create_side_by_side{a.tag}.md").open("w") as fh:
            for r in recs:
                if r["status"] == 200:
                    fh.write(
                        f"# {r['archive']}\n\n## stored\n\n{r['baseline']}\n\n## {MODEL}\n\n{r['text']}\n\n"
                    )
        print(
            "stored baselines missing sections:",
            sum(bool(r.get("baseline_missing")) for r in recs if r["status"] == 200),
        )

    ok = [r for r in recs if r["status"] == 200]
    print(
        f"calls {len(recs)} ok {len(ok)} errors {[(r['status'], r.get('error')) for r in recs if r['status'] != 200]}"
    )
    if not ok:
        return
    tin, tout, lat = (
        [r[k] for r in ok] for k in ("input_tokens", "output_tokens", "latency_s")
    )
    lat.sort()
    print(
        f"input tokens mean={statistics.mean(tin):.0f} max={max(tin)} over100K={sum(t > 100_000 for t in tin)}"
    )
    print(
        f"output tokens mean={statistics.mean(tout):.0f} max={max(tout)}  stops={sorted({r['stop_reason'] for r in ok})}"
    )
    print(f"latency p50={lat[len(lat) // 2]}s max={lat[-1]}s")
    print("verdicts:", dict(Counter(r["verdict"] for r in ok)))
    if not a.create:
        print("OV 0.4.20 update branch:", dict(Counter(r["ov_outcome"] for r in ok)))
    cost = run_cost(MODEL, [(r["input_tokens"], r["output_tokens"]) for r in ok])
    if cost is None:
        print(f"run cost: no price table for {MODEL} (add it to pricing.py)")
    else:
        print(f"run cost ${cost:.3f}  mean cost/call ${cost / len(ok):.5f}")


if __name__ == "__main__":
    asyncio.run(main())
