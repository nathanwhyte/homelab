# pop ollama — macOS firewall (socketfilterfw) — FEAT-1021

Cluster pods reach pop's ollama as `http://ollama-pop.homelab.local:11434`
via the `coredns-custom` ConfigMap (resolves to `192.168.1.6`). The path
that gets the packet to pop is **LAN or Tailscale only** — pop ollama is
deliberately not exposed via the public Cloudflare tunnel. Scope the
macOS firewall so the bind on `0.0.0.0:11434` (set by
`com.user.ollama-serve` LaunchAgent) is reachable from the cluster's
subnets and the Tailscale CGNAT range, and nothing else.

## One-time setup — `pf` (recommended)

`pf` is preferred over `socketfilterfw` because it scopes by source
subnet (LAN + Tailscale CGNAT) rather than allowing the binary
unconditionally. Rules live in `/etc/pf.anchors/` and are loaded at
boot via `/etc/pf.conf`.

```sh
# 1. Install the anchor file shipped with this repo.
sudo cp mac/pf/com.user.ollama-serve.anchor /etc/pf.anchors/com.user.ollama-serve
sudo chmod 644 /etc/pf.anchors/com.user.ollama-serve

# 2. Wire it into /etc/pf.conf. Append after the OS-default anchors,
#    before the closing brace:
#
#      load anchor "com.user.ollama-serve" from "/etc/pf.anchors/com.user.ollama-serve"
#
#    (Edit with `sudo vim /etc/pf.conf` or `sudo nano /etc/pf.conf`.)

# 3. Verify the LaunchAgent is bound to 0.0.0.0:11434 (FEAT-1021 Phase 2).
lsof -nP -iTCP:11434 -sTCP:LISTEN

# 4. Load and enable.
sudo pfctl -f /etc/pf.conf
sudo pfctl -e

# 5. Sanity: the rules are live.
sudo pfctl -sr | grep 11434
# expect:
#   pass in quick on en0 inet proto tcp from 192.168.1.0/24 to any port 11434
#   pass in quick on utun* inet proto tcp from 100.64.0.0/10 to any port 11434
#   block in quick on en0 inet proto tcp to any port 11434
```

## Alternative — `socketfilterfw` (binary allow, no subnet scoping)

`socketfilterfw --add` is binary (allow / block) and does not have a
first-class `--from 192.168.1.0/24` flag. Use this only if you can't
edit `/etc/pf.conf`. The `socketfilterfw` rules are global, so they
survive `com.user.ollama-serve` restarts and reboots.

```sh
# Verify current state — pop should ALREADY bind 0.0.0.0:11434 (the
# LaunchAgent's OLLAMA_HOST). If it still binds 127.0.0.1, the LaunchAgent
# didn't load; check `launchctl list | grep ollama`.
lsof -nP -iTCP:11434 -sTCP:LISTEN

# Allow ollama inbound (the binary path).
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add /Applications/Ollama.app/Contents/Resources/ollama

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

### pf path (primary)

```sh
# Flush the anchor and unload from /etc/pf.conf.
sudo pfctl -a com.user.ollama-serve -F all
sudo rm /etc/pf.anchors/com.user.ollama-serve
# And remove the `load anchor` line from /etc/pf.conf
```

### socketfilterfw path (alternative)

```sh
sudo /usr/libexec/ApplicationFirewall/socketfilterfw \
  --remove /Applications/Ollama.app/Contents/Resources/ollama
```
