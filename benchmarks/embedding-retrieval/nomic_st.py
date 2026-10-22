#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["sentence-transformers>=3.0", "einops", "numpy<2"]
# ///
"""Fair-context nomic arm (TASK-1122) — nomic-embed-text-v1.5 at full 8192 ctx.

The llama.cpp GGUF clamps to 2048; sentence-transformers with the model's dynamic
NTK rope serves the true 8192 window. This is the confound-free nomic comparison
vs the 2048-clamped llama.cpp arm. Correct search_* prefixes, mean pooling, 768-d.
Ranks against the corpus in-process (numpy) and scores with the shared metric.
Writes results/nomic-8192.json.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
CORPUS = Path(os.environ.get("BENCH_CORPUS", HERE / "corpus.jsonl"))
GT = Path(
    os.environ.get("BENCH_GROUND_TRUTH", HERE / "eval_groundtruth_2026-07-04.json")
)
RESULTS = Path(os.environ.get("BENCH_RESULTS", HERE / "results"))
CHAR_CAP = 30000


def classify(expected, top1, n_above):
    if expected == "NONE":
        return "TN" if n_above == 0 else "FP"
    if top1 is None:
        return "FN"
    return "TP" if expected in top1.lower() else "MISS"


def score(per_q, thresholds=(0.0, 0.5, 0.55, 0.6)):
    out = []
    for th in thresholds:
        cats, tp, top5h, pos = {}, 0, 0, 0
        for r in per_q:
            above = [(e, s) for e, s in r["top5"] if s >= th]
            hit = classify(r["expected"], above[0][0] if above else None, len(above))
            isp = r["expected"] != "NONE"
            pos += isp
            in5 = isp and any(r["expected"] in e.lower() for e, s in above)
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


def main():
    from sentence_transformers import SentenceTransformer

    RESULTS.mkdir(parents=True, exist_ok=True)
    corpus = [json.loads(l) for l in open(CORPUS)]
    ids = [r["entry_id"] for r in corpus]
    gt = json.load(open(GT))["questions"]

    # Force CPU by default: full 8192 attention is O(seq^2) and OOMs the MPS
    # shared pool (single ~24 GiB batch alloc), especially alongside the bge arm.
    # CPU uses the 128 GB unified RAM instead. Override with ST_DEVICE=mps.
    device = os.environ.get("ST_DEVICE", "cpu")
    batch = int(os.environ.get("ST_BATCH", "4"))
    # NOMIC_CTX isolates the context effect from the serving-stack effect: run
    # this same sentence-transformers path at 2048 (control) and 8192 (fair) so
    # the 2048-vs-8192 delta can't be blamed on llama.cpp-vs-ST differences.
    ctx = int(os.environ.get("NOMIC_CTX", "8192"))
    m = SentenceTransformer(
        "nomic-ai/nomic-embed-text-v1.5", trust_remote_code=True, device=device
    )
    m.max_seq_length = ctx

    docs = ["search_document: " + r["text"][:CHAR_CAP] for r in corpus]
    dv = m.encode(
        docs, batch_size=batch, normalize_embeddings=True, show_progress_bar=False
    )
    qv = m.encode(
        ["search_query: " + q["question"] for q in gt],
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    sims = np.asarray(qv) @ np.asarray(dv).T  # cosine (normalized)

    per_q = []
    for i, q in enumerate(gt):
        idx = np.argsort(-sims[i])[:5]
        per_q.append(
            {
                "qid": q["qid"],
                "category": q["category"],
                "expected": q["match_fragment"],
                "top5": [[ids[j], float(sims[i][j])] for j in idx],
            }
        )
    out = {
        "model": f"nomic-st-{ctx}",
        "corpus_size": len(corpus),
        "thresholds": score(per_q),
        "per_query": per_q,
    }
    (RESULTS / f"nomic-st-{ctx}.json").write_text(json.dumps(out, indent=2))
    s0 = out["thresholds"][0]
    print(
        f"nomic-st-{ctx}: top1={s0['top1_accuracy'] * 100:.1f}% top5={s0['top5_accuracy'] * 100:.1f}%"
    )


if __name__ == "__main__":
    main()
