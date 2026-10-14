# IMPR-1311: LiteLLM router in front of OpenViking's VLM

_OpenViking's VLM goes through a LiteLLM router: gemma4:vlm takes up to 4 calls in flight, Claude Haiku 5.5 takes the overflow until about $90 of a hard $100 monthly cap, and everything else falls back to gemma. Grafana shows how much work Haiku picks up over time. Each cluster step below needs the user's go-ahead and applies one object at a time._

## Routing

| Order | Deployment | Gets a call when |
| --- | --- | --- |
| 1 | gemma4:vlm via chat-ollama, `max_parallel_requests: 4`, 90 s timeout | it has a free slot |
| 2 | Claude Haiku 5.5, native Messages API, thinking off, `temperature` dropped, 16,384 default `max_tokens`, 120 s timeout | gemma is full (router 429) or a gemma call ran past 90 s, and the router's Anthropic tally this month is under $90 |
| 3 | gemma4:vlm, uncapped, 900 s timeout | Haiku is over budget, rate limited, at the Console limit, or down; Ollama queues the call |

Haiku's share of calls measures how often gemma was **full or stuck**, not how slow gemma was: a call gemma accepts stays on gemma.

Timeouts are per attempt and bound the wait for a response, not wall-clock time: LiteLLM's transport sets connect and read timers with no total deadline (Codex round 2), and OpenViking's own 1,200 s `vlm.timeout` works the same way through the OpenAI SDK. These calls are non-streaming, so a model's whole reply arrives after generation and the read timer covers the generation; the three attempts' 90 + 120 + 900 = 1,110 s keeps a normal worst-case chain inside OpenViking's 1,200 s. A server that trickled a body slowly could exceed both bounds; neither side guards against that.

One instance only: one replica, one worker, because the router counts gemma's cap per process. No Mac runs its own router; a client off the cluster would need a tailnet route to this Service, with the master key required.

## Budget: soft threshold, hard cap

- **Hard cap, $100: the Claude Console workspace spend limit.** Anthropic enforces it and fails closed: past it Haiku calls error and the router moves them to order 3.
- **Routing threshold, $90: LiteLLM's `provider_budget_config`.** It keeps routing off Haiku before the hard cap is reached. It is not exact: LiteLLM 1.103.1 records a call's cost after it finishes and writes it to Redis asynchronously, so concurrent overflow can overshoot it, and spend recorded while Redis is down can be lost (Codex review on homelab#225). The $10 gap and the Console limit cover both.
- Spend is tallied in `litellm-redis` (append-only file on a PVC), so a router restart keeps the month's total.

## Deploy (each step its own go-ahead)

0. Console (user): set the OpenViking workspace's monthly spend limit to $100 in the Claude Console.
1. Secret, generated, never written to disk:

   ```bash
   kubectl create secret generic litellm-vlm -n viking \
     --from-literal=LITELLM_MASTER_KEY="sk-$(openssl rand -hex 24)" \
     --from-literal=REDIS_PASSWORD="$(openssl rand -hex 24)"
   ```

2. Redis: `kubectl diff` then apply `litellm-redis-pvc.yaml`, `litellm-redis-deployment.yaml`, `litellm-redis-service.yaml`, one at a time.
3. Router: the same for `litellm-vlm-configmap.yaml`, `litellm-vlm-deployment.yaml`, `litellm-vlm-service.yaml`, `litellm-vlm-servicemonitor.yaml`.
4. Smoke the router before OpenViking uses it (`kubectl -n viking port-forward svc/litellm-vlm 24000:4000`, model `ov-vlm`, the master key):
   - 8 concurrent short calls, so at least 4 overflow to Haiku: `litellm_deployment_success_responses_total` shows both deployments and `litellm_spend_metric_total{api_provider="anthropic"}` is above 0. Also one forced-tool call.
   - The remaining-budget gauge updates only when the router evaluates a request, so refresh it before each reading: wait a minute for the Redis write, send one single call (gemma serves it), then record `litellm_provider_remaining_budget_metric{api_provider="anthropic"}` (below $90). `kubectl -n viking rollout restart deploy/litellm-vlm`, send one single call again, and check the value matches. If it reads $90 again, spend is not persisting: stop.
   - Redis outage: `kubectl -n viking scale deploy/litellm-redis --replicas=0`, send another overflow batch (calls still succeed), then scale back to 1. Metrics alone can't show the router reconnected: LiteLLM tallies spend in memory and drops failed Redis writes silently (Codex round 3). So prove persistence again: send a fresh 8-call overflow batch (new Haiku spend), wait a minute, refresh and record the gauge, restart the router, refresh it again, and check it matches the pre-restart reading, which must be lower than the reading before the outage. Spend from the outage window may be missing from the tally; the Console limit covers it.
5. OpenViking's Deployment (the `LITELLM_KEY` init env): `kubectl diff` then apply `openviking-deployment.yaml`. OpenViking restarts and stays on gemma.
6. Cut over: `viking/scripts/ov-vlm-swap.py restore` shows the `vlm` diff (to `ov-vlm` at `litellm-vlm`); `--apply` replaces the ConfigMap and restarts OpenViking. Check with other sessions touching OV first (IMPR-1303, TASK-1218).
7. Dashboard: apply `openviking-dashboard-configmap.yaml` (namespace `grafana`).
8. Alerts: apply `litellm-vlm-alerts.yaml`.

Deployed 2026-10-14; OpenViking cut over at 13:36:41 CDT. Step 4's Redis outage gate failed as written, for a known reason (next section), and the user chose to proceed with the alert in step 8.

## Redis restarts: restart the router

LiteLLM 1.103.1's Redis client does not reconnect after a Redis restart. The router keeps serving, but Haiku spend goes untallied, both in memory and in Redis, until `litellm-vlm` itself restarts. Measured 2026-10-14: four Haiku calls after a Redis outage left the gauge unchanged across a router restart; after a clean router start, the same batch landed in Redis (`provider_spend:anthropic:1mo`).

So after any `litellm-redis` restart (node drain, eviction, a manifest apply, or an in-place container restart from OOM or liveness), run `kubectl -n viking rollout restart deploy/litellm-vlm`. `LiteLLMRedisRestartedAfterRouter` fires when the Redis container started more recently than the router container (`kube_pod_container_state_started`), which covers in-place restarts too.

The router's budget window is one month from when its tracking started (`time_period: 1mo` advances to the same date next month), not the calendar month the Console limit uses. The leak is bounded by Haiku's overflow rate, and the Console limit stays the hard cap.

## Watch

- Grafana "OpenViking — Stack Health": VLM calls by route, Haiku share (1h), Haiku budget left (against the $90 threshold), router failovers by reason (429 = gemma full, 408 = gemma past 90 s).
- OpenViking's own `openviking_vlm_calls_total{error_code}` now reports `model_name="ov-vlm"`; the per-model split is in LiteLLM's metrics.
- Claude Console usage for the workspace, the authoritative spend.

## Rollback

`viking/scripts/ov-vlm-swap.py gemma --apply` writes the direct chat-ollama block and restarts OpenViking; the router can stay up idle. To retire it, delete the four `litellm-vlm` objects and the three `litellm-redis` objects.
