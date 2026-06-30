# pop ollama — macOS firewall (socketfilterfw) — FEAT-1021

Cluster pods reach pop's ollama as `http://ollama-pop.homelab.local:11434`
via the `coredns-custom` ConfigMap (resolves to `192.168.1.6`). The path
that gets the packet to pop is **LAN or Tailscale only** — pop ollama is
deliberately not exposed via the public Cloudflare tunnel. Scope the
macOS firewall so the bind on `0.0.0.0:11434` (set by
`com.user.ollama-serve` LaunchAgent) is reachable from the cluster's
subnets and the Tailscale CGNAT range, and nothing else.

## One-time setup

Run from pop. The `socketfilterfw` rules are global, so they survive
`com.user.ollama-serve` restarts and reboots.

```sh
# Verify current state — pop should ALREADY bind 0.0.0.0:11434 (the
# LaunchAgent's OLLAMA_HOST). If it still binds 127.0.0.1, the LaunchAgent
# didn't load; check `launchctl list | grep ollama`.
lsof -nP -iTCP:11434 -sTCP:LISTEN

# Allow ollama inbound (the binary path; /usr/bin/true is a placeholder for
# the macOS Sonoma+ app-based path — see note below).
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add /Applications/Ollama.app/Contents/Resources/ollama

# Allow inbound 11434 from the LAN (cluster nodes + on-LAN MacBook).
sudo /usr/libexec/ApplicationFirewall/socketfilterfw \
  --add /Applications/Ollama.app/Contents/Resources/ollama \
  --block-only none

# Sanity: list active application rules.
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --listapps
```

### `socketfilterfw` and per-IP scoping

`socketfilterfw --add` is binary (allow / block) and does not have a
first-class `--from 192.168.1.0/24` flag. The standard approaches:

| Approach                                         | Pros                                                    | Cons                                                                                               |
| ------------------------------------------------ | ------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| Allow the app + bind on `0.0.0.0` (above)        | Simple, idempotent                                      | Anything routable can hit 11434 — relies on the LAN / Tailnet not being routed past pop's firewall |
| `pf` (packet filter) rules in `/etc/pf.anchors/` | True subnet scoping (`192.168.1.0/24`, `100.64.0.0/10`) | More setup, survives across reboots only if the anchor is loaded via `pf.conf`                     |

The cluster traffic **stays on the LAN/Tailnet** because pop's external
firewall (router) doesn't forward 11434 from the WAN, and the Tailnet
adapter sits on `utun*` which `pf` can scope. The table-above `pf` recipe:

```pf
# /etc/pf.anchors/com.user.ollama-serve
# Allow inbound TCP 11434 from LAN + Tailscale CGNAT only.
pass in quick on en0 inet proto tcp from 192.168.1.0/24 to any port 11434
pass in quick on utun* inet proto tcp from 100.64.0.0/10 to any port 11434
block in quick on en0 inet proto tcp to any port 11434
```

Wire it up in `/etc/pf.conf` with an `anchor "com.user.ollama-serve/*"`
include (after the OS-default anchors) and load with
`sudo pfctl -f /etc/pf.conf`. Test with
`sudo pfctl -sr | grep 11434` after a `pfctl -e`.

## Verification

```sh
# From pop:
lsof -nP -iTCP:11434 -sTCP:LISTEN       # single process on *:11434 (or 0.0.0.0:11434)
curl -s http://192.168.1.6:11434/api/tags | jq .models[].name

# From the work MacBook (or any LAN peer):
curl -s http://192.168.1.6:11434/api/tags | jq .models[].name

# From a Tailscale peer (off-LAN):
curl -s http://100.113.28.62:11434/api/tags | jq .models[].name

# From a cluster pod (viking ns):
kubectl --context=homelab -n viking exec deploy/openviking -- \
  curl -s http://ollama-pop.homelab.local:11434/api/tags | jq .models[].name
# expect: "qwen3.6:35b-mlx"
```

## Rollback

```sh
# If the LaunchAgent path is rolled back to the pre-FEAT-1021
# `ollama serve` started by the Ollama.app login item:
sudo /usr/libexec/ApplicationFirewall/socketfilterfw \
  --remove /Applications/Ollama.app/Contents/Resources/ollama
# And/or remove the pf anchor:
sudo pfctl -a com.user.ollama-serve -F all
```
