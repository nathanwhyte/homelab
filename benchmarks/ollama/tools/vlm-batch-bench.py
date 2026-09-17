#!/usr/bin/env python3
"""VLM-only num_batch benchmark: does num_batch change summarize throughput? (IDEA-1105)

Runs OpenViking-shaped summarize requests (one rendered `semantic.document_summary`
prompt per request, /api/chat, no FIM load) against temporary tags built from
BASE_TAG with an explicit `num_batch`, across concurrency levels and input-size
buckets. No FIM probes: this isolates the VLM side.

Matrix: every bucket x num_batch x concurrency. Within each bucket the num_batch
levels run in ABBA order (A B B A for two levels), so drift and GPU heat do not
favour one level; each cell is therefore measured in two passes, and its
REQUESTS_PER_PASS requests per pass are split across `concurrency` workers.

Inputs (--inputs) are a JSON object {bucket: [{"label", "prompt", ...}, ...]}.
Prompts are salted per request so the prefix cache never replays. Keep every
prompt under the runner's num_ctx: Ollama rejects an over-length prompt with
HTTP 400 rather than truncating it, and any failed request fails the run.

The committed summary (--out) holds aggregates only: no prompt or response text
(inputs may be private vault content). --samples writes one response per cell to
a separate path, for a local quality check.

Temporary tags are deleted and FIM_TAG is re-pinned (keep_alive -1) on exit,
pass or fail. Loading any benchmark tag evicts FIM (OLLAMA_MAX_LOADED_MODELS=1).

Usage:
  vlm-batch-bench.py --inputs inputs.json --out results.json [--samples samples.json]
      [--host http://192.168.1.19:11434] [--base-tag deepseek-coder-v2:instruct]
      [--fim-tag deepseek-coder-v2:fim] [--batches 512,2048] [--concurrency 1,2,3]
      [--requests-per-pass 10] [--num-predict 256]
"""

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request


