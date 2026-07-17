#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27"]
# ///
"""Throughput benchmark for the OpenViking embedder (TASK-1136).

Measures embedding *speed* — the dimension the retrieval-quality suite
(``benchmark_embedders.py``) never records. It embeds a fixed corpus once and
only reports a coarse "corpus embedded in Ns" line; this harness replays a
fixed corpus through an OpenAI-compatible ``/v1/embeddings`` endpoint
(llama.cpp server), single-stream (no concurrency — matching the production
``--parallel 1`` config), and records docs/s, tokens/s, and per-batch latency
(p50 / p95 / mean).

The workload is identical across cards, so results are directly comparable.
Card + backend identity is passed via ``--label`` and recorded in the result
JSON (e.g. ``gtx1080-cuda`` vs ``9070xt-vulkan``).

GPU-side utilisation is intentionally out of scope here — it is already
observable during the run via the NVIDIA DCGM and amd-smi Prometheus
exporters feeding Grafana. This harness measures client-observed throughput
and latency only.

Env / flags (flag overrides env):
  EMBED_URL   / --url         required; e.g.
                              http://embedder-cuda-bench.bench.svc:8000/v1/embeddings
  BENCH_CORPUS / --corpus     default /data/corpus.jsonl ({entry_id, text} per line)
  BENCH_RESULTS / --out-dir   default /data/results
  BENCH_LABEL  / --label      required; card+backend tag, e.g. gtx1080-cuda
  --batch-size                docs per request (default 16)
  --max-chars                 per-doc char cap guarding the ctx window
                              (default 30000 ~ 7.5k tokens; 2 corpus entries exceed 8192)
  --warmup                    warmup batches excluded from steady-state (default 3)
  --repeat                    replay the corpus N times for a longer run (default 1)
  --max-docs                  cap docs for a smoke run (default: all)
  --model                     model name sent in payload (default 'current'; llama.cpp ignores)
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path

import httpx


def _pct(values: list[float], p: float) -> float:
    """Nearest-rank percentile (p in [0, 100])."""
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def load_corpus(path: Path, max_chars: int, max_docs: int | None) -> list[str]:
    texts: list[str] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            text = (row.get("text") or "")[:max_chars]
            if text:
                texts.append(text)
            if max_docs and len(texts) >= max_docs:
                break
    return texts


def embed(client: httpx.Client, url: str, model: str, batch: list[str]) -> dict:
    """POST one batch to /v1/embeddings; return the parsed JSON."""
    r = client.post(url, json={"model": model, "input": batch}, timeout=600.0)
    r.raise_for_status()
    return r.json()


def batches(texts: list[str], size: int) -> list[list[str]]:
    return [texts[i : i + size] for i in range(0, len(texts), size)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=os.environ.get("EMBED_URL"))
    ap.add_argument(
        "--corpus", default=os.environ.get("BENCH_CORPUS", "/data/corpus.jsonl")
    )
    ap.add_argument(
        "--out-dir", default=os.environ.get("BENCH_RESULTS", "/data/results")
    )
    ap.add_argument("--label", default=os.environ.get("BENCH_LABEL"))
    ap.add_argument(
        "--batch-size", type=int, default=int(os.environ.get("BENCH_BATCH", "16"))
    )
    ap.add_argument(
        "--max-chars", type=int, default=int(os.environ.get("BENCH_MAX_CHARS", "30000"))
    )
    ap.add_argument(
        "--warmup", type=int, default=int(os.environ.get("BENCH_WARMUP", "3"))
    )
    ap.add_argument(
        "--repeat", type=int, default=int(os.environ.get("BENCH_REPEAT", "1"))
    )
    ap.add_argument(
        "--max-docs",
        type=int,
        default=int(os.environ.get("BENCH_MAX_DOCS", "0")) or None,
    )
    ap.add_argument("--model", default=os.environ.get("BENCH_MODEL", "current"))
    args = ap.parse_args()

    if not args.url:
        ap.error("--url (or EMBED_URL) is required")
    if not args.label:
        ap.error("--label (or BENCH_LABEL) is required")

    corpus = load_corpus(Path(args.corpus), args.max_chars, args.max_docs)
    if not corpus:
        ap.error(f"no documents loaded from {args.corpus}")
    texts = corpus * args.repeat
    all_batches = batches(texts, args.batch_size)

    print(f"[{args.label}] {args.url}")
    print(
        f"  corpus={len(corpus)} repeat={args.repeat} docs={len(texts)} "
        f"batch_size={args.batch_size} batches={len(all_batches)} "
        f"max_chars={args.max_chars} warmup={args.warmup}"
    )

    client = httpx.Client()

    # --- warmup (excluded from steady-state timing) ---
    for i in range(min(args.warmup, len(all_batches))):
        embed(client, args.url, args.model, all_batches[i])
    timed = (
        all_batches[args.warmup :] if args.warmup < len(all_batches) else all_batches
    )
    print(f"  warmed up; timing {len(timed)} batches...")

    per_batch: list[dict] = []
    tokens_estimated = False
    wall_t0 = time.perf_counter()
    for idx, batch in enumerate(timed):
        t0 = time.perf_counter()
        resp = embed(client, args.url, args.model, batch)
        dt = time.perf_counter() - t0
        usage = resp.get("usage") or {}
        toks = usage.get("prompt_tokens") or usage.get("total_tokens")
        if toks is None:
            toks = sum(len(t) // 4 for t in batch)  # coarse fallback estimate
            tokens_estimated = True
        per_batch.append(
            {"i": idx, "n": len(batch), "tokens": int(toks), "latency_s": round(dt, 4)}
        )
    wall_s = time.perf_counter() - wall_t0

    total_docs = sum(b["n"] for b in per_batch)
    total_tokens = sum(b["tokens"] for b in per_batch)
    lat = [b["latency_s"] for b in per_batch]
    per_doc_lat = [b["latency_s"] / b["n"] for b in per_batch if b["n"]]

    result = {
        "label": args.label,
        "url": args.url,
        "model": args.model,
        "config": {
            "batch_size": args.batch_size,
            "max_chars": args.max_chars,
            "warmup_batches": args.warmup,
            "repeat": args.repeat,
        },
        "corpus": {"docs": len(corpus), "timed_docs": total_docs},
        "tokens_estimated": tokens_estimated,
        "totals": {
            "docs": total_docs,
            "tokens": total_tokens,
            "wall_s": round(wall_s, 3),
            "docs_per_s": round(total_docs / wall_s, 2) if wall_s else 0.0,
            "tokens_per_s": round(total_tokens / wall_s, 1) if wall_s else 0.0,
        },
        "batch_latency_s": {
            "mean": round(statistics.mean(lat), 4) if lat else 0.0,
            "p50": round(_pct(lat, 50), 4),
            "p95": round(_pct(lat, 95), 4),
            "max": round(max(lat), 4) if lat else 0.0,
        },
        "per_doc_latency_s": {
            "mean": round(statistics.mean(per_doc_lat), 4) if per_doc_lat else 0.0,
            "p50": round(_pct(per_doc_lat, 50), 4),
            "p95": round(_pct(per_doc_lat, 95), 4),
        },
        "per_batch": per_batch,
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"throughput-{args.label}.json"
    out_path.write_text(json.dumps(result, indent=2))

    t = result["totals"]
    bl = result["batch_latency_s"]
    print(
        f"\n  RESULT [{args.label}]  "
        f"{t['docs_per_s']} docs/s  {t['tokens_per_s']} tok/s  "
        f"wall={t['wall_s']}s  batch p50={bl['p50']}s p95={bl['p95']}s"
        + ("  (tokens estimated)" if tokens_estimated else "")
    )
    print(f"  wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
