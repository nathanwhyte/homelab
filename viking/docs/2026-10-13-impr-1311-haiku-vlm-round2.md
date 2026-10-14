# IMPR-1311 round 2: one busy hour of OpenViking's VLM on Claude Haiku 5.5

_Point prod OpenViking's VLM at Claude Haiku 5.5 (the Max plan's monthly API credit) for one busy-hour window, measure it, and restore gemma4:vlm. Round 3, a 20-way `add-resource` burst, runs inside the same window. Every cluster step needs the user's go-ahead and applies one object at a time._

## What makes Haiku work through OpenViking 0.4.20

Probed offline from pop on 2026-10-13 by driving v0.4.20's own `OpenAIVLM` at Anthropic's OpenAI-compatible endpoint:

| Need | How | Verified |
| --- | --- | --- |
| Endpoint and auth | `provider: openai`, `api_base: https://api.anthropic.com/v1/`, Console key as `api_key` | Plain, tool-forced and multi-turn tool-result calls all return |
| Workspace | The key is not workspace-scoped; `extra_headers: {anthropic-workspace-id: …}` | Without it the API answers 400 |
| No `temperature` | `ov_claude_params_patch.py` drops it for `claude-*` models | Haiku 5.5 answers 400 "`temperature` is deprecated for this model"; `VLMConfig` rejects `temperature: null` |
| Thinking off | `extra_request_body: {thinking: {type: disabled}}` | One WM rewrite: 3,537 output tokens disabled against 5,850 default |
| Output cap | `max_tokens: 16384` (also caps memory extraction, default 32768) | With thinking on at 8192, 3 of 12 WM rewrites were cut off |

`ov_wm_update_patch.py` stays on: the WM creation path is the one proven 12/12 on Haiku.

## Before the window (each step its own go-ahead)

1. Check with the TASK-1218 session that its embedder probe is not running or about to run; an embedder outage skews every number below.
2. Secret, from the key file without writing the value anywhere:

   ```bash
   kubectl create secret generic anthropic-vlm-key -n viking \
     --from-file=API_KEY=$HOME/code/keys/claude-console-api-key-openviking-vlm.txt
   ```

3. Patch ConfigMap: `kubectl diff -f viking/manifests/openviking-nav-patch-configmap.yaml`, then apply that file (one object).
4. Deployment (the new `items` entry and the `ANTHROPIC_KEY` init env): `kubectl diff -f viking/manifests/openviking-deployment.yaml`, then apply it (one object; the rollout restarts the pod). Still on gemma, so the patch is inert; confirm `ov-claude-params-patch: applied` in the pod log.

## The window

1. Record the baseline for the previous hour (queries below).
2. `viking/scripts/ov-vlm-swap.py haiku` prints the diff (the `vlm` block only); `--apply` applies the one ConfigMap and restarts OpenViking.
3. Confirm the first calls succeed: no `400`/`401` in the pod log, `openviking_model_calls_total{model_type="vlm"}` rising.
4. Round 3, about 15 minutes in: 20 concurrent `ov add-resource` calls of small files under a scratch URI, then remove them.
5. After an hour, `viking/scripts/ov-vlm-swap.py restore --apply` writes back the repo's `vlm` block (gemma4:vlm) and restarts.

The `max_concurrent` rise (2 to 4) widens only the Semantic DAG on v0.4.20; it is in the swap so the window measures the end state.

## Measure

| Signal | Query or source |
| --- | --- |
| VLM calls and tokens | `increase(openviking_model_calls_total{model_type="vlm"}[1h])`, `increase(openviking_model_tokens_total{model_type="vlm"}[1h])` by `token_type` |
| Semantic queue | `openviking_queue_pending` for the Semantic queue, against the baseline hour |
| Errors | Pod log: `429`, `400`, `WM creation failed`, `falling back`; Loki `{namespace="viking", container="openviking"}` |
| Spend | Claude Console usage for the window (round 1 tallied within a cent of it) |

Results go in compendium IMPR-1311 § Round 2.

## Rollback

`ov-vlm-swap.py restore --apply` at any point. If the patch misbehaves, `OV_CLAUDE_PARAMS_PATCH=0` on the Deployment disables it alone; it is inert on gemma either way.
