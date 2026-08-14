#!/usr/bin/env bash
set -euo pipefail
# Optimal Ollama configuration for timmy (RX 9070 XT, 16GB VRAM, 32GB RAM)
# Run this script ON timmy after installing Ollama.
#
# NOTE: This is the host-level systemd install path. The live production config
# is the k8s Deployment (llama/ollama-deployment.yaml) — it uses Vulkan
# (GGML_VK_VISIBLE_DEVICES=1), OLLAMA_KV_CACHE_TYPE=q8_0, OLLAMA_NUM_PARALLEL=2,
# and OLLAMA_MAX_LOADED_MODELS=1. If you run this script, reconcile the values
# below with that manifest so the host and cluster configs don't diverge.

echo "=== Configuring Ollama systemd service ==="

sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null <<'EOF'
[Service]
# Listen on all interfaces (so wemby/other nodes can connect)
Environment="OLLAMA_HOST=0.0.0.0"

# Flash attention — required for KV cache quantization
Environment="OLLAMA_FLASH_ATTENTION=1"

# Quantize KV cache to q4_0 (from default f16)
# Cuts KV memory by ~75%, maximizing decode throughput
Environment="OLLAMA_KV_CACHE_TYPE=q4_0"

# Keep model loaded indefinitely (dedicated GPU, single-user)
# Default is 5m which wastes 10s reloading between Claude Code prompts
Environment="OLLAMA_KEEP_ALIVE=-1"

# Single concurrent request (Claude Code is single-threaded)
# More slots = more KV cache memory per slot = less context per request
Environment="OLLAMA_NUM_PARALLEL=1"

# Reuse KV cache for shared prompt prefixes (Claude Code sends same system prompt)
Environment="OLLAMA_MULTIUSER_CACHE=1"

# Only keep 1 model loaded (16GB can't fit two models)
Environment="OLLAMA_MAX_LOADED_MODELS=1"

# Let Ollama use all available VRAM (default behavior, explicit for clarity)
# Do NOT set OLLAMA_GPU_MEMORY — let it auto-detect the 16GB
EOF

echo "=== Reloading systemd ==="
sudo systemctl daemon-reload

echo "=== Creating optimized Modelfile ==="

# qwen-claude: optimized for ollama launch claude
cat >/tmp/Modelfile-qwen-claude <<'MODELFILE'
FROM qwen3.5:9b-q4_K_M

# Context: 32K fits easily in VRAM with q4_0 KV + q4_0 model
# Plenty for Claude Code tool-call prompts, leaves headroom for fast KV access
PARAMETER num_ctx 32768

# Larger batch for faster prompt evaluation (prefill phase)
PARAMETER num_batch 1024

# Keep model defaults for generation quality
# (temperature=1, top_k=20, top_p=0.95, presence_penalty=1.5)
MODELFILE

echo "=== Restarting Ollama ==="
sudo systemctl restart ollama
sleep 3

echo "=== Creating qwen-claude model ==="
ollama create qwen-claude -f /tmp/Modelfile-qwen-claude

echo "=== Installing ollama-exporter ==="

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
sudo mkdir -p /opt/ollama-exporter
sudo cp "$SCRIPT_DIR/ollama-exporter.py" /opt/ollama-exporter/ollama-exporter.py

sudo tee /etc/systemd/system/ollama-exporter.service >/dev/null <<'EOF'
[Unit]
Description=Prometheus exporter for Ollama inference metrics
After=ollama.service
Wants=ollama.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/ollama-exporter/ollama-exporter.py --ollama http://localhost:11434 --port 9111
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ollama-exporter

echo "=== Verifying ==="
ollama ps
echo ""
systemctl is-active ollama-exporter && echo "ollama-exporter: running on :9111" || echo "ollama-exporter: FAILED"
echo ""
echo "Done. Use from any machine on the LAN:"
echo "  export OLLAMA_HOST=http://192.168.1.19:11434"
echo "  ollama launch claude --model qwen-claude"
echo ""
echo "Metrics: http://192.168.1.19:9111/metrics"
