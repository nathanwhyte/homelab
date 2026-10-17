# Embedder prompt shim: Qwen3-Embedding query instruction for OpenViking (2026-10-17)

Compendium: IMPR-1344 (the change and its done-when list), TASK-1218 (the measurement), IMPR-1208 (recall thresholds that move with it).

**Do not cut production over without the user's explicit go-ahead.** The cutover changes how every OpenViking query is embedded and moves the score band that recall thresholds are fitted on.

## What it does

Qwen3-Embedding is instruction-aware: queries should be embedded as `Instruct: <task>\nQuery: <text>`, documents as raw text. OpenViking's `openai` provider cannot prepend text, but with `query_param` and `document_param` set it sends an `input_type` field (`query` or `document`) on every embeddings request, which llama-server ignores.

`embedder-prompt-shim` (Deployment and Service in `viking`, script `viking/tools/embedder-prompt-shim.py`) sits between OpenViking and `embedder-qwen`:

- `input_type: query`: prepends `SHIM_QUERY_PREFIX` to each input.
- `input_type: document`: forwards the input unchanged (`SHIM_DOCUMENT_PREFIX` is empty).
- Missing or unknown `input_type` on `.../embeddings`: 400, never guessed. An OV pointed at the shim without the params fails loudly instead of embedding documents as queries.
- `input_type` is always removed before forwarding. Every other request passes through.
- `GET /healthz` answers locally, so an embedder outage does not also eject the shim from its Service.

Two replicas with preferred anti-affinity and `maxUnavailable: 0`; every OV embedding call passes through it.

Editing the script: change `viking/tools/embedder-prompt-shim.py`, run `viking/scripts/regen-prompt-shim-configmap.sh`, commit both, apply the ConfigMap, then bump `homelab.nathanwhyte.dev/config-rev` on the Deployment (the script is read at start). The `embedder-prompt-shim-tests` pre-commit hook fails when the two copies differ.

## Production cutover

1. Every item in § Production gate is checked, and the shim is running and ready: `kubectl -n viking get deploy embedder-prompt-shim` shows 2/2.
2. Take a vectors-included export of `viking://resources/compendium` **before** the change (the rollback pack) and record it as "params unset".
3. Record the IDEA-1100 gold-query baseline (unprefixed) the same day.
4. In `openviking-standalone-config` (`openviking-standalone-configmap.yaml`), `embedding.dense`:
   - `api_base`: `http://embedder-prompt-shim.viking.svc.cluster.local:8080/v1`
   - add `"query_param": "query"` and `"document_param": "document"`
5. `kubectl diff -f viking/manifests/openviking-standalone-configmap.yaml`, apply that one file, `kubectl -n viking rollout restart deploy/openviking`.
6. Check: shim logs show `query` and `document` counts and no `http_400`; a search returns results.
7. Take a vectors-included export right after; record it as "params set".

No reindex: document vectors through the shim are identical to direct-embedder vectors (see Validation).

## Rollback

Revert step 4 (point `api_base` back at `http://embedder-qwen.viking.svc.cluster.local:8080/v1` and remove both params), apply the ConfigMap, restart `deploy/openviking`. Stored vectors stay valid either way, since documents were embedded identically. The shim can then be scaled to 0.

## Vector backups (ovpack)

OpenViking treats `query_param` and `document_param` as part of the embedding identity. `_embedding_snapshot_compatible` (`storage/ovpack/vectors.py`, v0.4.20) compares `provider`, `model`, `input`, `query_param`, `document_param` and `dimensions` by exact equality (`api_base` is not compared). A pack exported with the params unset has neither key in its manifest, which compares equal only to a server with both unset.

| Pack exported with | Imported into a server with | `vector_mode=require` | `auto` |
| --- | --- | --- | --- |
| params unset (before cutover) | params unset | restores vectors | restores vectors |
| params unset | params set (after cutover) | **refused**: `embedding query_param mismatch` (tested) | re-embeds every record |
| params set (after cutover) | params unset (ov-test, rolled-back prod) | **refused** | re-embeds every record |
| params set | params set | restores vectors | restores vectors |

Restore procedure for a pack whose param setting differs from the server's: temporarily match the server's params to the pack (documents embed identically either way, so the stored vectors stay valid), import with `vector_mode=require`, then put the params back and restart. Queries embedded while the params are off are unprefixed, so do it in a quiet window.

On the S3 AGFS backend, `/pack/import` reports `Resource already exists` for any target, because listing a missing prefix returns an empty list instead of NotFound. Pass `on_conflict: overwrite` for a target known to be empty. The import writes one object at a time: the 2,583-entry compendium pack took about 60 minutes on ov-test, so call it with a long client timeout or poll the vector count.

## Validation on ov-test (2026-10-17)

All runs used ov-test (`openviking-test`) on a fresh collection `context_test_impr1344`, scored with TASK-1218's `score.py` (`/search/search`, no session, `limit 5`, URI-substring judging, ground truth `eval_groundtruth_2026-07-04.json`, sha256 `33a171c7…`) against `viking://resources/impr1344/compendium`.

| Check | Result |
| --- | --- |
| Shim passthrough, both pods | Document vectors through the shim vs direct `embedder-qwen`: cosine 1.000000 for 4 inputs up to ~4–5k tokens. Query vectors through the shim vs direct embedding of `prefix + query`: 1.000000; vs the raw query: 0.78–0.85 |
| Restore with params unset | Production's pre-change export (2,583 entries, 2,430 Qwen vectors) imported with `vector_mode=require`, `on_conflict=overwrite`: log `Restored vector snapshot`, about 60 min |
| Control (direct, no params) | top-1 13/34, top-5 21/34, weakest top-1 hit 0.6639, highest negative 0.4457, matching production's TASK-1218 numbers |
| Prompted (shim, params set) | top-1 **19/34**, top-5 **24/34**, weakest top-1 hit 0.5682, highest negative 0.3199, matching the TASK-1218 prototype |
| Old pack, params set | `require` import refused in 1.4 s: `INVALID_ARGUMENT`, `embedding query_param mismatch`. The check runs before any write, so nothing is created |
| New documents through the shim | Adding a resource (`wait: true`, 3 embeddings, plus its memory-linking commit) and committing a 4-message session (2 memories written) raised the shims' `document` counters by 13 and `query` by 2, with no `http_400` and no embedding errors in OV's log. OV v0.4.20's two `embeddings.create` calls both build `extra_body` through `_build_extra_body(is_query)`, so every request is marked once both params are set |
| Rollback (back to direct, no params) | top-1 13/34, top-5 21/34, identical scores to the control |

Median query latency on the fixture was 64 ms (control), 162 ms (prompted) and 102 ms (rollback). The embedder is shared with production, so these are noisy; the prompt adds about 20 tokens per query and one hop.

ov-test was restored to its pre-test ConfigMap (`context_test`, direct, no params). Left in place: collection `context_test_impr1344`, trees `viking://resources/impr1344/` and `viking://resources/impr1344probe/`, and the probe session's two memories under `viking://user/noot` on ov-test.

## Production gate

Before step 4 of the cutover, all of these hold:

- [x] Passthrough cosine 1.0 (above)
- [x] Fixture top-1 ≥ 19/34 and top-5 ≥ 24/34 on ov-test (above); re-score through production after the cutover
- [x] New-document indexing marked and accepted through the shim (above)
- [x] Rollback tested on ov-test (above)
- [ ] IDEA-1100 gold-query baseline (unprefixed) recorded the same day
- [ ] IMPR-1208 recall thresholds re-fitted on the prompted band (weakest hit 0.664 → 0.568, highest negative 0.446 → 0.320)
