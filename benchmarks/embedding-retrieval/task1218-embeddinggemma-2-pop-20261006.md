# TASK-1218 — EmbeddingGemma 2 on pop, 2026-10-06

Informational runs of EmbeddingGemma 2 (270M text model) against the production embedder, Qwen3-Embedding-4B Q8_0, on pop (Apple Silicon). TASK-1218 had parked EmbeddingGemma 2 for lack of a cluster host; these numbers go on record against its revisit triggers. Two of those triggers fired while this ran: llama.cpp merged EmbeddingGemma 2 support on 2026-10-06 (ggml-org/llama.cpp#30054) and official GGUFs exist (`ggml-org/embeddinggemma-2-GGUF`), so llama-server on manu's GTX 1080 is a possible host. Nothing in production or on the cluster changed.

## Method

- **Corpus**: the frozen 1,280-entry `corpus.jsonl` from the 2026-09-23 run, SHA-256 `2c5256873ecd70175a0c0b7bf0a6e74d43d3b149f46c4af9979b6273d619e732`. `benchmark_embedders.py` now refuses to run on any other corpus unless `BENCH_CORPUS_SHA256` is overridden.
- **Fixture**: `eval_groundtruth_2026-07-04.json`, 34 positive and 4 negative queries. One query is about 2.9 points of top-1.
- **Harness**: `benchmark_embedders.py` with a scratch pgvector, exact cosine, documents capped at 30,000 characters. On a context overflow a document is retried at 60% of its characters; results record every such shrink.
- **EmbeddingGemma 2**: Ollama 0.40.0 (MLX engine), tags `embeddinggemma-2:270m-mxfp8-text` and `:270m-bf16-text`, sent with `truncate: false` and `num_ctx` 8192 so an overflow fails rather than being cut silently.
- **Prompts**: the prefixed arm uses the model's own retrieval prompts, query `task: search result | query:` and document `title: none | text:`, from [`config_sentence_transformers.json`](https://huggingface.co/google/embeddinggemma-2/raw/main/config_sentence_transformers.json) on the official model page.
- **Qwen control**: the same 4B Q8_0 GGUF as 2026-09-23 (SHA-256 `b60ae5ce…416949d`, re-downloaded and matched), llama.cpp Metal, one slot, context 8192, run twice: through the 2026-09-23 runner `benchmark_local_quality.py` and through `benchmark_embedders.py` via `serve_local.sh qwen4b`.
- **One local model at a time**: every model was stopped before the next loaded.

## Retrieval quality

| Arm | Top-1 | Top-5 | Shrunk on overflow |
| --- | --- | --- | --- |
| Qwen3-Embedding-4B Q8_0, unprefixed (production) | 18/34 (52.9%) | 24/34 (70.6%) | 2 |
| **EmbeddingGemma 2 270M mxfp8, unprefixed** | **19/34 (55.9%)** | **28/34 (82.4%)** | 5 |
| EmbeddingGemma 2 270M mxfp8, model-card prompts | 22/34 (64.7%) | 27/34 (79.4%) | 5 |
| EmbeddingGemma 2 270M bf16, unprefixed | 20/34 (58.8%) | 27/34 (79.4%) | 5 |
| EmbeddingGemma 2 270M mxfp8, 256 dims, unprefixed | 16/34 (47.1%) | 27/34 (79.4%) | 5 |

- **The control reproduces 2026-09-23 exactly.** Both runners gave Qwen 18/34 and 24/34, and the same top-1 entry on every query, so the harness change did not move the baseline.
- **Unprefixed is the production-equivalent comparison**, because OV's `openai` provider cannot add text prefixes. There EmbeddingGemma 2 is level with Qwen at top-1 (+1, within one query) and ahead at top-5 (+4). Top-1 differs on nine queries: EmbeddingGemma 2 alone gets E2, U1, D4, S5 and H3; Qwen alone gets U6, D1, S6 and X1.
- **The model's prompts add three top-1 hits** (22/34), but only behind a prefix shim.
- **bf16 vs mxfp8**: within one query on both measures, with the same top-1 on 36 of 38 queries. Quantization is not distorting the result.
- **256 dimensions** costs three top-1 hits against 768 and nothing at top-5.
- **Thresholds don't transfer.** EmbeddingGemma 2's top-1 scores for the four negative queries are 0.63–0.73 (0.59–0.67 with prompts), overlapping its weakest positives (minimum 0.725). Qwen's negatives are 0.27–0.51 against a positive median of 0.76. The fixture thresholds 0.5/0.55/0.6 reject almost no EmbeddingGemma 2 negatives. So the "at least as good" reading covers ranking only: EmbeddingGemma 2 is worse at telling a question with no answer from one with an answer. That matters for OV recall, whose plugin `scoreThreshold` is 0.35 today and whose relevance gate (compendium IMPR-1208, in progress) is being calibrated on Qwen's score distribution. A model switch would invalidate that calibration, and on this fixture no absolute cutoff separates EmbeddingGemma 2's negatives from its positives; a relative gate (top score vs the runner-up) would need testing instead.
- **Overflow**: 9 documents exceed the 30,000-character cap for both models. On EmbeddingGemma 2, five of those still overflowed 8,192 tokens and were embedded from 18,000 characters; Qwen shrank two.

## Latency (same hardware only)

`latency_local.py` sent the 44-text latency sample (`~/ov-pilot-20260921/06-trial/embedder-exp/sample.json`, SHA-256 recorded in each result) to each server, one request at a time, three repeats. Each request carried a fresh random 16-character prefix so no prompt cache could serve it. Both servers handle one request at a time: llama-server runs `--parallel 1`, and pop's Ollama runs `OLLAMA_NUM_PARALLEL:1` (its server-config log line). The queueing numbers confirm it: in both, a short query sent 100 ms after a long text finishes only after the long one does.

| Median latency | EmbeddingGemma 2 mxfp8 (Ollama) | Qwen 4B Q8_0 (llama-server) | Ratio |
| --- | --- | --- | --- |
| 300-token target (36 requests) | 17.7 ms | 92.4 ms | 5.2× |
| 1,000 (30) | 35.9 ms | 254 ms | 7.1× |
| 3,000 (30) | 115 ms | 946 ms | 8.2× |
| 6,000 (18) | 332 ms | 2,418 ms | 7.3× |
| 7,600 (18) | 474 ms (15 ok, 3 overflow) | 3,414 ms | 7.2× |
| Short query alone (234 tokens) | 17.3 ms | 74.9 ms | 4.3× |
| **Short query behind a 7,600-token text** | **422 ms** | **3,401 ms** | **8.1×** |
| First request | 652 ms, model-cold (unloaded beforehand) | 64 ms, server already loaded | — |

- Token counts are each model's own (EmbeddingGemma 2's tokenizer gives about 7% more tokens on this sample). One 7,600-target text (sample index 38) is 8,192+ Gemma tokens and fails with `the input length exceeds the context length` on every repeat; it is excluded from that bucket's median.
- The queueing test uses sample index 39 as the long text for both models (7,600 Qwen tokens, 8,141 Gemma tokens), so it fits both windows. A first pass used the sample's longest text, index 38, which overflowed EmbeddingGemma 2 instantly and made its short query look unqueued; that pass was discarded and `latency_local.py` now drops any queueing run with a failed request.
- The slow-recall tail is a short query waiting behind a long document. On this hardware EmbeddingGemma 2 shortens that wait about eightfold, in line with its size. Whether that carries to production depends on hardware it cannot run on today.

## Limits

- Whole-entry retrieval on 34 positives. It is not the OV chunked-collection arm, the loaded-recall 500 ms gate, or a re-index measurement, all of which TASK-1218 still requires.
- Apple Silicon timings do not transfer to the GTX 1080 in production. Ollama's MLX path still needs Apple Silicon or CUDA 13+ on SM 7.5+, so a cluster run would go through llama.cpp's GGUF support instead, and these Ollama/MLX scores do not prove that path ranks the same.

## Files

- `results/task1218-20261006/` — per-arm retrieval results (`eg2-*.json`, `qwen3-4b-unprefixed.json`) and latency (`latency-*.json`).
- `latency_local.py` — the latency runner.
- Not committed: `scratch/task1218-20261006/` (run scripts, logs, the 2026-09-23-runner raw vectors and its `result.json`, 18/34 and 24/34).
