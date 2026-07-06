# Embedding prefix / instruction behavior (Phase 0 item 2)

Determining what prompt each stack applies to **queries vs documents**, so the
benchmark matches each candidate's documented policy instead of silently
embedding queries unprefixed (which would contaminate every comparison).

## What the live stack does today (determined from config, 2026-07-04)

- **OV embedding client** — `openviking-standalone-configmap.yaml` `embedding.dense`
  is `provider: openai`, `model: qwen3-embedding-4b`, `api_base:
http://embedder-qwen.viking.svc.cluster.local:8080/v1`. It calls the
  OpenAI-compatible `/v1/embeddings` endpoint. **No instruction / prompt field is
  configured** in the OV embedding block.
- **llama-server (wemby CUDA)** — `embedder-qwen-cuda-deployment.yaml` runs
  `llama-server --embedding --pooling last --ctx-size … --ubatch-size …`.
  **No prompt template / instruction is set at the server.**
- **Conclusion:** the production Qwen3-4B embedder is almost certainly fed **raw
  text with no Qwen query-instruction prefix**, for both documents and queries.
  Qwen3-Embedding is instruction-aware and its card recommends a query
  instruction (documents unprefixed) — so production is running in the
  **unprefixed** regime. This is the IDEA-1042 open question, now answered at the
  config layer.
- **omnipendium** — `services/embeddings.py:75` posts `{"model", "input": text}`
  to Ollama `/api/embed` verbatim: **no prefix** for nomic either (nomic
  _requires_ `search_query:` / `search_document:`), so omnipendium's nomic index
  is running mis-prefixed today.

## Still to confirm at run time (cheap live probe)

OV's internal `openai` embedding client _could_ prepend an instruction we can't
see from config. Before the first scoring pass, confirm empirically:

1. `POST /v1/embeddings` to the embedder directly with `"foo"` and with
   `"Instruct: …\nQuery: foo"`; if OV applies a prefix, the OV-path vector for a
   query will match the manually-prefixed direct vector, not the raw one.
2. Or grep OV request logs / set OV embedding debug logging for one query.

## Per-candidate prefix policy the harness must apply

| Candidate                 | Document                  | Query                                                 | Source                 |
| ------------------------- | ------------------------- | ----------------------------------------------------- | ---------------------- |
| nomic-embed-text-v1.5     | `search_document: <text>` | `search_query: <text>`                                | model card (required)  |
| Qwen3-Embedding-4B / 0.6B | raw text                  | `Instruct: <task>\nQuery: <text>` (instruction-aware) | model card             |
| bge-m3 (dense)            | raw text                  | raw text                                              | model card (no prefix) |

## Benchmark implication

Run **two Qwen arms** and **two nomic arms** where feasible — _prefixed_
(model-recommended) and _unprefixed_ (production parity) — so the attribution
table separates "model quality" from "we were embedding queries wrong." The
prefixed-vs-unprefixed delta is likely a chunk of BUG-1016 and is a config fix
independent of which model wins.
