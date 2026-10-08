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
2. Record the starting state: `kubectl -n viking get deploy embedder-qwen-cuda openviking openviking-test -o wide`, the Qwen pod's `imageID` (it must match the pinned rollback digest `sha256:ef08b5a9…`), and both live ConfigMaps (`kubectl -n viking get cm openviking-standalone-config openviking-test-config -o yaml > /tmp/ov-cm-before.yaml`).
3. Check the old collections exist and note their sizes, so the rollback can be verified: `ov ls viking://` and the vectordb collection stats if available.
4. Suspend `compendium/compendium-sync` for the window so it does not write into a half-built collection: `kubectl -n compendium patch cronjob compendium-sync -p '{"spec":{"suspend":true}}'`.
5. **Reindex inventory gate: complete it before stopping Qwen.** `ov ls viking://` as `noot` lists only `viking://user/noot`, and reading `noot-codex` or `noot-pilot` returns 403 (Codex review, 2026-10-08). So a generic root walk silently misses other users' memories. Write down, per instance:
   - every account and user that owns memories (at least `noot`, `noot-codex`, `noot-pilot`, plus any peers under each user), and the identity each reindex call must carry (the trusted-mode user and role headers, with the root key);
   - the roots per user (`viking://user/<id>`, which traverses its peer memory roots), `viking://resources`, and each skills/agent root by explicit URI (`viking://agent/skills` and any other in use). The deployed executor rejects bare `viking://agent` (`reindex_executor.py:277`), and its global traversal skips shared agent namespaces;
   - the old collection's per-root vector counts, for the coverage check after the reindex.

   Prove each (root, identity) pair is accepted on ov-test without writing, with `{"uri": "<root>", "mode": "prune_orphans", "dry_run": true}`. Then confirm task polling at `GET /api/v1/tasks/{task_id}`. Do not start the window until every pair has passed.

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
# 3. both OV configs at once: once embedder-qwen-cuda is at 0, an OV still on the
#    old config has no embedder at all (errors, not just thin results)
kubectl apply -f test/ov-test-configmap.yaml -f openviking-standalone-configmap.yaml
kubectl -n viking rollout restart deploy/openviking-test deploy/openviking
kubectl -n viking rollout status deploy/openviking-test && kubectl -n viking rollout status deploy/openviking
```

## Window impact

manu has one GPU, so the Qwen embedder stops the moment EmbeddingGemma 2 starts, and prod and ov-test switch together. ov-test is a canary for the **reindex procedure**, not for running the new model while prod stays on the old one.

| Phase | Prod recall | Prod writes |
| --- | --- | --- |
| Apply step 1 until step 3 finishes (a few minutes) | Fails open: no embedder behind the old config | Embedding queue errors; retried by OV |
| After step 3, until the prod reindex finishes | Thin: the new collection holds only what has been reindexed or written since | Land in `context_eg2` |
| After the reindex | Normal | Normal |

**Size the window before starting:** count the old collection's vectors (vectordb collection stats, or `ov ls -r -a` over each root) and time ov-test's full reindex. Qwen 4B ran ~700 tok/s on this card and EmbeddingGemma 2 was 5–8× faster on the same llama.cpp build on pop, so prod's reindex should take a fraction of the June Qwen re-embed. Use the measured ov-test rate, scaled by the vector count ratio, as the estimate, and pick a time when thin recall is acceptable.

## Reindex (vectors only)

OpenViking v0.4.20 exposes `POST /api/v1/content/reindex` (`openviking/server/routers/content.py`), role ROOT/ADMIN/USER, body `{"uri", "mode": "vectors_only", "wait", "recursive"}`. `vectors_only` re-embeds the stored L0/L1/L2 text from AGFS without regenerating summaries, for resources, memories, skills and the user/global namespaces (`openviking/service/reindex_executor.py`, `SUPPORTED_MODES_BY_TYPE`).

1. Use the inventory from Preconditions step 5. Work through every (root, identity) pair from that list. Do not walk `ov ls viking://` instead, because it shows only what the calling user can see.
2. On ov-test, reindex each pair with `"wait": false` and poll `GET /api/v1/tasks/{task_id}` until it finishes.
3. **Accept a root only if** its result reports `failed_records` 0 and `unsupported_records` 0, or every one of them is explained. The executor returns `status: completed` even when some embeddings failed (`reindex_executor.py` accumulates them, around line 411), so "completed" alone proves nothing. Also require all of these:
   - the embedding queue has drained;
   - no warnings in the OV log for that task;
   - no `dimension mismatch`;
   - per root and level, the new collection's vector count matches the old collection's.

   Then run `ov find` on a handful of known entries from each user and from resources.
4. Prod (config already applied in step 3): the same pairs and the same acceptance, memories first (recall hooks query them), then resources and skills. Prod runs patched reindex code for event memories (`openviking-nav-patch-configmap.yaml`, the `apply_reindex` hook from BUG-1179/IMPR-1200). Check it is loaded before relying on `vectors_only` for `events`.
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
# 2. land a rollback PR (a forward change, not a git revert, which would also drop
#    the Qwen digest pin): embedder-eg2-cuda replicas 0, embedder-qwen-cuda replicas 1
#    on its pinned digest, both OV configs back to qwen3-embedding-4b /
#    embedder-qwen / 2560 / context and context_test. Then apply from main:
cd ~/code/homelab/main/viking/manifests
kubectl apply -f embedder-qwen-cuda-deployment.yaml -f openviking-standalone-configmap.yaml -f test/ov-test-configmap.yaml
kubectl -n viking rollout status deploy/embedder-qwen-cuda --timeout=600s
kubectl -n viking rollout restart deploy/openviking deploy/openviking-test
```

In an emergency, step 2 can apply hand-edited copies of those three files before the rollback PR lands. Never apply pre-cutover files from `git show <base>`: that Qwen manifest has the floating tag. Record that drift on TASK-1218 and land the rollback PR the same day.

The pinned Qwen digest (`sha256:ef08b5a9…`, b11382) means a rollback serves the same engine its stored vectors came from.

**The old collections are only clean if nothing changed while EmbeddingGemma 2 was live.** AGFS is shared, but only the active collection receives mutations. So any addition, update, deletion or move during that time leaves the old 2,560-dim collection out of date:

- a new or changed URI has no current vector there;
- a deleted or moved memory keeps its stale vector there.

`vectors_only` upserts surviving content but never removes stale records, and `compendium-sync` cannot repair session-memory changes. After a rollback that follows any writes, do all of the following before calling the rollback complete:

1. Rebuild the old collection with Qwen: run the same inventory pairs with `vectors_only`.
2. Run `prune_orphans` with `dry_run: true` per root, review the candidate list, then run it for real.
3. Run `compendium-sync reconcile` and confirm it reports `missing 0`.

## Follow-ups (not in this change)

- Recall score thresholds: the pilot plugin's `scoreThreshold` (0.35) and IMPR-1208's gate were calibrated on Qwen. EmbeddingGemma 2's scores sit in a higher, narrower band (no-answer queries 0.63–0.73 on the fixture), so recalibrate after the cutover.
- Delete the old 2,560-dim collections and the Qwen model cache only after the 24-hour watch passes.
- IMPR-1237 moves this embedder to Ollama once a release bundles llama.cpp b11454 or newer.
- dotfiles `claude/ov-pilot/pending-canary.yaml` (dormant) still expects Qwen. Update it before it is reused.
- Unverified until the window: EmbeddingGemma 2's startup on the GTX 1080, its peak memory, how it handles long inputs, and the end-to-end reindex.
