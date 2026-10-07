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
- **Unprefixed is the production-equivalent comparison**, because OV's `openai` provider cannot add text prefixes. There EmbeddingGemma 2 is level with Qwen at top-1 (+1, within one query) and ahead at top-5 (+4). Top-1 correctness differs on nine queries: EmbeddingGemma 2 alone gets E2, U1, D4, S5 and H3; Qwen alone gets U6, D1, S6 and X1. The returned top-1 entry itself differs more often, on 19 of 34 positives (23 of 38 queries): where both are wrong, they are mostly wrong in different ways.
- **The model's prompts add three top-1 hits** (22/34), but only behind a prefix shim.
- **bf16 vs mxfp8**: within one query on both measures, with the same top-1 on 36 of 38 queries. Quantization is not distorting the result.
- **256 dimensions** costs three top-1 hits and one top-5 hit against 768-dimension mxfp8 (19/28 to 16/27).
- **Score thresholds need recalibrating, not ruling out.** EmbeddingGemma 2's top-1 scores sit in a high, narrow band: the four negative queries score 0.63–0.73 (0.59–0.67 with prompts) against a weakest positive of 0.725. Qwen's negatives are 0.27–0.51 against a weakest positive of 0.43 and a median of 0.76. So the fixture thresholds 0.5/0.55/0.6 reject almost no EmbeddingGemma 2 negatives, and OV recall's plugin `scoreThreshold` (0.35 today) and the relevance gate being calibrated on Qwen's scores (compendium IMPR-1208) would not carry over to a switch. In rank terms EmbeddingGemma 2 separates this fixture no worse: the highest cutoff that rejects every negative keeps 33 of 34 positives for unprefixed 768-dimension EmbeddingGemma 2 (31 of 34 for Qwen), and the prompted and 256-dimension arms separate positives from negatives completely. The margins are thin (0.002 for unprefixed mxfp8), the fixture has only four negatives, and these cutoffs are fitted after the fact, so any gate needs its own validation on labelled recall data.
- **Overflow**: 9 documents exceed the 30,000-character cap for both models. Five EmbeddingGemma 2 inputs then overflowed 8,192 tokens and were retried at 60%: three cut from 30,000 to 18,000 characters, and two below the cap (28,845 to 17,307 and 29,484 to 17,690). Qwen shrank two (30,000 to 18,000 and 28,845 to 17,307).

## Latency (same hardware only)

`latency_local.py` sent the 44-text latency sample (`~/ov-pilot-20260921/06-trial/embedder-exp/sample.json`, SHA-256 recorded in each result) to each server, one request at a time, three repeats. Each request carried a fresh random 16-character prefix so no prompt cache could serve it. Both servers handle one request at a time: llama-server runs `--parallel 1`, and pop's Ollama runs `OLLAMA_NUM_PARALLEL:1` (its server-config log line). The queueing numbers confirm it: in both, a short query sent 100 ms after a long text finishes only after the long one does.

| Median latency | EmbeddingGemma 2 mxfp8 (Ollama) | Qwen 4B Q8_0 (llama-server) | Ratio |
| --- | --- | --- | --- |
| 300-token target (36 requests) | 18.2 ms | 92.3 ms | 5.1× |
| 1,000 (30) | 36.8 ms | 259 ms | 7.0× |
| 3,000 (30) | 116 ms | 961 ms | 8.3× |
| 6,000 (18) | 332 ms | 2,419 ms | 7.3× |
| 7,600 (18) | 468 ms (15 ok, 3 overflow) | 3,440 ms | 7.3× |
| Short query alone (234 tokens) | 18.3 ms | 76.6 ms | 4.2× |
| **Short query behind a 7,600-token text** | **425 ms** | **3,418 ms** | **8.0×** |
| First request | 760 ms, model-cold (unloaded beforehand) | 68 ms, server already loaded | — |

