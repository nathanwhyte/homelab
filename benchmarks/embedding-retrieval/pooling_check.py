#!/usr/bin/env python3
# /// script
# dependencies = ["httpx>=0.27"]
# ///
"""Validate Qwen3-Embedding pooling on an Ollama endpoint (TASK-1122).

Replicates the Qwen3-Embedding-4B model-card reference pair. With correct
last-token pooling + query instruction, the matching pair scores ~0.75 and the
non-matching ~0.11 (card values). If pooling is wrong (mean), the gap collapses
(both mid-range) and any 4B-on-Ollama numbers are invalid — fall back to
llama.cpp --pooling last on the 9070 XT via Vulkan.

  uv run pooling_check.py [host] [model]
"""

import sys

import httpx

HOST = sys.argv[1] if len(sys.argv) > 1 else "http://192.168.1.19:11434"
MODEL = sys.argv[2] if len(sys.argv) > 2 else "dengcao/Qwen3-Embedding-4B:Q8_0"
INSTRUCT = "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"


def embed(text):
    r = httpx.post(f"{HOST}/api/embed", json={"model": MODEL, "input": [text]}, timeout=60)
    r.raise_for_status()
    return r.json()["embeddings"][0]


def cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb)


q = embed(INSTRUCT + "What is the capital of China?")
d_match = embed("The capital of China is Beijing.")
d_non = embed("Gravity is a force that attracts two bodies towards each other.")
m, n = cos(q, d_match), cos(q, d_non)
print(f"dim={len(q)}")
print(f"matching  cos = {m:.3f}   (card: ~0.75)")
print(f"non-match cos = {n:.3f}   (card: ~0.11)")
gap = m - n
verdict = "OK — last-token pooling looks correct" if (m > 0.6 and gap > 0.25) else \
          "SUSPECT — pooling likely wrong (mean?); gap too small. Use llama.cpp --pooling last."
print(f"gap = {gap:.3f}  ->  {verdict}")
