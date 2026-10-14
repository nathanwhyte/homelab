# IMPR-1311: LiteLLM router in front of OpenViking's VLM

_OpenViking's VLM goes through a LiteLLM router: gemma4:vlm takes up to 4 calls in flight, Claude Haiku 5.5 takes the overflow under a $100 monthly cap, and everything else falls back to gemma. Grafana shows how much work Haiku picks up over time. Each cluster step below needs the user's go-ahead and applies one object at a time._

## Routing

| Order | Deployment | Gets a call when |
| --- | --- | --- |
| 1 | gemma4:vlm via chat-ollama, `max_parallel_requests: 4`, 90 s timeout | it has a free slot |
| 2 | Claude Haiku 5.5, native Messages API, thinking off, `temperature` dropped, 16,384 default `max_tokens` | gemma is full (router 429) or a gemma call ran past 90 s, and Anthropic spend this month is under $100 |
| 3 | gemma4:vlm, uncapped | Haiku is over budget, rate limited or down; Ollama queues the call |

Haiku's share of calls measures how often gemma was **full or stuck**, not how slow gemma was: a call gemma accepts stays on gemma.

One instance only: one replica, one worker, because the router counts gemma's cap per process. No Mac runs its own router; a client off the cluster would need a tailnet route to this Service, with the master key required.

Verified on pop with v1.103.1 and OpenViking's own request shapes (2026-10-14): overflow at the cap, forced and multi-turn tool calls on both models, thinking off and `temperature` dropped for Haiku, timeout failover to Haiku, budget exhaustion falling through to order 3 with no errors, and the Prometheus metrics. Not verifiable off-cluster: that Redis keeps the month's spend across a router restart (step 4).

## Deploy (each step its own go-ahead)

1. Secret, generated, never written to disk:

   ```bash
   kubectl create secret generic litellm-vlm -n viking \
     --from-literal=LITELLM_MASTER_KEY="sk-$(openssl rand -hex 24)" \
     --from-literal=REDIS_PASSWORD="$(openssl rand -hex 24)"
   ```

2. Redis: `kubectl diff` then apply `litellm-redis-pvc.yaml`, `litellm-redis-deployment.yaml`, `litellm-redis-service.yaml`, one at a time.
3. Router: the same for `litellm-vlm-configmap.yaml`, `litellm-vlm-deployment.yaml`, `litellm-vlm-service.yaml`, `litellm-vlm-servicemonitor.yaml`.
4. Smoke the router before OpenViking uses it (`kubectl -n viking port-forward svc/litellm-vlm 24000:4000`):
   - a plain and a forced-tool call with model `ov-vlm` and the master key;
   - `/metrics/` shows `litellm_provider_remaining_budget_metric{api_provider="anthropic"}`;
   - `kubectl -n viking rollout restart deploy/litellm-vlm`, then the remaining budget is unchanged (spend persisted in Redis).
5. OpenViking's Deployment (the `LITELLM_KEY` init env): `kubectl diff` then apply `openviking-deployment.yaml`. OpenViking restarts and stays on gemma.
6. Cut over: `viking/scripts/ov-vlm-swap.py restore` shows the `vlm` diff (to `ov-vlm` at `litellm-vlm`); `--apply` replaces the ConfigMap and restarts OpenViking. Check with other sessions touching OV first (IMPR-1303, TASK-1218).
7. Dashboard: apply `openviking-dashboard-configmap.yaml` (namespace `grafana`).

## Watch

- Grafana "OpenViking — Stack Health": VLM calls by route, Haiku share (1h), Haiku budget left, router failovers by reason (429 = gemma full, 408 = gemma past 90 s).
- OpenViking's own `openviking_vlm_calls_total{error_code}` now reports `model_name="ov-vlm"`; the per-model split is in LiteLLM's metrics.

## Rollback

`viking/scripts/ov-vlm-swap.py gemma --apply` writes the direct chat-ollama block and restarts OpenViking; the router can stay up idle. To retire it, delete the four `litellm-vlm` objects and the three `litellm-redis` objects.