- Token counts are each model's own (EmbeddingGemma 2's tokenizer gives about 7% more tokens on this sample). One 7,600-target text (sample index 38) is 8,192+ Gemma tokens and fails with `the input length exceeds the context length` on every repeat; it is excluded from that bucket's median.
- The queueing test uses sample index 39 as the long text for both models (7,600 Qwen tokens, 8,141 Gemma tokens), so it fits both windows. A first pass used the sample's longest text, index 38, which overflowed EmbeddingGemma 2 instantly and made its short query look unqueued; that pass was discarded and `latency_local.py` now drops any queueing run with a failed request.
- **Dispatch order is recorded.** The latency tables come from a 2026-10-07 re-run with the fixed runner, which waits for the first request's dispatch, records each request's start and end and the actual gap, and drops a sample whose order reversed or whose short query went out after the long one had finished. In all four configurations every pair went out in order, 100.4–105.2 ms apart, and none was dropped. The first runs, which lacked those checks (the queueing numbers were 422 / 3,401 / 513 / 2,669 ms; git history keeps the files), agree with the re-run within 3% everywhere.
- The slow-recall tail is a short query waiting behind a long document. On this hardware EmbeddingGemma 2 shortens that wait about eightfold, in line with its size.

## GGUF on llama.cpp (2026-10-07)

The path a cluster card would use: the official `ggml-org/embeddinggemma-2-GGUF` files (Q8_0 SHA-256 `2188ac1d…09135`, BF16 `68bae29d…f216`) on a llama.cpp b11472 release build, `serve_local.sh eg2q8` / `eg2bf16` (mean pooling, one slot, context and batch 8192). Support first ships in **b11454** (ggml-org/llama.cpp#30054, merged 2026-10-06); pop's Homebrew build b11146 predates it, so the run used `LLAMA_SERVER` pointing at the release binary.

| Arm | Top-1 | Top-5 |
| --- | --- | --- |
| GGUF Q8_0, unprefixed | 20/34 | 27/34 |
| GGUF Q8_0, model-card prompts | 22/34 | 26/34 |
| GGUF BF16, unprefixed | 20/34 | 27/34 |

- **Matches the Ollama/MLX run within a query.** GGUF Q8_0 and Ollama mxfp8 pick the same top-1 entry on 33 of 34 positives; BF16 scores exactly as Ollama's bf16. The negatives score the same 0.63–0.73, so the threshold-calibration caveat carries over.
- **Overflow message differs.** For this mean-pooling model llama-server rejects an over-long input with HTTP 500 `input (N tokens) is too large to process. increase the physical batch size`, not the causal models' `exceeds the available context size`; the harness now treats both as overflow. Production would hit this wherever OV's cap lets more than 8,192 Gemma tokens through.

Latency, both models on the same b11472 build, one slot each:

| Median latency | EmbeddingGemma 2 GGUF Q8_0 | Qwen 4B Q8_0 | Ratio |
| --- | --- | --- | --- |
| 300-token target | 13.3 ms | 90.0 ms | 6.8× |
| 1,000 | 37.2 ms | 237 ms | 6.4× |
| 3,000 | 146 ms | 826 ms | 5.6× |
| 6,000 | 416 ms | 1,921 ms | 4.6× |
| 7,600 (EG2: 15 ok, 3 overflow) | 602 ms | 2,653 ms | 4.4× |
| **Short query behind a 7,600-token text** | **531 ms** | **2,731 ms** | **5.1×** |

b11472 is itself faster for Qwen than Homebrew b11146 (2,731 vs 3,418 ms behind a long text), so compare within a table, not across them.

## Limits

- Whole-entry retrieval on 34 positives. It is not the OV chunked-collection arm, the loaded-recall 500 ms gate, or a re-index measurement, all of which TASK-1218 still requires.
- Apple Silicon timings do not transfer to the GTX 1080 in production. A cluster deployment would also need the embedder image on llama.cpp b11454 or newer.

## Files

- `results/task1218-20261006/` — per-arm retrieval results (`eg2-*.json`, `qwen3-4b-unprefixed.json`) and latency (`latency-*.json`).
- `results/task1218-20261007/` — GGUF retrieval (`eg2-gguf-*.json`) and b11472 latency for both models.
- `latency_local.py` — the latency runner.
- Not committed: `scratch/task1218-20261006/` (run scripts, logs, the 2026-09-23-runner raw vectors and its `result.json`, 18/34 and 24/34).
