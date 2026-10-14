"""Replay OpenViking's semantic pipeline against a Claude model (IMPR-1311 round 1).

For each sampled directory under `--root`, runs OpenViking 0.4.20's two stages the
way the semantic processor does: `semantic.document_summary` per file, then
`semantic.overview_generation` over those summaries. Records tokens, latency, 429s
and the `anthropic-ratelimit-*` headers, and writes each new overview next to the
stored one. Read-only against OpenViking; the API key is read from a file and never
printed. Outputs carry real vault content: write them outside any repo.

Claude 5 models think by default; pass `--no-thinking` to measure what a production
config with `thinking: disabled` would spend.
"""

import argparse
import asyncio
import json
import random
import statistics
import subprocess
import time
from pathlib import Path

import httpx
from openviking.prompts.manager import render_prompt
from pricing import call_cost, run_cost  # sibling module

API = "https://api.anthropic.com/v1/messages"
MODEL, ROOT = "claude-haiku-5-5", "viking://resources/compendium/"  # --model, --root
MAX_TOKENS, NO_THINKING = 4096, False  # --max-tokens, --no-thinking
WEEKLY_VLM_CALLS = (
    15002  # increase(openviking_model_calls_total{model_type="vlm"}[7d]), 2026-10-13
)


def ov_json(*args: str):
    out = subprocess.run(
        ["ov", *args, "-o", "json"], capture_output=True, text=True, check=True
    ).stdout
    return json.loads(out[out.index("{") :])["result"]


def walk() -> list[tuple[str, list[dict]]]:
    dirs, frontier = [], [ROOT]
    while frontier:
        uri = frontier.pop()
        kids = ov_json("ls", uri)
        if any(not k["isDir"] and k["uri"].endswith(".md") for k in kids):
            dirs.append((uri, kids))
        frontier += [k["uri"] for k in kids if k["isDir"]]
    return dirs


def pick(dirs, n_random: int, n_largest: int, rng: random.Random):
    nfiles = lambda d: sum(not k["isDir"] for k in d[1])
    largest = sorted(dirs, key=nfiles, reverse=True)[:n_largest]
    rest = [d for d in dirs if d not in largest]
    return largest + rng.sample(rest, min(n_random, len(rest)))


