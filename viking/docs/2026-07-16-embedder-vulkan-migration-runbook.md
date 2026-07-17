# Embedder ROCm → Vulkan Migration Runbook (IMPR-1068)

**Date:** 2026-07-16
**What:** Cut the OpenViking embedder (`embedder-qwen`, Qwen3-Embedding-4B) on timmy's RX 9070 XT from the llama.cpp **ROCm** backend to **Vulkan**, matching Ollama's 2026-07-06 migration on the same card.
**Why:** The embedder is the last active consumer of the ROCm compute runtime on the 9070 XT; retiring it lets the ROCm toolchain (rocblas hostPath, HIP, `llama.cpp:server-rocm`) go. The GPU stays AMD RDNA4 — driver, device plugin, `amd-smi` exporter, fan control, dashboards are unaffected.

Plan: `~/code/compendium/docs/plans/2026-07-16-IMPR-1068-embedder-qwen-vulkan-migration.md`. Item: IMPR-1068.

## Preconditions

- Both manifests use `metadata.name: embedder-qwen` (IMPR-1040), so `kubectl apply` of the Vulkan manifest **reconfigures the existing Deployment in place** — it does not create a second one. `strategy: Recreate` tears down the ROCm pod, then starts the Vulkan pod (clean single-GPU swap).
- GPU access stays `privileged` (Ollama holds both `amd.com/gpu` device-plugin units; the embedder never used the plugin).
- Confirm no active OpenViking indexing burst is mid-flight (a brief embedder outage during Recreate stalls the queue; it resumes on Ready).

## Cutover

```bash
cd ~/code/homelab

# 1. Apply the Vulkan manifest — reconfigures embedder-qwen in place.
kubectl apply -f viking/manifests/embedder-qwen-vulkan-deployment.yaml

# 2. Watch the Recreate swap (old ROCm pod terminates, Vulkan pod starts).
kubectl rollout status deploy/embedder-qwen -n viking --timeout=300s
kubectl get pods -n viking -l app=embedder-qwen -o wide

# 3. Follow startup until /health serves.
kubectl logs deploy/embedder-qwen -n viking -f
```

## Validation gates (all must pass before Phase 3 cleanup)

### Gate 1 — health + correct device

```bash
kubectl get pod -n viking -l app=embedder-qwen \
  -o jsonpath='{.items[0].status.containerStatuses[0].ready}{"\n"}'   # -> true
# /health from inside the cluster:
kubectl run ov-embed-probe --rm -it --image=curlimages/curl:8.12.1 -n viking --restart=Never -- \
  curl -sf http://embedder-qwen.viking.svc:8080/health && echo OK
```

In `kubectl logs deploy/embedder-qwen -n viking`, confirm:

- The selected **Vulkan device is the RX 9070 XT**, not the AMD iGPU (look for the `ggml_vulkan: Using ... Radeon RX 9070 XT` device line; if it names the iGPU, `GGML_VK_VISIBLE_DEVICES` is wrong).
- All layers offloaded to GPU (`--n-gpu-layers 999`; `load_tensors: offloaded 37/37 layers to GPU` or similar).

### Gate 2 — flash-attn on Vulkan/gfx1201

- The manifest ships `--flash-attn on`. If the Vulkan backend rejects FA at startup (llama.cpp Vulkan FA has been partial historically), the log shows an FA error / the server fails to start.
- **If it errors:** edit the manifest to `--flash-attn off`, re-apply, and record the fallback here and in IMPR-1068. (Ollama runs FA on this card under Vulkan, so this is expected to pass.)

### Gate 3 — retrieval quality (go/no-go)

Same model + quant + pooling ⇒ Vulkan embeddings are fp-equivalent to the ROCm baseline, so **no re-index is expected**. This gate proves the stored (ROCm-generated) `ov-vectordb` vectors still retrieve correctly against Vulkan-generated query vectors.

```bash
cd ~/code/homelab/benchmarks/embedding-retrieval
# Run the retrieval eval against eval_groundtruth_2026-07-04.json and compare
# recall/nDCG to the ROCm baseline in BENCHMARK.md (see run-all.sh).
./run-all.sh   # or the documented eval entrypoint
```

- **PASS** (no meaningful recall/nDCG regression): migration valid; proceed to soak, then Phase 3.
- **FAIL**: roll back and investigate before retrying (do NOT proceed to cleanup).

## Rollback

```bash
# Restore the ROCm embedder in place (Recreate swap back).
kubectl apply -f viking/manifests/embedder-qwen-rocm-deployment.yaml
kubectl rollout status deploy/embedder-qwen -n viking --timeout=300s
```

Secondary lever: `embedder-qwen-cuda-deployment.yaml` (replicas=0 rollback on another node), per the 2026-07-06 runbook.

## After soak (Phase 3 — separate PR)

Only once the gates are green and the soak window has passed: swap the `kustomization.yaml` entry to the Vulkan manifest, delete the ROCm manifest + ROCm-only tooling, and flip the active-tense ROCm docs (`CLAUDE.md` topology, `reference/service-routing.md`, `reference/llm-config.md`, the rocblas guardrail in `HARDWARE.md`). Historical ROCm docs stay as history. See the plan's Phase 3.
