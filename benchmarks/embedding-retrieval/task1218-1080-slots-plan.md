# TASK-1218 — GTX 1080 model and slot comparison

## Question

Can Qwen3-Embedding-0.6B F16 increase aggregate embedding throughput on manu's GTX 1080, especially with more serving slots, without an unacceptable retrieval-quality loss versus the current 4B Q8_0? The user will judge the tradeoff from measured results unless a numerical quality gate is selected before execution.

## Window and isolation

The user authorized execution now after preparation, and chose to judge the reported quality tradeoff afterward. Budget up to 140 minutes including restoration, with the benchmark interrupted at 130 minutes to reserve recovery time; measure actual outage start/end. The earlier 90-minute estimate was revised after counting 2.57 million tokens per full quality pass. Both production and test OpenViking share the production embedder and will be unable to embed during this window. This experiment does not change their collections, vector dimensions, or configuration.

Pre-stage the small GGUF and verify both model hashes before scaling production down. Use separate benchmark Pods on manu, outside the production Service selector, and direct loopback port-forwards. Only one GPU model runs at a time. Preserve the production Deployment's exact template and replica count. A coordinator must restore that replica count in its cleanup path on success, failure, or interruption, then verify model health, a real embedding, Service endpoints, OV readiness, and manifest diff. Never route 1,024-dimensional candidate vectors into the production 2,560-dimensional collection.

## Controlled matrix

| Model | Slots | Total context | Context per slot | KV |
| --- | --- | --- | --- | --- |
| 4B Q8_0 | 1 | 8,192 | 8,192 | F16 |
| 0.6B F16 | 1 | 8,192 | 8,192 | F16 |
| 0.6B F16 | 2 | 16,384 | 8,192 | F16 |
| 0.6B F16 | 4 | 32,768 | 8,192 | F16 |

Pin the currently running production image digest. Keep batch/ubatch 2,048, last-token pooling, full GPU offload, four CPU threads, flash attention, and `--cache-ram 0`. Record actual server slot/context properties and GPU residency before measuring. Reject OOM, CPU fallback, context rejection, nonfinite/wrong-dimension vectors, and incomplete arms; do not silently reduce context or change quantization to make a cell pass.

## Measurements

1. **Component latency:** the frozen 44-text sample, sequential, reporting every size bucket with cold-start observation separated.
2. **Throughput versus concurrency:** the same fixed workload at client concurrency 1, 2, and 4 for each server configuration, three deterministic shuffled passes. Report requests/s, actual response-token counts/s, wall time, request latency distributions, failures, and server metric deltas. Compare identical workload/order seeds. Slots and client concurrency are separate axes; extra slots alone do not establish throughput scaling.
3. **Queue interference:** repeat short-behind-long and long-behind-short with a controlled stagger, recording request start/end times and server queue metrics. This checks whether higher aggregate throughput also helps the short query tail.
4. **Quality:** score the same 1,280-entry frozen corpus and 34-positive/four-negative query fixture for each configuration. Freeze effective input lengths from the completed paired Mac arms; fail rather than silently changing inputs. Generate candidate vectors under the tested concurrency, retain raw vectors/query vectors, and compare per-query gains/losses. Report the coarse one-query resolution (2.94 percentage points) and the fixture's known broad-match/negative-abstention limits. This is whole-entry quality, not acceptance of OV's chunked retrieval.
5. **Resources:** sample labeled CPU temperatures, GPU temperature/utilization/power/VRAM, process RSS/high-water mark, and CPU load. Capture available throttling counters and concurrent node workloads. Manu retains a conservative 80°C CPU / 85°C GPU guard, given its distinct CPU and recorded thermal history; wemby's 95°C setting does not transfer to manu. Abort on two consecutive over-limit samples or lost telemetry.

## Decision boundaries

- Keep current production configuration after the experiment, regardless of the winning benchmark cell.
- Report smaller-model loss and slot-induced differences separately; do not call the existing two-hit difference statistically conclusive.
- The real OV retrieval, loaded 500 ms recall gate, and re-index cost remain separate acceptance work.
- Increased server throughput is only usable if OV's embedding concurrency can feed it; the current per-component limit of one must be assessed in an isolated OV test before any production tuning.
- Wemby remains excluded from sustained inference.

## Execution outcome — 2026-09-23

This is the original plan, not a claim that every cell completed. The measured
0.6B speedup was about 2.87×, making the full four-arm corpus matrix too long
for the estimated 140-minute window. Timmy also lost power during the 0.6B
quality pass; the user reported an accidental power-button press and rebooted
it. The 4B arm completed, both one-slot component sweeps completed, and 0.6B
quality stopped at 894/1,280 documents before query scoring.

After verified production recovery, a bounded four-slot finish ran sequential
latency, three four-client throughput passes, and fresh-input queue probes.
It omitted full quality and client-concurrency 1/2 sweeps; the two-slot arm was
not run. Production was finally restored at 20:33:28 UTC, before the original
20:42:48 deadline. The requested longer window was not approved or used.

See [the measured report](task1218-1080-slots-20260923.md) for complete results,
remaining gates, raw evidence, and both restoration records. Size any future
full-matrix window from these measured runtimes rather than reusing the initial
140-minute estimate.