class Api:
    def __init__(self, headers: dict, concurrency: int):
        self.headers, self.sem = headers, asyncio.Semaphore(concurrency)
        self.client = httpx.AsyncClient(timeout=300)
        self.recs: list[dict] = []
        self.throttled = 0

    async def complete(self, kind: str, uri: str, prompt: str) -> str:
        body = {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
        if NO_THINKING:
            body["thinking"] = {"type": "disabled"}
        for attempt in range(6):
            async with self.sem:
                t0 = time.monotonic()
                r = await self.client.post(API, headers=self.headers, json=body)
                dt = time.monotonic() - t0
            rl = {
                k: v
                for k, v in r.headers.items()
                if k.startswith("anthropic-ratelimit") or k == "retry-after"
            }
            if r.status_code in (429, 529):
                self.throttled += 1
                self.recs.append(
                    {"kind": kind, "uri": uri, "status": r.status_code, "ratelimit": rl}
                )
                await asyncio.sleep(float(r.headers.get("retry-after", 2**attempt)))
                continue
            data = r.json()
            rec = {
                "kind": kind,
                "uri": uri,
                "status": r.status_code,
                "latency_s": round(dt, 2),
                "ratelimit": rl,
            }
            if r.status_code != 200:
                rec["error"] = data.get("error")
                self.recs.append(rec)
                raise RuntimeError(f"{r.status_code}: {data.get('error')}")
            text = "".join(b.get("text", "") for b in data["content"])
            rec |= {
                "input_tokens": data["usage"]["input_tokens"],
                "output_tokens": data["usage"]["output_tokens"],
                "stop_reason": data["stop_reason"],
            }
            self.recs.append(rec)
            return text.strip()
        raise RuntimeError(f"gave up after retries: {uri}")


async def run_dir(api: Api, uri: str, kids: list[dict], batch_size: int) -> dict:
    files = [k for k in kids if not k["isDir"]][:batch_size]
    subdirs = [k for k in kids if k["isDir"]]

    async def summarize(f):
        name = f["uri"].rsplit("/", 1)[-1]
        content = await asyncio.to_thread(ov_json, "read", f["uri"])
        prompt = render_prompt(
            "semantic.document_summary",
            {"file_name": name, "content": content, "output_language": "en"},
        )
        return name, await api.complete("document_summary", f["uri"], prompt)

    # Await every summary before failing, so no billed response is still in flight
    # when the caller reports and closes the client.
    results = await asyncio.gather(
        *(summarize(f) for f in files), return_exceptions=True
    )
    failed = next((r for r in results if isinstance(r, BaseException)), None)
    if failed is not None:
        raise failed
    summaries = results
    file_lines = [
        f"- {n} (link: viking://input_sample_f{i}): {s}"
        for i, (n, s) in enumerate(summaries, 1)
    ]
    child_lines = [
        f"- {k['uri'].rsplit('/', 1)[-1]}/: {k.get('abstract', '')}" for k in subdirs
    ]
    prompt = render_prompt(
        "semantic.overview_generation",
        {
            "dir_name": uri.rstrip("/").rsplit("/", 1)[-1],
            "file_summaries": "\n".join(file_lines) or "None",
            "children_abstracts": "\n".join(child_lines) or "None",
            "output_language": "en",
            "directory_coverage": "",
        },
    )
    overview = await api.complete("overview_generation", uri, prompt)
    try:
        baseline = await asyncio.to_thread(ov_json, "overview", uri)
    except subprocess.CalledProcessError:
        baseline = ""
    return {"uri": uri, "files": len(files), "overview": overview, "baseline": baseline}


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def report(api: Api, wall_s: float):
    ok = [r for r in api.recs if r["status"] == 200]
    print(
        f"calls ok={len(ok)} throttled(429/529)={api.throttled} other_errors={sum(r['status'] not in (200, 429, 529) for r in api.recs)} wall={wall_s:.0f}s rpm={len(ok) / wall_s * 60:.0f}"
    )
    for kind in sorted({r["kind"] for r in ok}):
        rs = [r for r in ok if r["kind"] == kind]
        tin, tout, lat = (
            [r[k] for r in rs] for k in ("input_tokens", "output_tokens", "latency_s")
        )
        print(
            f"{kind:20} n={len(rs):4} in mean={statistics.mean(tin):6.0f} max={max(tin):6}  "
            f"out mean={statistics.mean(tout):4.0f} max={max(tout):5}  "
            f"latency p50={pct(lat, 0.5)}s p90={pct(lat, 0.9)}s  stops={sorted({r['stop_reason'] for r in rs})}"
        )
    cost = run_cost(MODEL, [(r["input_tokens"], r["output_tokens"]) for r in ok])
    if ok and cost is None:
        print(f"run cost: no price table for {MODEL} (add it to pricing.py)")
    elif ok:
        per_call = cost / len(ok)
        monthly = WEEKLY_VLM_CALLS / 7 * 30
        print(
            f"run cost ${cost:.3f}  mean cost/call ${per_call:.5f}  projected {monthly:.0f} calls/month -> ${per_call * monthly:.0f}"
        )
        mo = sum(r["output_tokens"] for r in ok) / len(ok)
        print(
            f"  same output, IMPR-1303's 9.4K-token mean prompt -> ${call_cost(MODEL, 9400, mo) * monthly:.0f}/month"
        )
    last = next((r["ratelimit"] for r in reversed(api.recs) if r["ratelimit"]), {})
    print("rate-limit headers (last response):")
    for k, v in sorted(last.items()):
        print(f"  {k}: {v}")


async def main():
    global MODEL, ROOT, MAX_TOKENS, NO_THINKING
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-file", required=True, type=Path)
    ap.add_argument("--workspace-id")
    ap.add_argument("--random-dirs", type=int, default=4)
    ap.add_argument("--largest-dirs", type=int, default=2)
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--seed", type=int, default=1311)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--root", default=ROOT, help="OV directory tree to sample from")
    ap.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    ap.add_argument(
        "--no-thinking", action="store_true", help="send thinking: disabled"
    )
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    MODEL, ROOT, MAX_TOKENS, NO_THINKING = a.model, a.root, a.max_tokens, a.no_thinking
    a.out.mkdir(parents=True, exist_ok=True)

    chosen = pick(walk(), a.random_dirs, a.largest_dirs, random.Random(a.seed))
    print(
        "dirs:",
        ", ".join(
            f"{u[len(ROOT) :]}({sum(not k['isDir'] for k in ks)})" for u, ks in chosen
        ),
    )

    headers = {
        "x-api-key": a.key_file.read_text().strip(),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if a.workspace_id:
        headers["anthropic-workspace-id"] = a.workspace_id
    api = Api(headers, a.concurrency)
    t0 = time.monotonic()
    results = await asyncio.gather(
        *(run_dir(api, u, ks, a.batch_size) for u, ks in chosen), return_exceptions=True
    )
    wall = time.monotonic() - t0
    await api.client.aclose()

    with (a.out / "results.jsonl").open("w") as fh:
        for r in api.recs:
            fh.write(json.dumps(r) + "\n")
    with (a.out / "overviews_side_by_side.md").open("w") as fh:
        for r in results:
            if isinstance(r, Exception):
                fh.write(f"## failed\n\n{r}\n\n")
                continue
            fh.write(
                f"## {r['uri']} ({r['files']} files)\n\n### stored\n\n{r['baseline']}\n\n### {MODEL}\n\n{r['overview']}\n\n"
            )
    for r in results:
        if isinstance(r, Exception):
            print("dir failed:", r)
    report(api, wall)


if __name__ == "__main__":
    asyncio.run(main())
