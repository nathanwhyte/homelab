# OV embedder migration — wemby (CUDA) → timmy (ROCm), 2026-07-06

## Why

wemby's charging cable/port keeps intermittently failing → the battery drains →
the node hard-drops (repeated `NodeStatusUnknown` flaps overnight 2026-07-05/06).
The **live OV embedder** (`embedder-qwen-cuda`, Qwen3-Embedding-4B) sits on wemby's
GTX 1060, so every wemby power loss takes OV's embedding path down with it.

Migrate the embedder to **timmy's RX 9070 XT** (ROCm) — the only node stable
through the incident, with 16 GB VRAM (ample headroom to co-reside with Ollama).
TASK-1122 validated this exact card: Qwen-4B on the 9070 XT reproduced the
wemby/Metal retrieval numbers exactly (55.9/73.5), so quality is backend-invariant.

**No vectordb re-embed needed:** same model, Q8_0 quant, `--pooling last`, 2560
dim → identical vectors. The Service endpoint (`embedder-qwen.viking.svc:8080`) is
unchanged, so OV config needs no edit.

## Files

- `viking/manifests/embedder-qwen-rocm-deployment.yaml` — new PRIMARY (replicas 1,
  timmy, ctx 8192 / parallel 1 / batch 4096, **no `--mlock`**) + the `embedder-qwen`
  Service (selector `app=embedder-qwen-rocm`).
- `viking/manifests/embedder-qwen-cuda-deployment.yaml` — now rollback (replicas 0,
  Service block removed).

## Pre-checks

```bash
kubectl get node timmy                                    # Ready
kubectl -n viking get deploy embedder-qwen-cuda           # current primary, 1/1
kubectl -n llama get pods -l app=ollama -o wide           # Ollama on timmy — note VRAM
# 9070 XT free VRAM (need ~5 GB for the embedder alongside Ollama):
kubectl -n llama exec deploy/ollama -c ollama -- rocm-smi --showmeminfo vram || true
```

Do a low-index-activity window if possible (brief embedding pause during cutover).
**Do not run this while the parallel `viking` cleanup session is active.**

## Cutover (the model PVC is RWO — it must hand off wemby → timmy)

```bash
# 1. Release the PVC + GPU on wemby (drains embedder off the flaky node).
kubectl -n viking scale deploy embedder-qwen-cuda --replicas=0
kubectl -n viking wait --for=delete pod -l app=embedder-qwen-cuda --timeout=120s
#    confirm the Longhorn volume detached from wemby before proceeding
kubectl -n longhorn-system get volumes.longhorn.io \
  -o custom-columns=NAME:.metadata.name,STATE:.status.state,NODE:.status.currentNodeID | grep -i qwen || true

# 2. Bring up ROCm on timmy + repoint the Service (one apply). Model is already
#    on the Longhorn PVC, so no 4.5 GB re-download; startupProbe should pass fast.
kubectl apply -f viking/manifests/embedder-qwen-rocm-deployment.yaml
kubectl -n viking rollout status deploy/embedder-qwen-rocm --timeout=600s

# 3. Persist the retired cuda state (replicas 0 + Service removed) so repo == live.
kubectl apply -f viking/manifests/embedder-qwen-cuda-deployment.yaml
```

## Verify

```bash
kubectl -n viking get pods -l app=embedder-qwen-rocm -o wide      # Running on timmy
kubectl -n viking get endpoints embedder-qwen                     # points at the rocm pod IP
kubectl -n viking exec deploy/embedder-qwen-rocm -c llamacpp -- \
  wget -qO- http://localhost:8000/health                          # {"status":"ok"}
# End-to-end: an OV search should return results (embedding path live again).
```

Optional: `benchmarks/embedding-retrieval/pooling_check.py` against the new
endpoint should return the card reference sims (~0.75 match / ~0.11 non-match) —
confirms `--pooling last` is honored on ROCm.

## Rollback (only if the 9070 XT contends with Ollama, or a ROCm regression — and wemby power is fixed)

```bash
kubectl -n viking scale deploy embedder-qwen-rocm --replicas=0
kubectl -n viking wait --for=delete pod -l app=embedder-qwen-rocm --timeout=120s
kubectl -n viking patch svc embedder-qwen \
  -p '{"spec":{"selector":{"app":"embedder-qwen-cuda"}}}'
kubectl -n viking scale deploy embedder-qwen-cuda --replicas=1
kubectl -n viking rollout status deploy/embedder-qwen-cuda --timeout=600s
```

## Post-cutover doc updates (do AFTER it's live)

- `CLAUDE.md` cluster-topology table + OpenViking summary: embedder wemby → timmy.
- `reference/service-routing.md` + `reference/llm-config.md`: node/backend for the embedder.
- Note in the TASK-1122 / IDEA-1042 compendium entries that the fragility of the
  1060 node (power, not GPU) forced the 9070-XT target ahead of schedule.
