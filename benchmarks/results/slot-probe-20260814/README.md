# Runner slot-count probe — 2026-08-14

Settles the open question from the timmy matrix's finding 8: did the models that
showed a "serialization signature" actually receive the 8 slots the benchmark
requested?

**They did not.** Slot allocation correlates perfectly with observed scaling.

## Method

Deployment `ollama` in ns `llama` patched to the benchmark posture
(`OLLAMA_NUM_PARALLEL=8`, `OLLAMA_CONTEXT_LENGTH=32768`, `MAX_LOADED_MODELS=1`,
`KV_CACHE_TYPE=q8_0`, Vulkan on the discrete RX 9070 XT), then each model loaded
once at the `num_ctx` its benchmark row used, reading the llama.cpp runner's
`n_seq_max` and per-slot `new slot, n_ctx` lines from the ollama container log.
Deployment restored to `NUM_PARALLEL=2` / `CONTEXT_LENGTH=131072` afterwards and
FIM residency re-warmed.

## Result

| Model | Requested `num_ctx` | `n_seq_max` | Slots × per-slot ctx | Scaled in the matrix? |
| --- | ---: | ---: | --- | --- |
| `deepseek-coder-v2:fim` (prod warm, control) | 16384 | **8** | 8 × 16384 | n/a |
| `gemma4:12b-it-qat` | 32768 | **8** | 8 × 32768 (total 262144) | **yes — 2.2×** |
| `qwen3.5:9b-q4_K_M` | 32768 | **1** | 1 × 32768 | no — "flat" |
| `qwen3.5:9b-q4_K_M` | 16384 | **1** | 1 × 16384 | no — "flat" |
| `nemotron-3-nano:4b-bf16` | 16384 | **1** | 1 × 16384 | no — "flat" |

Every model that scaled got 8 slots. Every model that "serialized" got exactly
one. A single-slot runner cannot batch by construction, so the flat aggregate,
the ITL constant to four decimals, and the strict-FIFO queue arithmetic are all
fully explained by slot allocation — no model property is required.

## Clean re-run — co-residency confound ruled out

The first pass loaded each model shortly after evicting the pinned FIM model
(`MAX_LOADED_MODELS=1`, `KEEP_ALIVE=Forever`, so `deepseek-coder-v2:fim` keeps
re-warming). Eviction is asynchronous and the scheduler sizes slots against free
VRAM *at load time*, so a not-yet-released allocation could in principle have
capped the slot count. The probe was therefore re-run and the scheduler's own
VRAM accounting captured per load (`clean-rerun/ollama-pod-full.log`, pod
`ollama-6d855bc45f-khd6s`).

**Every one of the five loads reports the same device state:**

```text
msg="gpu memory" id=0 library=Vulkan available="15.4 GiB" free="15.9 GiB" minimum="457.0 MiB" overhead="0 B"
```

That is a free 16 GB card at each load — nothing was co-resident. Results are
identical to the first pass:

| # | Load | `n_seq_max` | `n_ctx` | Slots |
| --- | --- | ---: | ---: | --- |
| 1 | `deepseek-coder-v2:fim` @16K (startup warm) | **8** | 131072 | 8 × 16384 |
| 2 | `qwen3.5:9b-q4_K_M` @32K | **1** | 32768 | 1 × 32768 |
| 3 | `gemma4:12b-it-qat` @32K | **8** | 262144 | 8 × 32768 |
| 4 | `nemotron-3-nano:4b-bf16` @16K | **1** | 16384 | 1 × 16384 |
| 5 | `qwen3.5:9b-q4_K_M` @16K | **1** | 16384 | 1 × 16384 |

Ordering also argues against a residual-memory explanation independently of the
VRAM lines: the model that received **8** slots (gemma4) was loaded *between*
two models that received **1**. A lingering allocation would have penalised the
middle load, not spared it.

### The cap is set before the fitting step, not by it

For the loads that got 8 slots the fitter reports it had room to spare, e.g.
gemma4: `projected to use 5567 MiB of device memory vs. 16185 MiB of free device
memory` … `will leave 10617 >= 1919 MiB of free device memory, no changes
needed`. The qwen3.5 loads report the same "no changes needed" outcome — yet
arrive at the context construction with `n_seq_max` already equal to 1. Nothing
was reduced to make anything fit; the single-slot value is chosen upstream, in
Ollama's scheduler, before llama.cpp's parameter fitting runs.

## Why this is not simply "the KV did not fit"

`gemma4:12b-it-qat` is the **larger** model (8.0 GB resident vs qwen3.5's 6.1 GB)
and still received 8 slots at the **larger** context — 262144 total tokens of KV
versus the 16384 qwen3.5 was given. Whatever caps qwen3.5 and nemotron at
`n_seq_max = 1` is model-specific scheduler behavior, not a simple VRAM ceiling.
Diagnosing that cause is separate follow-up work; this probe establishes only
that the cap exists and that the affected rows do not measure what they claim.

## Consequences

- The qwen3.5 mixed and agentic rows, and the nemotron bf16 mixed row, are
  **not valid concurrency-scaling measurements**. They are single-slot
  measurements taken under an 8-slot label.
- The matrix's "gemma4 is the only model measured that scales under concurrency"
  is unsupported: the other two were never given the opportunity to batch.
- `nemotron_h` SSM serialization remains independently supported by the pop MLX
  result (INFO-1140), but **this Vulkan row is not evidence for it** — a
  one-slot runner would look identical.
- Solo-latency and single-stream decode figures are unaffected: at C=1 every row
  used one slot regardless.

## Reproduction

```sh
kubectl -n llama set env deploy/ollama OLLAMA_NUM_PARALLEL=8 OLLAMA_CONTEXT_LENGTH=32768
kubectl -n llama rollout status deploy/ollama
kubectl -n llama port-forward svc/ollama 11499:11434 &
curl -s -X POST http://localhost:11499/api/generate \
  -d '{"model":"<tag>","prompt":"hi","stream":false,"options":{"num_ctx":<ctx>,"num_predict":1}}'
kubectl -n llama logs <ollama-pod> -c ollama --tail=4000 |
  grep -Ei 'n_seq_max|new slot, n_ctx'
# restore
kubectl -n llama set env deploy/ollama OLLAMA_NUM_PARALLEL=2 OLLAMA_CONTEXT_LENGTH=131072
```

`run-vulkan-benchmark-jobs.sh` now captures these lines automatically — the grep
in `capture_backend_proof` was widened to keep `n_seq_max` / `n_ctx_per_seq` /
`new slot`, which it previously discarded. Every future row proves the
parallelism it actually received.

## Artifact provenance

- `clean-rerun/ollama-pod-full.log` — the **authoritative** evidence: a real
  `kubectl logs` capture of pod `ollama-6d855bc45f-khd6s`, taken *before* the
  restore rollout, containing all five loads with their per-load VRAM
  accounting.
- `runner-slot-lines.log` — first pass, **transcribed** from the session rather
  than re-captured: that probe's restore rolled the pod and destroyed its
  container log before it was saved. Retained because it is an independent
  replication (different pod, different load order, same result); prefer the
  clean-rerun log when auditing.
