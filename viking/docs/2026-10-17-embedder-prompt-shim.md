# Embedder prompt shim: Qwen3-Embedding query instruction for OpenViking (2026-10-17)

Compendium: IMPR-1344 (the change and its done-when list), TASK-1218 (the measurement), IMPR-1208 (recall thresholds that move with it).

**Do not cut production over without the user's explicit go-ahead.** The cutover changes how every OpenViking query is embedded and moves the score band that recall thresholds are fitted on.

## What it does

Qwen3-Embedding is instruction-aware: queries should be embedded as `Instruct: <task>\nQuery: <text>`, documents as raw text. OpenViking's `openai` provider cannot prepend text, but with `query_param` and `document_param` set it sends an `input_type` field (`query` or `document`) on every embeddings request, which llama-server ignores.

`embedder-prompt-shim` (Deployment and Service in `viking`, script `viking/tools/embedder-prompt-shim.py`) sits between OpenViking and `embedder-qwen`:

- `input_type: query`: prepends `SHIM_QUERY_PREFIX` to each input.
- `input_type: document`: forwards the input unchanged (`SHIM_DOCUMENT_PREFIX` is empty).
- Missing or unknown `input_type` on a POST to `.../embedding` or `.../embeddings` (llama-server serves `/embedding`, `/embeddings` and `/v1/embeddings`): 400, never guessed. An OV pointed at the shim without the params fails loudly instead of embedding documents as queries.
- A percent-encoded request path is a 400 on any method. llama-server decodes `%XX` and `%uXXXX` before routing, and OV never encodes its paths, so refusing them keeps the shim's view of the route equal to the embedder's.
- `input_type` is always removed before forwarding. Every other request passes through: GET, HEAD (with the upstream's `Content-Length` and no body), OPTIONS, PUT, PATCH, DELETE, and POST elsewhere.
- `GET` and `HEAD /healthz` answer locally, so an embedder outage does not also eject the shim from its Service.

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
6. In the same window, ship the recall floor re-fitted on the prompted band (dotfiles `OPENVIKING_SCORE_THRESHOLD`, IMPR-1208). The prompt lowers every memory score by about 0.12, so the old floor cuts most real hits (see the 2026-10-18 attempt below).
7. Check: shim logs show `query` and `document` counts and no `http_400`; a search returns results; the gold-query recall run at the new floor matches the baseline.
8. Take a vectors-included export right after; record it as "params set".

No reindex: document vectors through the shim are identical to direct-embedder vectors (see Validation).

## Rollback

Revert step 4 (point `api_base` back at `http://embedder-qwen.viking.svc.cluster.local:8080/v1` and remove both params), apply the ConfigMap, restart `deploy/openviking`, and revert step 6's floor. Stored vectors stay valid either way, since documents were embedded identically. The shim can then be scaled to 0.

## Vector backups (ovpack)

OpenViking treats `query_param` and `document_param` as part of the embedding identity. `_embedding_snapshot_compatible` (`storage/ovpack/vectors.py`, v0.4.20) compares `provider`, `model`, `input`, `query_param`, `document_param` and `dimensions` by exact equality (`api_base` is not compared). A pack exported with the params unset has neither key in its manifest, which compares equal only to a server with both unset.

| Pack exported with | Imported into a server with | `vector_mode=require` | `auto` |
| --- | --- | --- | --- |
| params unset (before cutover) | params unset | restores vectors | restores vectors |
| params unset | params set (after cutover) | **refused**: `embedding query_param mismatch` (tested) | re-embeds every record |
| params set (after cutover) | params unset (ov-test, rolled-back prod) | **refused** | re-embeds every record |
| params set | params set | restores vectors | restores vectors |

Restore procedure for a pack whose param setting differs from the server's: temporarily switch the server's whole `embedding.dense` route to the pack's setting, import, then switch it back. Documents embed identically either way, so the stored vectors stay valid. `api_base` and the two params always change together, because the shim answers 400 to any embedding request without `input_type`:

1. **Params-unset pack into a params-set server** (any pack taken before 2026-10-19 04:28 UTC into production now): apply the Rollback configuration (`api_base` to `embedder-qwen`, both params removed) and restart `deploy/openviking`. Import with `vector_mode=require`. Then reapply cutover step 4 and restart again.
2. **Params-set pack into a params-unset server** (ov-test, or a rolled-back production): apply cutover step 4 (`api_base` to the shim, both params added) and restart. Import with `vector_mode=require`. Then revert and restart again.

While the route is switched, queries embed in the pack's form, and recall floors are fitted for the other one. Do it in a quiet window.

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

## Production cutover attempt (2026-10-18): rolled back

Switched at 04:01:57 UTC with the user's go-ahead (steps 1–5; the floor was not re-fitted), rolled back at 17:11 UTC (rollout done 17:17). Evidence: `~/code/archive/ov-pilot-20260921/06-trial/impr-1344-recall/` (`pre-switch-*`, `post-switch-*`, `post-rollback-*` recall runs, the "params unset" pack under `packs/`, `score-prod-postswitch.json`).

| Measure | Before (direct) | Switched (shim) | After rollback |
| --- | --- | --- | --- |
| Resource fixture top-1 / top-5 | 13/34, 21/34 | **20/34, 25/34** | — |
| Memory recall at the production floor 0.60: answerable gold queries kept | 4/7 | **2/6** | 4/7 |
| Mean memories kept per gold query / noise per off-topic prompt | 4.1 / 0.1 | 0.4 / 0.0 | 4.1 / 0.1 |

Memory recall (dotfiles `recall-gate-calibrate.py`, the plugin's own request with no floor, 40 gold queries, 8 off-topic prompts) regressed for two reasons:

- **Uniform score drop.** All 230 memory/query pairs returned in both runs scored lower with the prompt: mean −0.122, range 0 to −0.235, relevant and noise alike. The 0.60 floor (dotfiles `claude/settings.json`, IMPR-1250) was fitted on the unprefixed band, so it acted like a floor near 0.72.
- **No ranking gain on memories.** Relevant memories moved up for three gold queries and down for three; answerable queries went from 7 to 6. The resource fixture's gain did not carry over.

A floor moved with the band recovers most of it (switched run, `floor-sweep.py`):

| Floor | Answerable kept | Mean noise | Off-topic prompts with ≤ 2 noise |
| --- | --- | --- | --- |
| Unprefixed, 0.60 (production) | 4/7 | 0.1 | 8/8 |
| Unprefixed, 0.55 | 6/7 | 1.0 | 7/8 |
| Prompted, 0.40 | 6/6 | 1.9 | 6/8 |
| Prompted, 0.42 | 4/6 | 0.0 | 8/8 |
| Prompted, 0.44–0.48 | 3/6 | 0.0 | 8/8 |

The set is too small to pick between these: only 7 of the 40 gold queries have an answer stored in OpenViking. Nothing stored changed during the window, so the rollback needed no cleanup; recall after it is identical to before.

## Production gate

Before step 4 of the cutover, all of these hold:

- [x] Passthrough cosine 1.0 (above)
- [x] Fixture top-1 ≥ 19/34 and top-5 ≥ 24/34 on ov-test (above); production reached 20/34 and 25/34 on 2026-10-18
- [x] New-document indexing marked and accepted through the shim (above)
- [x] Rollback tested on ov-test (above) and used on production (2026-10-18)
- [x] IDEA-1100 gold-query baseline (unprefixed) recorded the same day: round 2's unprefixed arm (2026-10-18, below) includes the 40 C1 gold queries on the live tree
- [x] IMPR-1208 recall floor re-fitted on the prompted band, on a labelled recall set with tens of answerable queries (2026-10-18, below); ships in the same window (step 6)
- [x] Memory recall at the new floor shows no regression against the unprefixed baseline (offline replica, below); re-check with the live calibrator after the cutover

## Floor re-fit (2026-10-18, offline)

Measured without touching the server: a replica of OV v0.4.20's context search (plain cosine, live recall's scopes and quotas) against production's `ov-vectordb`, with query vectors from the shim (`input_type: document` for the unprefixed arm, `query` for the prompted arm). It matched production's `/search/search` exactly on 25 queries. The labelled set has 56 prompts for memories sampled from the live tree, 30 off-topic prompts, and 120 real prompts from the recall hook log.

| Arm, floor | Targets recalled (of 56) | Noise per off-topic prompt | Entries per real prompt |
| --- | --- | --- | --- |
| Unprefixed, 0.60 (production) | 40 | 0.17 | 3.44 |
| Prompted, 0.48 | 45 | 0.17 | 4.04 |
| Prompted, 0.50 | 44 | 0.13 | 3.58 |

Candidate for step 6: `OPENVIKING_SCORE_THRESHOLD=0.50`. Other fixed floors read the same band: the MCP `viking_search` default (0.50, keeps every prompted resource-fixture hit, cuts the lower memory hits) and Codex's `OPENVIKING_SCORE_THRESHOLD` (0.55). Evidence and method: `~/code/archive/ov-pilot-20260921/06-trial/impr-1344-recall/round2-20261018/README.md`; analysis in IMPR-1208 § Round 2.

## Production cutover (2026-10-19 UTC): live

With the user's go-ahead, all eight steps ran in one window:

| Step | Result |
| --- | --- |
| 2. Rollback pack, params unset | `packs/compendium-prod-pre-switch2-params-unset-20261019T0424Z.ovpack` (28.2 MB) |
| 3. Unprefixed baseline | Round 2's unprefixed arm, 2026-10-18 (above) |
| 4–5. ConfigMap applied, OV restarted | 04:28:47 UTC; rollout done 04:29:12 |
| 6. Floors | dotfiles#466 merged 04:30:55: Claude Code recall 0.60 → 0.50, `viking_search` default 0.50 → 0.45, Codex 0.55 → 0.45 (pop's `~/.config/openviking/codex.env` set by hand) |
| 7. Check | Production's `/search/search` matches the replica's prompted arm on a fresh 25-query sample: same entry set 25/25, max score difference 0.000000 (the unprefixed arm now differs on 25/25). Round 2's prompted 0.50 row therefore describes live recall. No OV embedding errors after the restart |
| 8. Pack, params set | `packs/compendium-prod-post-switch2-params-set-20261019T0432Z.ovpack` (28.2 MB) |

Evidence: `~/code/archive/ov-pilot-20260921/06-trial/impr-1344-recall/post-cutover2-20261019T0429Z/`. Workbook's `codex.env` floor still needs the same change.
