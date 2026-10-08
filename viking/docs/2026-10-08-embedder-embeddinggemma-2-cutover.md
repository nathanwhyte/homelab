# OpenViking embedder cutover: Qwen3-Embedding-4B → EmbeddingGemma 2 (2026-10-08)

Compendium: TASK-1218 (model decision and evidence), IMPR-1237 (the later move to Ollama). Decision by the user on 2026-10-07: adopt EmbeddingGemma 2 now on llama-server, with one re-index.

**Do not run this without the user's explicit go-ahead.** It changes the live embedder for prod and ov-test, and OpenViking recall stays degraded until the prod reindex finishes.

## What changes

| Object | Before | After |
| --- | --- | --- |
| Embedder Deployment | `embedder-qwen-cuda` at 1 | `embedder-eg2-cuda` at 1; `embedder-qwen-cuda` at 0 (rollback) |
| Embedder image | `llama.cpp:server-cuda` (floating tag; b11382 running) | `llama.cpp@sha256:fff6185e…` (b11459, pinned) |
| Model | Qwen3-Embedding-4B Q8_0, `--pooling last`, 2,560 dims | EmbeddingGemma 2 Q8_0 (`ggml-org/embeddinggemma-2-GGUF`, SHA-256 checked at start), `--pooling mean`, 768 dims |
| Service OV calls | `embedder-qwen:8080` | `embedder-eg2:8080` (`embedder-qwen` stays for rollback) |
| Prod collection | `context`, 2,560 dims | `context_eg2`, 768 dims (new; `context` kept untouched) |
| Test collection | `context_test`, 2,560 dims | `context_test_eg2`, 768 dims (new; `context_test` kept) |

Unchanged: `max_input_tokens` 6000, `max_concurrent` 1, `batch_size` 256, AGFS content, the VLM. No query/document prefixes (the `openai` provider cannot add them).

## Preconditions

1. No other session is using manu's GPU, and no IMPR-1237 window is open.
2. Record the starting state: `kubectl -n viking get deploy embedder-qwen-cuda openviking openviking-test -o wide`, the Qwen pod's `imageID`, and both live ConfigMaps (`kubectl -n viking get cm openviking-standalone-config openviking-test-config -o yaml > /tmp/ov-cm-before.yaml`).
3. Check the old collections exist and note their sizes, so the rollback can be verified: `ov ls viking://` and the vectordb collection stats if available.
4. Suspend `compendium/compendium-sync` for the window so it does not write into a half-built collection: `kubectl -n compendium patch cronjob compendium-sync -p '{"spec":{"suspend":true}}'`.

## Apply (one window)

The GTX 1080 holds one GPU pod, so prod and ov-test switch embedders together. Apply only the changed objects, never the whole directory (homelab `CLAUDE.md` § Ground rules), and read each `kubectl diff` first.

```bash
cd ~/code/homelab/main/viking/manifests
kubectl diff -f embedder-eg2-cuda-deployment.yaml -f embedder-eg2-service.yaml -f embedder-qwen-cuda-deployment.yaml -f openviking-standalone-configmap.yaml -f test/ov-test-configmap.yaml
# 1. free the GPU, then start the new embedder
kubectl apply -f embedder-qwen-cuda-deployment.yaml          # replicas 0
kubectl apply -f embedder-eg2-cuda-deployment.yaml -f embedder-eg2-service.yaml
kubectl -n viking rollout status deploy/embedder-eg2-cuda --timeout=600s
# 2. smoke: a real 768-dim vector through the Service
kubectl -n viking run eg2-smoke --rm -i --restart=Never --image=curlimages/curl:8.12.1 -- \
  curl -s http://embedder-eg2.viking.svc:8080/v1/embeddings -H 'Content-Type: application/json' \
  -d '{"model":"embeddinggemma-2","input":["smoke"]}'
# 3. ov-test first (canary)
kubectl apply -f test/ov-test-configmap.yaml
kubectl -n viking rollout restart deploy/openviking-test && kubectl -n viking rollout status deploy/openviking-test
```

## Reindex (vectors only)

OpenViking v0.4.20 exposes `POST /api/v1/content/reindex` (`openviking/server/routers/content.py`), role ROOT/ADMIN/USER, body `{"uri", "mode": "vectors_only", "wait", "recursive"}`. `vectors_only` re-embeds the stored L0/L1/L2 text from AGFS without regenerating summaries, for resources, memories, skills and the user/global namespaces (`openviking/service/reindex_executor.py`, `SUPPORTED_MODES_BY_TYPE`).

1. Enumerate the roots: `ov ls viking://` (resources, each `viking://user/<id>`, skills/agent namespaces).
2. On ov-test, reindex each root with `"wait": false`, then follow the returned task until it finishes (task tracker, `admin_reindex` task type). Verify the endpoint, the task polling route and the ROOT key handling on ov-test before prod.
3. Verify ov-test: `ov find` on a handful of known entries returns them; the collection's vector count is close to the old `context_test` count; no `dimension mismatch` in the logs.
4. Prod: `kubectl apply -f openviking-standalone-configmap.yaml`, restart `openviking`, then the same reindex calls against prod, resources first (largest recall value), then memories.
5. Resume `compendium-sync` and run `uv run python _scripts/compendium-sync.py reconcile` from the vault: `missing 0`, no `COVERAGE INCOMPLETE`.

Duration: Qwen 4B ran ~700 tok/s on this card; EmbeddingGemma 2 was 5–8× faster on the same llama.cpp build on pop. Measure ov-test's reindex time and extrapolate to prod before starting it.

## Verify

- `embedder-eg2-cuda` 1/1, `embedder-qwen-cuda` 0/0; `embedder-eg2` has one endpoint.
- Prod and ov-test `/health` and `/ready` succeed; `openviking-server doctor` passes.
- A recall smoke from a Claude Code session returns relevant memories.
- 24-hour watch: no embedding circuit-breaker openings, no 500 `too large to process` from the embedder (over-window inputs), recall latency under the 500 ms gate.

## Rollback

The old collections and the Qwen Deployment are untouched, so rollback re-points config, not data:

```bash
# 1. free the GPU
kubectl -n viking scale deploy/embedder-eg2-cuda --replicas=0
# 2. land a revert PR of this change, then apply the reverted objects from main
cd ~/code/homelab/main/viking/manifests
kubectl apply -f embedder-qwen-cuda-deployment.yaml -f openviking-standalone-configmap.yaml -f test/ov-test-configmap.yaml
kubectl -n viking rollout status deploy/embedder-qwen-cuda --timeout=600s
kubectl -n viking rollout restart deploy/openviking deploy/openviking-test
```

In an emergency, step 2 can apply the pre-cutover files from `git show <base>:viking/manifests/<file>` before the revert PR lands; record that drift on TASK-1218 and land the revert the same day.

Anything written to OpenViking during the window exists only in the `_eg2` collections; after a rollback, reindex those URIs into the old collection (`vectors_only`) or let `compendium-sync` re-push them.

## Follow-ups (not in this change)

- Recall score thresholds: the pilot plugin's `scoreThreshold` (0.35) and IMPR-1208's gate were calibrated on Qwen. EmbeddingGemma 2's scores sit in a higher, narrower band (no-answer queries 0.63–0.73 on the fixture), so recalibrate after the cutover.
- Delete the old 2,560-dim collections and the Qwen model cache only after the 24-hour watch passes.
- IMPR-1237 moves this embedder to Ollama once a release bundles llama.cpp b11454 or newer.
