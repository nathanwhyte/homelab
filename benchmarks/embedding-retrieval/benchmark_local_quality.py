#!/usr/bin/env python3
"""TASK-1218 preliminary whole-entry retrieval, with no database or OV writes.

Run one model at a time. Owns a loopback llama-server and always stops it.
Keeps raw vectors and input hashes for auditing; this is not the OV acceptance arm.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import socket
import subprocess
import time
import urllib.error
import urllib.request
from array import array
from pathlib import Path


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def request(base, route, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        base + route, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=180) as response:
        return json.load(response)


def normalize(values, dimension):
    if len(values) != dimension or not all(math.isfinite(v) for v in values):
        raise ValueError("Invalid embedding dimension or nonfinite vector")
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0:
        raise ValueError("Zero embedding")
    return array("d", (v / norm for v in values))


def embed_document(base, text):
    # Preserve the historical harness's 60% retry, but only on a confirmed
    # context overflow. Record the actual input for paired-arm verification.
    for _ in range(9):
        try:
            return text, request(
                base,
                "/v1/embeddings",
                {
                    "model": "benchmark",
                    "input": [text],
                    "cache_prompt": False,
                },
            )
        except urllib.error.HTTPError as error:
            body = error.read().decode()
            if (
                error.code != 400
                or "exceeds the available context size" not in body
                or len(text) <= 400
            ):
                raise RuntimeError(
                    f"Embedding failed: HTTP {error.code}: {body}"
                ) from error
            text = text[: int(len(text) * 0.6)]
    raise ValueError("Context overflow after nine attempts")


def score(rows):
    positive = [r for r in rows if r["expected"] != "NONE"]
    return {
        "positives": len(positive),
        "top1_hits": sum(r["expected"] in r["top5"][0][0] for r in positive),
        "top5_hits": sum(
            any(r["expected"] in entry for entry, _ in r["top5"]) for r in positive
        ),
        "negative_queries": len(rows) - len(positive),
        "negative_policy": "Ranking only; no abstention threshold assessed",
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True, choices=["4b", "0.6b"])
    ap.add_argument("--gguf", required=True, type=Path)
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--ground-truth", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--port", type=int, default=18082)
    args = ap.parse_args()
    dimension = 2560 if args.model == "4b" else 1024
    corpus = [json.loads(line) for line in args.corpus.read_text().splitlines()]
    questions = json.loads(args.ground_truth.read_text())["questions"]
    if len({r["entry_id"] for r in corpus}) != len(corpus):
        raise ValueError("Duplicate corpus IDs")
    for q in questions:
        if q["match_fragment"] != "NONE" and not any(
            q["match_fragment"] in r["entry_id"].lower() for r in corpus
        ):
            raise ValueError(f"Missing gold target: {q['qid']}")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", args.port))
    args.output.mkdir(parents=True, exist_ok=False)
    base = f"http://127.0.0.1:{args.port}"
    command = [
        "llama-server",
        "-m",
        str(args.gguf),
        "--host",
        "127.0.0.1",
        "--port",
        str(args.port),
        "--embedding",
        "--pooling",
        "last",
        "--parallel",
        "1",
        "--ctx-size",
        "8192",
        "--batch-size",
        "8192",
        "--ubatch-size",
        "8192",
        "--n-gpu-layers",
        "99",
        "--cache-ram",
        "0",
    ]
    metadata = {
        "model": args.model,
        "dimension": dimension,
        "command": command,
        "gguf_sha256": sha256(args.gguf),
        "corpus_sha256": sha256(args.corpus),
        "ground_truth_sha256": sha256(args.ground_truth),
        "corpus_size": len(corpus),
        "query_prefix": "",
        "document_prefix": "",
        "char_cap": 30000,
        "overflow_policy": "confirmed context overflow only: retain 60% of characters; log actual input hash and length",
        "scope": "Mac Metal whole-entry preliminary; not OV chunked retrieval or CUDA latency",
        "llama_version": subprocess.check_output(
            ["llama-server", "--version"], text=True, stderr=subprocess.STDOUT
        ).strip(),
    }
    (args.output / "provenance.json").write_text(json.dumps(metadata, indent=2) + "\n")
    vectors = []
    with (args.output / "server.log").open("w") as log:
        proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 180
            while True:
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"llama-server exited {proc.returncode}; see server.log"
                    )
                try:
                    if request(base, "/health").get("status") == "ok":
                        break
                except (OSError, urllib.error.URLError):
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError("Server health timeout")
                time.sleep(1)
            started = time.monotonic()
            with (args.output / "vectors.jsonl").open("w") as raw:
                for index, row in enumerate(corpus):
                    text = row["text"][:30000]
                    text, response = embed_document(base, text)
                    values = response["data"][0]["embedding"]
                    vector = normalize(values, dimension)
                    vectors.append(vector)
                    raw.write(
                        json.dumps(
                            {
                                "entry_id": row["entry_id"],
                                "input_sha256": hashlib.sha256(
                                    text.encode()
                                ).hexdigest(),
                                "input_chars": len(text),
                                "capped": len(row["text"]) > len(text),
                                "usage": response.get("usage"),
                                "vector": values,
                            }
                        )
                        + "\n"
                    )
                    if (index + 1) % 50 == 0:
                        print(
                            f"{args.model}: {index + 1}/{len(corpus)} documents, {time.monotonic() - started:.0f}s",
                            flush=True,
                        )
            per_query = []
            for question in questions:
                response = request(
                    base,
                    "/v1/embeddings",
                    {
                        "model": "benchmark",
                        "input": [question["question"]],
                        "cache_prompt": False,
                    },
                )
                values = response["data"][0]["embedding"]
                qv = normalize(values, dimension)
                ranked = sorted(
                    (
                        (r["entry_id"].lower(), sum(a * b for a, b in zip(v, qv)))
                        for r, v in zip(corpus, vectors)
                    ),
                    key=lambda x: (-x[1], x[0]),
                )[:5]
                per_query.append(
                    {
                        "qid": question["qid"],
                        "category": question["category"],
                        "expected": question["match_fragment"],
                        "top5": ranked,
                        "vector": values,
                    }
                )
            result = {
                **metadata,
                "elapsed_seconds": time.monotonic() - started,
                "scores": score(per_query),
                "per_query": per_query,
            }
            (args.output / "result.json").write_text(
                json.dumps(result, indent=2) + "\n"
            )
            print(json.dumps(result["scores"]), flush=True)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


if __name__ == "__main__":
    main()
