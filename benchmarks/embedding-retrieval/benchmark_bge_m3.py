#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["FlagEmbedding>=1.3", "numpy<2"]
# ///
"""bge-m3 dense + sparse/hybrid arm (TASK-1122 Phase 3, offline).

Runs entirely on CPU/MPS via FlagEmbedding — independent of the cluster. Encodes
the corpus once (dense_vecs + lexical_weights, cached to scratch/), then scores:
  - dense-only  (cosine ranking)
  - hybrid      (per-query min-max-normalized dense+sparse, weighted, w swept)

The hybrid arm is the BUG-1016 hypothesis test: does sparse/lexical recover the
exact-ID/keyword queries pure dense misses? Writes results/bge-m3-{dense,hybrid}.json.

  uv run benchmark_bge_m3.py            # full corpus (slow: minutes on CPU)
  uv run benchmark_bge_m3.py --max-docs 50   # smoke test
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

import os

HERE = Path(__file__).parent
CORPUS = Path(os.environ.get("BENCH_CORPUS", HERE / "corpus.jsonl"))
GROUND_TRUTH = Path(
    os.environ.get("BENCH_GROUND_TRUTH", HERE / "eval_groundtruth_2026-07-04.json")
)
RESULTS = Path(os.environ.get("BENCH_RESULTS", HERE / "results"))
SCRATCH = Path(os.environ.get("BENCH_SCRATCH", HERE / "scratch"))
CHAR_CAP = 16000  # ~4k tokens; MPS/CPU attention is O(seq^2) — 8192 OOMs
MAX_LEN = 4096  # covers 95% of the corpus; keeps attention memory sane
W_SWEEP = [0.3, 0.5, 0.7]


def rrf_fuse(*score_rows, k=60):
    """Reciprocal-rank fusion of several per-doc score arrays → fused score array."""
    n = len(score_rows[0])
    fused = np.zeros(n)
    for row in score_rows:
        order = np.argsort(-row)
        rank = np.empty(n, dtype=int)
        rank[order] = np.arange(n)
        fused += 1.0 / (k + rank + 1)
    return fused


def classify(expected, top1_id, n_above):
    if expected == "NONE":
        return "TN" if n_above == 0 else "FP"
    if top1_id is None:
        return "FN"
    return "TP" if expected in top1_id.lower() else "MISS"


def score(per_q, thresholds=(0.0,)):
    out = []
    for th in thresholds:
        cats, tp, top5h, pos = {}, 0, 0, 0
        for r in per_q:
            above = [(e, s) for e, s in r["top5"] if s >= th]
            top1 = above[0][0] if above else None
            hit = classify(r["expected"], top1, len(above))
            is_pos = r["expected"] != "NONE"
            pos += is_pos
            in5 = is_pos and any(r["expected"] in e.lower() for e, s in above)
            tp += hit == "TP"
            top5h += in5
            c = cats.setdefault(r["category"], {"n": 0, "tp": 0, "top5": 0})
            c["n"] += 1
            c["tp"] += hit == "TP"
            c["top5"] += in5
        out.append(
            {
                "threshold": th,
                "top1_accuracy": round(tp / pos, 4),
                "top5_accuracy": round(top5h / pos, 4),
                "by_category": cats,
            }
        )
    return out


def rank(scores_row, ids):
    idx = np.argsort(-scores_row)[:5]
    return [(ids[i], float(scores_row[i])) for i in idx]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-docs", type=int, default=0)
    ap.add_argument(
        "--colbert", action="store_true", help="add ColBERT multi-vector arm"
    )
    ap.add_argument("--rrf", action="store_true", help="add reciprocal-rank-fusion arm")
    args = ap.parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)
    SCRATCH.mkdir(parents=True, exist_ok=True)

    from FlagEmbedding import BGEM3FlagModel

    corpus = [json.loads(l) for l in open(CORPUS)]
    if args.max_docs:
        corpus = corpus[: args.max_docs]
    ids = [r["entry_id"] for r in corpus]
    docs = [r["text"][:CHAR_CAP] for r in corpus]
    gt = json.load(open(GROUND_TRUTH))["questions"]

    # Force CPU — MPS OOMs on 4k-token attention. Slower but reliable.
    model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=False, devices="cpu")
    print(
        f"encoding {len(docs)} docs (dense+sparse{'+colbert' if args.colbert else ''}, "
        f"max_len={MAX_LEN}) on CPU…"
    )
    enc = model.encode(
        docs,
        batch_size=4,
        max_length=MAX_LEN,
        return_dense=True,
        return_sparse=True,
        return_colbert_vecs=args.colbert,
    )
    dvecs = np.asarray(enc["dense_vecs"], dtype=np.float32)  # normalized
    dlex = enc["lexical_weights"]
    dcol = enc.get("colbert_vecs") if args.colbert else None

    queries = [q["question"] for q in gt]
    qenc = model.encode(
        queries,
        batch_size=16,
        max_length=512,
        return_dense=True,
        return_sparse=True,
        return_colbert_vecs=args.colbert,
    )
    qd = np.asarray(qenc["dense_vecs"], dtype=np.float32)
    qlex = qenc["lexical_weights"]
    qcol = qenc.get("colbert_vecs") if args.colbert else None

    # dense cosine (normalized dot) — (Nq, Nd)
    dense_scores = qd @ dvecs.T
    # sparse lexical matching per (query, doc)
    sparse_scores = np.zeros_like(dense_scores)
    for i in range(len(queries)):
        for j in range(len(docs)):
            sparse_scores[i, j] = model.compute_lexical_matching_score(qlex[i], dlex[j])

    def minmax(row):
        lo, hi = row.min(), row.max()
        return (row - lo) / (hi - lo) if hi > lo else row * 0.0

    # dense-only result
    dense_pq = [
        {
            "qid": gt[i]["qid"],
            "category": gt[i]["category"],
            "expected": gt[i]["match_fragment"],
            "top5": [[e, round(s, 4)] for e, s in rank(dense_scores[i], ids)],
        }
        for i in range(len(gt))
    ]
    (RESULTS / "bge-m3-dense.json").write_text(
        json.dumps(
            {
                "model": "bge-m3-dense",
                "corpus_size": len(corpus),
                "thresholds": score(dense_pq, (0.0, 0.5, 0.55, 0.6)),
                "per_query": dense_pq,
            },
            indent=2,
        )
    )

    # hybrid w-sweep on per-query min-max normalized scores
    sweep = {}
    best = None
    for w in W_SWEEP:
        pq = []
        for i in range(len(gt)):
            h = w * minmax(dense_scores[i]) + (1 - w) * minmax(sparse_scores[i])
            pq.append(
                {
                    "qid": gt[i]["qid"],
                    "category": gt[i]["category"],
                    "expected": gt[i]["match_fragment"],
                    "top5": [[e, round(s, 4)] for e, s in rank(h, ids)],
                }
            )
        s0 = score(pq)[0]
        sweep[w] = {"top1": s0["top1_accuracy"], "top5": s0["top5_accuracy"]}
        if best is None or s0["top1_accuracy"] > best[1]:
            best = (w, s0["top1_accuracy"], pq)
    bw, _, bpq = best
    (RESULTS / "bge-m3-hybrid.json").write_text(
        json.dumps(
            {
                "model": "bge-m3-hybrid",
                "best_w": bw,
                "w_sweep": sweep,
                "corpus_size": len(corpus),
                "thresholds": score(bpq),
                "per_query": bpq,
            },
            indent=2,
        )
    )

    print(
        f"\n=== bge-m3 dense ===  top1={score(dense_pq)[0]['top1_accuracy']:.1%}  "
        f"top5={score(dense_pq)[0]['top5_accuracy']:.1%}"
    )
    print(f"=== bge-m3 hybrid === best_w={bw}  sweep={sweep}")

    # --- ColBERT multi-vector arm (late interaction) ---
    colbert_scores = None
    if args.colbert and dcol is not None:
        colbert_scores = np.zeros((len(gt), len(docs)))
        for i in range(len(gt)):
            for j in range(len(docs)):
                colbert_scores[i, j] = model.colbert_score(qcol[i], dcol[j])
        col_pq = [
            {
                "qid": gt[i]["qid"],
                "category": gt[i]["category"],
                "expected": gt[i]["match_fragment"],
                "top5": [[e, round(s, 4)] for e, s in rank(colbert_scores[i], ids)],
            }
            for i in range(len(gt))
        ]
        (RESULTS / "bge-m3-colbert.json").write_text(
            json.dumps(
                {
                    "model": "bge-m3-colbert",
                    "corpus_size": len(corpus),
                    "thresholds": score(col_pq),
                    "per_query": col_pq,
                },
                indent=2,
            )
        )
        print(
            f"=== bge-m3 colbert === top1={score(col_pq)[0]['top1_accuracy']:.1%} "
            f"top5={score(col_pq)[0]['top5_accuracy']:.1%}"
        )

    # --- RRF fusion arm (rank-based, scale-free) ---
    if args.rrf:
        pq = []
        for i in range(len(gt)):
            rows = [dense_scores[i], sparse_scores[i]]
            if colbert_scores is not None:
                rows.append(colbert_scores[i])
            f = rrf_fuse(*rows)
            pq.append(
                {
                    "qid": gt[i]["qid"],
                    "category": gt[i]["category"],
                    "expected": gt[i]["match_fragment"],
                    "top5": [[e, round(s, 6)] for e, s in rank(f, ids)],
                }
            )
        modes = "dense+sparse" + ("+colbert" if colbert_scores is not None else "")
        (RESULTS / "bge-m3-rrf.json").write_text(
            json.dumps(
                {
                    "model": "bge-m3-rrf",
                    "modes": modes,
                    "corpus_size": len(corpus),
                    "thresholds": score(pq),
                    "per_query": pq,
                },
                indent=2,
            )
        )
        print(
            f"=== bge-m3 rrf ({modes}) === top1={score(pq)[0]['top1_accuracy']:.1%} "
            f"top5={score(pq)[0]['top5_accuracy']:.1%}"
        )


if __name__ == "__main__":
    main()
