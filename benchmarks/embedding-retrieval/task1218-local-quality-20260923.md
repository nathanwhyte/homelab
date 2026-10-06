# TASK-1218 — isolated Mac retrieval comparison, 2026-09-23

This is a preliminary whole-entry comparison of unprefixed Qwen3-Embedding-4B Q8_0 and Qwen3-Embedding-0.6B F16. It does not satisfy TASK-1218's CUDA component-latency, fresh-collection OV retrieval, loaded recall-latency, or re-index acceptance criteria. Production and ov-test were not changed.

## Method and evidence

- Frozen input: the existing local `corpus.jsonl`, 1,280 entries, copied into `scratch/task1218-20260923/corpus.jsonl`. This is a cached fixture snapshot, not a fresh export of the live vault or OV corpus.
- Corpus SHA-256: `2c5256873ecd70175a0c0b7bf0a6e74d43d3b149f46c4af9979b6273d619e732`.
- Gold fixture: `eval_groundtruth_2026-07-04.json`, SHA-256 `33a171c79bac1d996ee17ffa8f271bd4fccc50c9df4be420d12d08131dc4b3dd`; 34 positive and four negative queries. The legacy `R2` broad-report query accepts any `bug-` identifier; this metric inherits that limitation. Negatives are retained with rankings but no abstention threshold is evaluated.
- `benchmark_local_quality.py` runs the models sequentially on Mac Metal using llama.cpp build 10964, commit `b29c606e2`. Each server uses last-token pooling, one slot, context/batch/ubatch 8192, and `--cache-ram 0`; it binds loopback and stops in a `finally` block. These differ from the production CUDA build and batch size. Elapsed Mac runtime is not a GPU-card latency comparison.
- Documents are capped at 30,000 characters, matching the older fixture harness. A confirmed context-overflow response retries with 60% of the preceding characters, also matching the older harness. Other HTTP errors fail the run. The actual input hash and length are saved per document. This is not OpenViking's production truncation policy.
- Exact cosine ranking is computed in memory. No PostgreSQL tables or OV collections are written. Corpus vectors and query vectors are retained for independent ranking replay.
- Models were downloaded from the official [0.6B GGUF repository](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/tree/main) and [4B GGUF repository](https://huggingface.co/Qwen/Qwen3-Embedding-4B-GGUF/tree/main). The result files record the SHA-256 of the actual downloaded GGUFs.

## Results

| Model | Top-1 | Top-5 | Status |
| --- | --- | --- | --- |
| 0.6B F16, unprefixed | 16/34 (47.1%) | 23/34 (67.6%) | Complete |
| 4B Q8_0, unprefixed | 18/34 (52.9%) | 24/34 (70.6%) | Complete |

Each arm produced 1,280 finite vectors, with dimensions 1,024 and 2,560 respectively. Every effective document input hash matches between arms. Independent cosine replay reproduced all 76 saved top-five rankings. Ten documents were shortened identically: eight only at the 30,000-character cap; `feat-020` and `proj-1006` also hit the context-overflow retry. The initial fail-on-overflow trial stopped after 239 documents and is excluded; its partial evidence remains under `scratch/task1218-20260923/0.6b/`.

Compact results: `results-local/task1218-20260923-qwen06.json` and `results-local/task1218-20260923-qwen4b.json`. Raw inputs, server logs, vectors, and query vectors are local ignored artifacts under `scratch/task1218-20260923/`; the compact exports include raw-file hashes. Retain that directory if removing this worktree: compact rankings alone cannot reconstruct the original input corpus or vectors.

The paired top-1 changes are four queries favoring 4B (`D1`, `S4`, `H1`, `R3`) and two favoring 0.6B (`U1`, `S3`); net 4B advantage is two hits, or 5.9 percentage points. At top-five, three favor 4B (`D4`, `X3`, `R2`) and two favor 0.6B (`U1`, `H3`); net advantage is one hit, or 2.9 points. This small fixture establishes those observed differences, not equivalence or a production-quality verdict. Keep the model-switch decision open until the fresh-collection OV arm and loaded recall gate are measured.

## Historical comparison

The apparent 0.6B score discrepancy in the handoff has an identifiable source: `results/qwen3-0.6b-unprefixed.json` records 13/34 top-1 on 1,097 entries (Mac), whereas `results-cluster/qwen3-0.6b-unprefixed.json` records 12/34 on 1,099 entries (cluster). TASK-1218 correctly cites the latter. Neither old run is the paired baseline for today's 1,280-entry comparison; the changed corpus and runtime prevent attributing cross-run differences to the model alone.

## GTX 1060 isolated probe — partial, thermal abort

The user confirmed wemby's power issue was repaired and tested, then explicitly authorized the isolated Pod and its cleanup. The probe used the production image digest, the same verified 4B Q8_0 GGUF, one slot, context 8192, batch/ubatch 512, and `--cache-ram 0`. Its unique label was outside the production Service selector. GPU residency was verified before sending requests.

The run began at 2026-09-23 16:36:21 UTC and returned **20 of 44** embeddings. At 16:36:43 and 16:36:48 UTC, consecutive CPU readings reached 80°C and 82°C, triggering the configured thermal guard. The guard deleted the Pod during the next request; the client reported `RemoteDisconnected`. This is a thermal abort, not a completed benchmark or evidence of another power failure. The maximum sampled GPU temperature was 67°C. Correction: the 80°C cutoff was overly conservative for wemby’s i7-8750H, whose [Intel Tjunction specification is 100°C](https://www.intel.com/content/www/us/en/products/sku/134906/intel-core-i78750h-processor-9m-cache-up-to-4-10-ghz/specifications.html). These readings do not establish faulty cooling. The user authorized a retry with a 95°C CPU guard; the 85°C GPU guard and two-consecutive-sample rule remain.

| Target tokens | Completed | Median | Range |
| --- | --- | --- | --- |
| 300 | 12 | 603 ms | 464–2,689 ms |
| 1,000 | 8 | 2,035 ms | 2,006–2,155 ms |

The first request includes cold-start overhead; one 300-target text contains 234 tokens. The 1,000 bucket is incomplete, and the 3,000–7,600 buckets were not reached. These client timings include port-forward/network overhead and concurrent telemetry. No full-sample throughput or long-input latency is established.

All 20 returned vectors had 2,560 dimensions. Cosine against the historical 2026-09-22 GTX 1080 vectors was at least **0.9997415** (the 12 shortest were identical). The baseline and sample hashes are retained; sample order is inherited from the old experiment. Different batch sizes and the historical control mean this does not isolate card-specific drift or establish long-input consistency.

Observed peaks: GPU memory **5,631/6,144 MiB**, sampled server RSS **1,411 MiB**, and process lifetime RSS high-water mark **4,424 MiB** (already present in the first sample, including model loading). The latter is not steady serving RSS. Only six telemetry samples were collected. Post-run metrics and server logs were unavailable after automatic deletion; raw partial vectors, initial metrics, runtime identity, and telemetry remain in `scratch/task1218-20260923/gtx1060-probe/result.json`. The compact export is `results-local/task1218-20260923-gtx1060-partial.json`.

Cleanup verified: the probe Pod is absent, all three nodes are Ready, production and test OV are Ready, the only production embedder endpoint is still the existing Pod on manu, and `kubectl diff` against the main embedder manifest is empty. No production inference requests were sent for this experiment.

## Authorized retry — CPU guard 95°C

The user requested a retry with a higher CPU threshold. The runner now records the guard settings with the result: CPU **95°C**, GPU **85°C**, two consecutive samples, nominal five-second sampling. The 95°C threshold is a chosen experimental margin below the i7-8750H's 100°C Tjunction specification, not a manufacturer-mandated shutdown point. Model, Pod settings, frozen sample, and historical baseline were unchanged.

The retry started at **2026-09-23 17:08:42 UTC**. It completed **23/44** responses, then the CPU reached **95°C at 17:09:19 and 17:09:25 UTC**. The runner deleted the Pod during request 24. GPU temperature peaked at **73°C**, so it did not trigger the stop. The saved server log shows orderly cleanup after deletion. This establishes another script-guard stop; hardware throttling or shutdown was not measured, and cooling failure is not established.

| Target tokens | Completed | Median latency |
| --- | --- | --- |
| 300 | 12 | 607 ms |
| 1,000 | 10 | 2,023 ms |
| 3,000 | 1 | 7,083 ms (single observation) |

Minimum cosine against the historical GTX 1080 baseline was **0.9996807** across the 23 completed responses. VRAM peaked at **5,631 MiB**, sampled RSS at **1,412 MiB**, and lifetime RSS high-water mark at **4,423 MiB**, including loading. Nine telemetry samples were collected. The 6,000/7,600 buckets, full-sample throughput, and post-run metrics remain unavailable. The same historical-control and client-timing caveats apply.

Raw vectors, telemetry, and server log are preserved separately at `scratch/task1218-20260923/gtx1060-retry95/`; compact results and raw hashes are in `results-local/task1218-20260923-gtx1060-retry95.json`. Cleanup checks again confirmed the probe absent, all three nodes Ready, prod/test OV Ready, the unchanged production endpoint on manu, and an empty production embedder manifest diff. No additional retry was attempted.

## Remaining work

1. The local control and paired-input/ranking verification are complete; preserve the raw evidence alongside the compact exports.
2. Prepare the CUDA component arm for the shared 44-text sample. The existing `cluster/throughput-cuda-run.sh` only selects 4B quantization, and its template lacks `--cache-ram 0` and uses a floating image tag. Do not run it unmodified as TASK-1218 evidence. Pin the production image/build, use one slot and production batch settings, retain thermal guards, and record both short/long orders plus VRAM and RSS peaks.
3. The 95°C retry also hit the script guard. Before another sustained attempt, inspect labeled CPU sensors, actual throttling indicators, and concurrent host load to choose the next experiment; the temperature samples alone do not diagnose cooling failure. A later 1080 acceptance leg interrupts the production embedder and needs explicit authorization plus restore/readiness checks.
4. Run each surviving model through ov-test with a fresh collection, unprefixed fixture and gold-query scoring, representative indexing traffic, recall p50/p95/p99, OV semaphore wait, and llama.cpp queue metrics. Measure re-index time and coverage there. Keep the 500 ms gate unchanged.
5. IDEA-1109 now has partial short-input cosine evidence; long-input consistency and capacity remain unverified. The IDEA-1100 outage ledger records the handoff incidents with affected-turn recovery explicitly unverified; current pod readiness and an empty embedder manifest diff do not close that gap.

## Reproduction

From this worktree, after downloading the named GGUFs to `models/qwen06/` and `models/qwen4b/`, run one model at a time. Choose a fresh output directory; the runner refuses to overwrite one.

```bash
uv run --no-project python benchmarks/embedding-retrieval/benchmark_local_quality.py \
  --model 0.6b \
  --gguf benchmarks/embedding-retrieval/models/qwen06/Qwen3-Embedding-0.6B-f16.gguf \
  --corpus benchmarks/embedding-retrieval/scratch/task1218-20260923/corpus.jsonl \
  --ground-truth benchmarks/embedding-retrieval/eval_groundtruth_2026-07-04.json \
  --output benchmarks/embedding-retrieval/scratch/task1218-20260923/0.6b-rerun
```

For the control use `--model 4b`, `models/qwen4b/Qwen3-Embedding-4B-Q8_0.gguf`, a distinct output directory, and `--port 18083`. Compare input hashes before treating the results as paired.
