#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]>=3.2", "httpx>=0.27"]
# ///
"""Embedder A/B harness (TASK-1122 Phase 1).

For one candidate: embed the corpus (with the model's document prefix) into a
per-candidate pgvector table, embed the 34 positive + 4 negative queries (with the
query prefix), rank by cosine, and score top-1 / top-5 vs the frozen ground truth.

Backends:
  openai  — OpenAI-compatible /v1/embeddings (llama.cpp server; Qwen via port-forward)
  ollama  — POST {ollama}/api/embed  (nomic, bge-m3 dense)

Candidates are defined in CANDIDATES below. Run one at a time:
  uv run benchmark_embedders.py --model qwen3-4b
  uv run benchmark_embedders.py --model qwen3-4b-unprefixed

Writes results/<model>.json. Idempotent: drops + recreates the candidate's table.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import httpx
import psycopg

HERE = Path(__file__).parent
CORPUS = Path(os.environ.get("BENCH_CORPUS", HERE / "corpus.jsonl"))
GROUND_TRUTH = Path(
    os.environ.get("BENCH_GROUND_TRUTH", HERE / "eval_groundtruth_2026-07-04.json")
)
RESULTS = Path(os.environ.get("BENCH_RESULTS", HERE / "results"))
# BENCH_DB / EMBED_URL let the same harness run in-cluster (Service DNS) or on the
# Mac (localhost). EMBED_URL, when set, overrides the candidate's url — the driver
# sets it per arm before each run.
DB = os.environ.get("BENCH_DB", "postgresql://bench:bench@localhost:5433/embedbench")

THRESHOLDS = [
    0.0,
    0.50,
    0.55,
    0.60,
]  # 0.0 = pure-ranking accuracy (cross-model comparable)
CHAR_CAP = (
    30000  # ~7.5k tokens; guards the 8192-token window (2 corpus entries exceed it)
)

QWEN_INSTRUCT = "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: "
# Domain-tailored instruction (Qwen card recommends customizing per scenario).
QWEN_DOMAIN = (
    "Instruct: Given a question about a homelab / compendium knowledge base "
    "(bugs, errors, features, guides, info, tasks), retrieve the entry that answers it\nQuery: "
)

# name -> serving config. doc_prefix/query_prefix applied verbatim.
CANDIDATES = {
    "qwen3-4b": {
        "backend": "openai",
        "url": "http://127.0.0.1:8083/v1/embeddings",
        "model": "qwen3-embedding-4b",
        "dim": 2560,
        "doc_prefix": "",
        "query_prefix": QWEN_INSTRUCT,
        "batch": 8,
    },
    "qwen3-4b-unprefixed": {  # production parity — no query instruction
        "backend": "openai",
        "url": "http://127.0.0.1:8083/v1/embeddings",
        "model": "qwen3-embedding-4b",
        "dim": 2560,
        "doc_prefix": "",
        "query_prefix": "",
        "batch": 8,
    },
    # Qwen3-Embedding-4B on timmy's RX 9070 XT via Ollama (LAN). POOLING must be
    # validated (last-token) before trusting — see pooling-validation note.
    "qwen3-4b-9070": {
        "backend": "ollama",
        "url": "http://192.168.1.19:11434/api/embed",
        "model": "dengcao/Qwen3-Embedding-4B:Q8_0",
        "dim": 2560,
        "doc_prefix": "",
        "query_prefix": QWEN_INSTRUCT,
        "batch": 16,
    },
    "qwen3-4b-9070-unprefixed": {
        "backend": "ollama",
        "url": "http://192.168.1.19:11434/api/embed",
        "model": "dengcao/Qwen3-Embedding-4B:Q8_0",
        "dim": 2560,
        "doc_prefix": "",
        "query_prefix": "",
        "batch": 16,
    },
    # Qwen with the domain-tailored instruction (EMBED_URL points at whichever
    # Qwen service; only the query prefix differs → the tuning experiment).
    "qwen3-4b-domain": {
        "backend": "openai",
        "url": "http://127.0.0.1:8083/v1/embeddings",
        "model": "qwen3-embedding-4b",
        "dim": 2560,
        "doc_prefix": "",
        "query_prefix": QWEN_DOMAIN,
        "batch": 8,
    },
    "qwen3-0.6b-domain": {
        "backend": "openai",
        "url": "http://127.0.0.1:8082/v1/embeddings",
        "model": "qwen3-embedding-0.6b",
        "dim": 1024,
        "doc_prefix": "",
        "query_prefix": QWEN_DOMAIN,
        "batch": 16,
    },
    # Qwen3-0.6B — local llama.cpp (serve_local.sh qwen06 -> :8082)
    "qwen3-0.6b": {
        "backend": "openai",
        "url": "http://127.0.0.1:8082/v1/embeddings",
        "model": "qwen3-embedding-0.6b",
        "dim": 1024,
        "doc_prefix": "",
        "query_prefix": QWEN_INSTRUCT,
        "batch": 16,
    },
    "qwen3-0.6b-unprefixed": {
        "backend": "openai",
        "url": "http://127.0.0.1:8082/v1/embeddings",
        "model": "qwen3-embedding-0.6b",
        "dim": 1024,
        "doc_prefix": "",
        "query_prefix": "",
        "batch": 16,
    },
    # nomic-embed-text-v1.5 — local llama.cpp. This GGUF's n_ctx clamps to 2048
    # (yarn didn't extend it); char_cap 7000 (~1750 tok) keeps inputs under 2048.
    # NOTE: 2048 is also how nomic ran in prod (old embedder-llamacpp) — this arm
    # is the production-representative baseline. A full-8192 nomic (rope-extended
    # GGUF or sentence-transformers) is a follow-up for a fair-context comparison.
    "nomic": {  # correctly prefixed (model-recommended)
        "backend": "openai",
        "url": "http://127.0.0.1:8081/v1/embeddings",
        "model": "nomic-embed-text-v1.5",
        "dim": 768,
        "doc_prefix": "search_document: ",
        "query_prefix": "search_query: ",
        "batch": 16,
        "char_cap": 7000,
    },
    "nomic-unprefixed": {  # production/omnipendium parity — no search_* prefix
        "backend": "openai",
        "url": "http://127.0.0.1:8081/v1/embeddings",
        "model": "nomic-embed-text-v1.5",
        "dim": 768,
        "doc_prefix": "",
        "query_prefix": "",
        "batch": 16,
        "char_cap": 7000,
    },
}


def l2(v: list[float]) -> list[float]:
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v] if n else v


def vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"


def embed_openai(
    client: httpx.Client, url: str, model: str, texts: list[str]
) -> list[list[float]]:
    r = client.post(url, json={"model": model, "input": texts}, timeout=120)
    r.raise_for_status()
    data = r.json()["data"]
    return [d["embedding"] for d in data]


def embed_ollama(
    client: httpx.Client, url: str, model: str, texts: list[str]
) -> list[list[float]]:
    r = client.post(url, json={"model": model, "input": texts}, timeout=120)
    r.raise_for_status()
    return r.json()["embeddings"]


def _fn(cfg):
    return embed_openai if cfg["backend"] == "openai" else embed_ollama


def embed_one(client, cfg, text):
    """Embed one text; on a context-overflow 400/500, shrink and retry until it fits."""
    fn = _fn(cfg)
    t = text or " "
    for _ in range(8):
        try:
            return l2(fn(client, cfg["url"], cfg["model"], [t])[0])
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (400, 500) and len(t) > 400:
                t = t[: int(len(t) * 0.6)]
                continue
            raise
    return l2(fn(client, cfg["url"], cfg["model"], [t[:400]])[0])


def embed_batch(client, cfg, texts):
    fn = _fn(cfg)
    try:  # fast path: whole batch in one request
        return [l2(v) for v in fn(client, cfg["url"], cfg["model"], texts)]
    except Exception:
        # a doc in the batch overflowed context — embed each with shrink-to-fit
        return [embed_one(client, cfg, t) for t in texts]


def classify(expected: str, top1_id: str | None, n_above: int) -> str:
    if expected == "NONE":
        return "TN" if n_above == 0 else "FP"
    if top1_id is None:
        return "FN"
    return "TP" if expected in top1_id.lower() else "MISS"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, choices=list(CANDIDATES))
    args = ap.parse_args()
    cfg = dict(CANDIDATES[args.model])
    cfg["url"] = os.environ.get(
        "EMBED_URL", cfg["url"]
    )  # driver overrides per arm in-cluster
    table = "emb_" + args.model.replace("-", "_").replace(".", "_")
    RESULTS.mkdir(exist_ok=True)

    corpus = [json.loads(l) for l in open(CORPUS)]
    gt = json.load(open(GROUND_TRUTH))
    questions = gt["questions"]

    client = httpx.Client()
    conn = psycopg.connect(DB, autocommit=True)
    cur = conn.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {table}")
    cur.execute(
        f"CREATE TABLE {table} (entry_id text PRIMARY KEY, vault_path text, "
        f"embedding vector({cfg['dim']}) NOT NULL)"
    )

    # --- embed + load corpus ---
    t0 = time.time()
    cap = cfg.get("char_cap", CHAR_CAP)
    docs = [cfg["doc_prefix"] + r["text"][:cap] for r in corpus]
    B = cfg["batch"]
    for i in range(0, len(docs), B):
        chunk = docs[i : i + B]
        vecs = embed_batch(client, cfg, chunk)
        with cur.copy(
            f"COPY {table} (entry_id, vault_path, embedding) FROM STDIN"
        ) as cp:
            for r, v in zip(corpus[i : i + B], vecs):
                cp.write_row((r["entry_id"], r["vault_path"], vec_literal(v)))
        print(f"\r  embedded {min(i + B, len(docs))}/{len(docs)}", end="", flush=True)
    # No ANN index: 1097 rows → exact cosine is fast AND more accurate than
    # approximate hnsw. (hnsw also caps at 2000 dims, which Qwen-4B's 2560 exceeds.)
    print(f"\n  corpus embedded in {time.time() - t0:.0f}s (exact search, no index)")

    # --- query + score ---
    per_q = []
    for q in questions:
        if q["match_fragment"] == "NONE":
            qtext = cfg["query_prefix"] + q["question"]
        else:
            qtext = cfg["query_prefix"] + q["question"]
        qv = embed_batch(client, cfg, [qtext])[0]
        cur.execute(
            f"SELECT entry_id, 1 - (embedding <=> %s::vector) AS score "
            f"FROM {table} ORDER BY embedding <=> %s::vector LIMIT 5",
            (vec_literal(qv), vec_literal(qv)),
        )
        top5 = cur.fetchall()  # [(entry_id, score), ...] best first
        row = {
            "qid": q["qid"],
            "category": q["category"],
            "expected": q["match_fragment"],
            "top5": top5,
        }
        per_q.append(row)

    # --- aggregate per threshold ---
    def score_at(th: float):
        cats: dict = {}
        tp = miss = fp = tn = fn = top5hits = 0
        positives = 0
        for r in per_q:
            above = [(eid, s) for eid, s in r["top5"] if s >= th]
            top1 = above[0][0] if above else None
            hit = classify(r["expected"], top1, len(above))
            is_pos = r["expected"] != "NONE"
            positives += is_pos
            in5 = is_pos and any(r["expected"] in eid.lower() for eid, s in above)
            top5hits += 1 if in5 else 0
            tp += hit == "TP"
            miss += hit == "MISS"
            fp += hit == "FP"
            tn += hit == "TN"
            fn += hit == "FN"
            c = cats.setdefault(r["category"], {"n": 0, "tp": 0, "top5": 0})
            c["n"] += 1
            c["tp"] += hit == "TP"
            c["top5"] += 1 if in5 else 0
        return {
            "threshold": th,
            "top1_accuracy": round(tp / positives, 4),
            "top5_accuracy": round(top5hits / positives, 4),
            "counts": {
                "TP": tp,
                "MISS": miss,
                "FP": fp,
                "TN": tn,
                "FN": fn,
                "positives": positives,
            },
            "by_category": cats,
        }

    summary = {
        "model": args.model,
        "config": {
            k: cfg[k] for k in ("backend", "model", "dim", "doc_prefix", "query_prefix")
        },
        "corpus_size": len(corpus),
        "thresholds": [score_at(th) for th in THRESHOLDS],
        "per_query": [
            {
                "qid": r["qid"],
                "category": r["category"],
                "expected": r["expected"],
                "top5": [[e, round(s, 4)] for e, s in r["top5"]],
            }
            for r in per_q
        ],
    }
    out = RESULTS / f"{args.model}.json"
    out.write_text(json.dumps(summary, indent=2))

    print(f"\n=== {args.model} ===")
    for s in summary["thresholds"]:
        c = s["counts"]
        print(
            f"  th={s['threshold']:.2f}  top1={s['top1_accuracy']:.1%}  top5={s['top5_accuracy']:.1%}"
            f"  (TP{c['TP']} MISS{c['MISS']} FN{c['FN']} FP{c['FP']} TN{c['TN']}/{c['positives']}pos)"
        )
    print(f"  -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
