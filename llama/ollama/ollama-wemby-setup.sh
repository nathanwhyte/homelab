#!/usr/bin/env bash
set -euo pipefail
# Ollama configuration for wemby (GTX 1060, 6 GB VRAM).
# Run this script ON wemby after installing Ollama (https://ollama.com/install.sh).
#
# wemby's Ollama is a standalone host daemon: no k8s Service or EndpointSlice
# points at it (compare timmy's ollama-timmy-host). The 1060 is also the
# candidate OpenViking embedder host (TASK-1217), and k8s GPU scheduling cannot
# see VRAM held by this daemon. The settings below keep Ollama small and
# short-lived on that card so a loaded model cannot starve a GPU pod.
#
# The drop-in replaces the installer's environment. The upstream installer
# bakes the invoking shell's PATH (linuxbrew, ~/.bun, ~/.go, ...) into
# /etc/systemd/system/ollama.service; the empty `Environment=` below clears
# that list, so the `ollama` service user falls back to systemd's default PATH.
# Reinstalling or upgrading Ollama rewrites the main unit but leaves this
# drop-in in place.

override_dir=/etc/systemd/system/ollama.service.d
override="$override_dir/override.conf"

echo "=== Configuring Ollama systemd drop-in ==="

sudo mkdir -p "$override_dir"
if [ -f "$override" ]; then
  backup="$override.bak.$(date +%Y%m%d%H%M%S)"
  sudo cp "$override" "$backup"
  echo "Backed up existing drop-in to $backup"
fi

sudo tee "$override" >/dev/null <<'EOF'
[Service]
# Reset every Environment= inherited from the main unit, including the
# installer's copy of the login shell PATH.
Environment=

# Listen on all interfaces. Nothing in the cluster uses this daemon; narrow to
# the Tailscale IP or 127.0.0.1 once LAN clients are confirmed unneeded.
Environment="OLLAMA_HOST=0.0.0.0:11434"

# Flash attention: lower attention memory, and required for a quantized KV cache.
Environment="OLLAMA_FLASH_ATTENTION=1"

# q8_0 halves KV memory versus f16 with negligible quality loss. q4_0 (timmy's
# script) saves more but is not worth the loss for the small models 6 GB holds.
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"

# Two concurrent requests per loaded model. Ollama allocates KV cache for
# context x slots up front, so the default context is pinned at 4096: two
# slots reserve an 8192-token cache (q8_0), small enough for the 6 GB card.
# A request that sets a larger num_ctx still gets it, at 2x that cost.
Environment="OLLAMA_NUM_PARALLEL=2"
Environment="OLLAMA_CONTEXT_LENGTH=4096"

# One model at a time: 6 GB cannot hold two alongside anything else.
Environment="OLLAMA_MAX_LOADED_MODELS=1"

# Unload after 5 minutes idle. Never -1 here: a pinned model would hold VRAM
# the k8s scheduler believes is free (the embedder has ~472 MiB of margin).
Environment="OLLAMA_KEEP_ALIVE=5m"
EOF

echo "=== Reloading systemd and restarting Ollama ==="
sudo systemctl daemon-reload
sudo systemctl enable ollama
sudo systemctl restart ollama

echo "=== Waiting for Ollama API ==="
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo "=== Effective configuration ==="
systemctl show ollama -p Environment --no-pager
curl -fsS http://127.0.0.1:11434/api/version && echo
ollama ps

echo ""
echo "Done. Clients reach this daemon at:"
echo "  export OLLAMA_HOST=http://192.168.1.9:11434"
