# ollama-pop

pop's native MLX ollama, linked into the cluster as an in-cluster hostname
for Hermes (and any other consumer that needs a 35B-class reasoning tier
without paying for `glm-5.1:cloud`).

Source: FEAT-1021 — promoted from IDEA-1051.

## Active transport — direct DNS via coredns-custom

| Property         | Value                                                                                                     |
| ---------------- | --------------------------------------------------------------------------------------------------------- |
| Cluster hostname | `ollama-pop.homelab.local:11434`                                                                          |
| Resolves to      | `192.168.1.6` (pop LAN, router-reserved)                                                                  |
| DNS mechanism    | `coredns-custom` ConfigMap, A record + 60s cache + 15s reload                                             |
| Manifest         | `k8s/coredns-custom-configmap.yaml`                                                                       |
| Pop-side         | `mac/launchd/com.user.ollama-serve.plist` (LaunchAgent binds `0.0.0.0:11434`, exports `OLLAMA_ORIGINS=*`) |
| Firewall         | `mac/docs/ollama-pop-fw.md` (LAN + Tailscale CGNAT only)                                                  |

This is the **lean primary** per IDEA-1051: one ConfigMap key, no
kube-proxy, no Endpoints subset to go stale when pop sleeps.

## Why DNS, not a Service+Endpoints

A selector-less Service + manual Endpoints object would also work and is
the conventional pattern (it matches `ollama.llama.svc:11434`'s shape).
For a few consumers, all hitting one model, the coredns-custom path is
simpler: nothing to keep in sync, the ConfigMap lives next to the
Corefile that imports it, and a sleeping pop simply means NXDOMAIN or a
fast health-probe miss (no leftover Service backends pointing at a
stale IP).

## Documented fallback — selector-less Service + Endpoints

If we ever need a Service abstraction (for Traefik Ingress, for example,
or for a kube-proxy-managed round-robin across multiple pop boxes), the
fallback is a single pair of objects:

```yaml
# DO NOT apply this — kept here for reference only.
apiVersion: v1
kind: Service
metadata:
  name: ollama-pop
  namespace: llama
spec:
  ports:
    - name: http
      port: 11434
      targetPort: 11434
---
apiVersion: v1
kind: Endpoints
metadata:
  name: ollama-pop
  namespace: llama
subsets:
  - addresses:
      - ip: 192.168.1.6 # pop LAN, primary
    ports:
      - port: 11434
```

The Endpoints object **does not** auto-update if pop sleeps or moves
subnets — a stale Endpoints subset will blackhole traffic instead of
returning a clean DNS NXDOMAIN. The coredns-custom path sidesteps that
failure mode.

## Deferred — chat-ollama-pop proxy

Hermes' chat-ollama proxy (`llama/chat-ollama-proxy.yaml`) injects
`reasoning_effort: "none"` so local models don't burn output budget on
thinking tokens. We currently route Hermes' pop tier directly at
`ollama-pop.homelab.local:11434` (no proxy), which means the pop model
**will** emit thinking tokens until either:

1. `qwen3.6:35b-mlx` itself respects a `reasoning_effort: none` field
   (it doesn't today), or
2. We stand up a `chat-ollama-pop` instance pointed at the pop endpoint.

That second item is the **deferred** follow-up. If/when we land it, the
manifest goes in `llama/chat-ollama-pop-*.yaml` next to
`chat-ollama-proxy.yaml`, and the Hermes `fallback_providers` entry's
`base_url` flips from the raw endpoint to
`http://chat-ollama-pop.llama.svc:11434`. Tracked under FEAT-1021
followups, not committed as a manifest yet.

## Endpoints (cheat sheet)

| Consumer                            | URL                                                                                                           |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| Cluster pod (any ns)                | `http://ollama-pop.homelab.local:11434`                                                                       |
| MacBook (LAN)                       | `http://192.168.1.6:11434`                                                                                    |
| MacBook (off-LAN, Tailscale)        | `http://100.113.28.62:11434`                                                                                  |
| MacBook (localhost, post-FEAT-1021) | `http://0.0.0.0:11434` (the LaunchAgent binds all interfaces; the prior `127.0.0.1`-only listener is removed) |

Public Cloudflare tunnel: **deliberately not** exposed. pop ollama has
no auth.

## Verification

```sh
# Cluster side
kubectl --context=homelab -n viking exec deploy/openviking -- \
  curl -s http://ollama-pop.homelab.local:11434/api/tags | jq .models[].name
# expect: "qwen3.6:35b-mlx"

# Mac side
lsof -nP -iTCP:11434 -sTCP:LISTEN
# expect: a single process on *:11434 (or 0.0.0.0:11434)
```
