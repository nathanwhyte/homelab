# Plan review — Benchmark nemotron-3-nano:4b-bf16 on timmy's RX 9070 XT (TASK-1013)

Reviewed against the current `main` worktree (`b9fa427`) and live cluster state
(read-only `kubectl` checks only; nothing was mutated).

## Verified correct

- **Phase 0 drift is real.** Live deployment `ollama` in `llama` has
  `OLLAMA_VULKAN=1` and `OLLAMA_LLM_LIBRARY=vulkan` in its env; the repo
  manifest `llama/ollama-deployment.yaml` has neither. The manifest's
  device-selection comment (lines 117-122) only mentions the `GGML_VK_VISIBLE_DEVICES`
  default, not the explicit Vulkan env. Adding both + updating the comment is
  the right reconciliation.
- **ConfigMap staleness is real.** `rebuild-configmap-vulkan.sh --check` fails:
  the tracked `benchmark-configmap-vulkan.yaml` embeds an older harness
  (`concurrency-bench.py` error-handling drift). Regenerating with `--no-apply`
  is correct and local (no cluster needed).
- **`rebuild-configmap-vulkan.sh` uses explicit `--from-file` lines** (lines
  49-54), not a glob — so a new config genuinely needs a new line added. Plan
  Phase 1 is right.
- **`run-vulkan-benchmark-jobs.sh` patches MAX_LOADED=1 / NP=8 and restores via
  trap** (lines 22-23, 156-167). The pod bounce clears the resident
  `deepseek-coder-v2:fim`, so eviction is handled automatically. Confirmed.
- **Open Q5 is real.** `run-cluster-np-sweep.sh` line 21 references
  `benchmarks/ollama/manifests/benchmark-pod.yaml`, which does not exist. It is
  already broken and unrelated — leaving it as-is is fine.
- **Open Q6 is real.** `run-vulkan-benchmark-jobs.sh` writes to
  `benchmarks/results/` and `capture_env` writes a fixed
  `cluster-vulkan-env.json` (line 140). See caveat below.
- **Tag exists.** `nemotron-3-nano:4b-bf16` is a real Ollama library tag
  (confirmed in TASK-1013 notes).

## Critical issue — config filename does not match the Job's glob

The Job `benchmark-vulkan-job.yaml` line 30 copies configs with:

```sh
cp /scripts/cluster-vulkan*.toml /bench/
```

The plan's Phase 1 proposes the filename
`cluster-nemotron-nano-bf16-default.toml`, which **does not match**
`cluster-vulkan*.toml`. The Job would not see it, and the run would fail with a
missing-config error. The plan itself flags this constraint in Phase 2 but the
Phase 1 name violates it.

Fix: name it `cluster-vulkan-nemotron-nano-bf16-default.toml` (matches the
glob, and keeps the vulkan-family grouping the ConfigMap/Job already assume).
The `prefix` inside the toml can stay `ollama-cluster-nemotron-nano-bf16-default`
for distinct output naming.

## Other issues / caveats

- **Phase 3 "Vulkan/ROCm discrepancy is resolved (Vulkan is live)" is premature.**
  The deployment env says Vulkan, but TASK-1013's 2026-08-11 note records the
  _running service_ as ROCm (`library=ROCm compute=gfx1201`, "experimental Vulkan
  support disabled"). The deployment manifest and the live service disagree.
  The Phase 2 preflight gate (Open Q7) is exactly the right way to settle this —
  but the "resolved" claim should be conditional on that gate passing, not
  asserted in Phase 3.
- **Open Q7 — recommend the log-grep over `ollama ps`.** `ollama ps`'s
  PROCESSOR column reports `100% GPU` regardless of backend; it does not
  distinguish Vulkan from ROCm. Grepping the pod logs for the
  `ggml_vulkan: Using ... Radeon RX 9070 XT` line is the only check that proves
  the Vulkan path actually engaged on the discrete GPU. (The manifest already
  sets `GGML_VK_VISIBLE_DEVICES=1` to exclude the iGPU, so the log line is the
  authoritative confirmation.)
- **Open Q6 — `cluster-vulkan-env.json` is a fixed filename.** Running the
  nemotron config through `run-vulkan-benchmark-jobs.sh` will overwrite the
  existing `cluster-vulkan-env.json` capture. If you want to preserve the
  prior gemma4 vulkan env record, either back it up first or use a separate
  dated output dir. Worth deciding before the run.
- **Open Q3 — VRAM math is worth confirming.** The manifest runs
  `OLLAMA_CONTEXT_LENGTH=32768` and `OLLAMA_KV_CACHE_TYPE=q8_0`. A bf16 4B
  (~8 GB) + 8-slot KV at 16384 ctx under a 16 GiB limit is plausible but tight;
  the task note's ~3.1 GB free figure is moot once the pod bounces (eviction
  frees the full card), so the real question is whether 8 slots at 16K fit
  alongside the 8 GB model. Confirm before locking the config.
- **Open Q1 — scope note.** The task's Goal 1 is a _smoke test_ (loads +
  coherent output), which is a correctness check. `concurrency-bench.py` is a
  throughput harness. It covers the throughput half of Goal 1 but not the
  "coherent output" half — the SSM_SCAN partial-support risk is specifically
  about _garbled output_, which throughput alone won't catch. If Goal 1 is the
  priority, a quick coherence check (e.g. a fizzbuzz-style prompt, as TASK-1132
  used on Metal) should accompany the throughput run.

## Summary

The plan is sound and well-grounded; the drift, staleness, and broken-script
claims all check out. One blocking fix (config filename must match
`cluster-vulkan*.toml`), one premature claim (Phase 3 "resolved"), and a few
open questions worth settling before the run (backend-confirmation method,
env-json overwrite, VRAM math, coherence check).