def api(host, path, body=None, method=None, timeout=600):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        host + path,
        data=data,
        method=method or ("POST" if data is not None else "GET"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def one_request(host, tag, prompt, num_predict):
    body = {
        "model": tag,
        "messages": [
            {"role": "user", "content": f"<!-- {time.time_ns()} -->\n{prompt}"}
        ],
        "stream": False,
        "options": {"num_predict": num_predict, "temperature": 0.2},
        "keep_alive": "15m",
    }
    t0 = time.perf_counter()
    try:
        d = api(host, "/api/chat", body)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        detail = (
            exc.read().decode(errors="replace")[:200] if hasattr(exc, "read") else ""
        )
        return {"error": f"{type(exc).__name__}: {exc} {detail}".strip()}
    if d.get("error"):
        return {"error": str(d["error"])}
    return {
        "wall": time.perf_counter() - t0,
        "prompt_tokens": d.get("prompt_eval_count"),
        "prefill_tps": d.get("prompt_eval_count", 0)
        / max(d.get("prompt_eval_duration", 1) / 1e9, 1e-9),
        "prefill_s": d.get("prompt_eval_duration", 0) / 1e9,
        "eval_tokens": d.get("eval_count"),
        "decode_tps": d.get("eval_count", 0)
        / max(d.get("eval_duration", 1) / 1e9, 1e-9),
        "text": (d.get("message") or {}).get("content", ""),
    }


def run_pass(host, tag, prompts, concurrency, n_requests, num_predict):
    """n_requests split across `concurrency` workers; returns (results, pass_wall)."""
    lock = threading.Lock()
    counter = {"next": 0}
    results = []

    def worker():
        while True:
            with lock:
                i = counter["next"]
                if i >= n_requests:
                    return
                counter["next"] += 1
            res = one_request(host, tag, prompts[i % len(prompts)], num_predict)
            with lock:
                results.append(res)

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, time.perf_counter() - t0


def summarize(results, pass_walls):
    ok = [r for r in results if "error" not in r]
    errors = [r["error"] for r in results if "error" in r]
    row = {
        "requests": len(results),
        "errors": len(errors),
        "error_samples": errors[:3],
        "valid": not errors and bool(ok),
    }
    if ok:
        words = [len(r["text"].split()) for r in ok]
        row.update(
            {
                "prompt_tokens_p50": statistics.median(r["prompt_tokens"] for r in ok),
                "prefill_s_p50": statistics.median(r["prefill_s"] for r in ok),
                "prefill_tps_p50": statistics.median(r["prefill_tps"] for r in ok),
                "decode_tps_p50": statistics.median(r["decode_tps"] for r in ok),
                "wall_p50": statistics.median(r["wall"] for r in ok),
                "wall_p95": q([r["wall"] for r in ok], 0.95),
                "abstracts_per_min": 60 * len(ok) / sum(pass_walls),
                "words_p50": statistics.median(words),
                "empty_outputs": sum(1 for w in words if w == 0),
            }
        )
    return row


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--inputs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--samples")
    ap.add_argument("--host", default="http://192.168.1.19:11434")
    ap.add_argument("--base-tag", default="deepseek-coder-v2:instruct")
    ap.add_argument("--fim-tag", default="deepseek-coder-v2:fim")
    ap.add_argument("--batches", default="512,2048")
    ap.add_argument("--concurrency", default="1,2,3")
    ap.add_argument("--requests-per-pass", type=int, default=10)
    ap.add_argument("--num-predict", type=int, default=256)
    args = ap.parse_args()

    host = args.host.rstrip("/")
    with open(args.inputs) as f:
        inputs = json.load(f)
    batches = [int(x) for x in args.batches.split(",")]
    concs = [int(x) for x in args.concurrency.split(",")]
    order = batches + batches[::-1]  # ABBA
    tags = {nb: f"{args.base_tag}-nb{nb}" for nb in batches}

    summary = {
        "host": host,
        "ollama_version": api(host, "/api/version")["version"],
        "base_tag": args.base_tag,
        "batches": batches,
        "concurrency": concs,
        "requests_per_pass": args.requests_per_pass,
        "num_predict": args.num_predict,
        "order": "ABBA per bucket",
        "buckets": {
            b: [
                {"label": it["label"], "prompt_chars": len(it["prompt"])}
                for it in items
            ]
            for b, items in inputs.items()
        },
        "cells": [],
    }
    samples = {}
    created = []
    failed = False
    try:
        for nb, tag in tags.items():
            api(
                host,
                "/api/create",
                {
                    "model": tag,
                    "from": args.base_tag,
                    "parameters": {"num_batch": nb},
                    "stream": False,
                },
            )
            created.append(tag)
        for bucket, items in inputs.items():
            prompts = [it["prompt"] for it in items]
            acc = {}  # (nb, c) -> (results, walls, vram)
            for nb in order:
                tag = tags[nb]
                warm = one_request(host, tag, prompts[0], 8)  # load + warm, not counted
                if "error" in warm:
                    print(
                        f"{bucket} nb{nb}: warmup failed: {warm['error']}",
                        file=sys.stderr,
                    )
                ps = api(host, "/api/ps")["models"]
                vram = next((m.get("size_vram") for m in ps if m["name"] == tag), None)
                for c in concs:
                    res, wall = run_pass(
                        host, tag, prompts, c, args.requests_per_pass, args.num_predict
                    )
                    r_acc, w_acc, _ = acc.get((nb, c), ([], [], vram))
                    acc[(nb, c)] = (r_acc + res, w_acc + [wall], vram)
                    ok = sum(1 for r in res if "error" not in r)
                    print(
                        f"{bucket:6s} nb{nb:<5d} c{c}: {ok}/{len(res)} ok in {wall:5.1f}s",
                        flush=True,
                    )
            for (nb, c), (res, walls, vram) in sorted(acc.items()):
                row = {
                    "bucket": bucket,
                    "num_batch": nb,
                    "concurrency": c,
                    "size_vram": vram,
                }
                row.update(summarize(res, walls))
                failed |= not row["valid"]
                summary["cells"].append(row)
                text = next((r["text"] for r in res if "error" not in r), None)
                samples[f"{bucket}/nb{nb}/c{c}"] = text
                if row["valid"]:
                    print(
                        f"  {bucket:6s} nb{nb:<5d} c{c}: prompt {row['prompt_tokens_p50']:.0f} tok | "
                        f"prefill p50 {row['prefill_s_p50']:.2f}s ({row['prefill_tps_p50']:.0f} tok/s) | "
                        f"decode p50 {row['decode_tps_p50']:.0f} tok/s | wall p50 {row['wall_p50']:.2f}s "
                        f"p95 {row['wall_p95']:.2f}s | {row['abstracts_per_min']:.1f}/min | vram {vram}"
                    )
                else:
                    print(
                        f"  {bucket} nb{nb} c{c}: INVALID {row['errors']} errors {row['error_samples']}"
                    )
    finally:
        for tag in created:
            try:
                api(host, "/api/delete", {"model": tag}, method="DELETE")
            except (urllib.error.URLError, OSError) as exc:
                print(f"WARN: could not delete {tag}: {exc}", file=sys.stderr)
        try:
            api(
                host,
                "/api/generate",
                {"model": args.fim_tag, "keep_alive": -1, "stream": False},
            )
            pinned = [
                (m["name"], m.get("context_length"))
                for m in api(host, "/api/ps")["models"]
            ]
            print(f"restored {args.fim_tag}: {pinned}")
        except (urllib.error.URLError, OSError) as exc:
            print(f"ERROR: could not re-pin {args.fim_tag}: {exc}", file=sys.stderr)
            failed = True
        summary["valid"] = not failed
        with open(args.out, "w") as f:
            json.dump(summary, f, indent=2)
        if args.samples:
            with open(args.samples, "w") as f:
                json.dump(samples, f, indent=2)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
