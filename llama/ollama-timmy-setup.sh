#!/usr/bin/env bash
set -euo pipefail
# Optimal Ollama configuration for timmy (RX 9070 XT, 16GB VRAM, 32GB RAM)
# Run this script ON timmy after installing Ollama.

echo "=== Configuring Ollama systemd service ==="

sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf > /dev/null <<'EOF'
[Service]
# Listen on all interfaces (so wemby/other nodes can connect)
Environment="OLLAMA_HOST=0.0.0.0"

# Flash attention — required for KV cache quantization
Environment="OLLAMA_FLASH_ATTENTION=1"

# Quantize KV cache to q8_0 (from default f16)
# Cuts KV memory by ~50% with negligible quality loss, doubling effective context
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"

# Keep model loaded indefinitely (dedicated GPU, single-user)
# Default is 5m which wastes 10s reloading between Claude Code prompts
Environment="OLLAMA_KEEP_ALIVE=-1"

# Single concurrent request (Claude Code is single-threaded)
# More slots = more KV cache memory per slot = less context per request
Environment="OLLAMA_NUM_PARALLEL=1"

# Only keep 1 model loaded (16GB can't fit two models)
Environment="OLLAMA_MAX_LOADED_MODELS=1"

# Let Ollama use all available VRAM (default behavior, explicit for clarity)
# Do NOT set OLLAMA_GPU_MEMORY — let it auto-detect the 16GB
EOF

echo "=== Reloading systemd ==="
sudo systemctl daemon-reload

echo "=== Creating optimized Modelfile ==="

# qwen35-claude: optimized for ollama launch claude
cat > /tmp/Modelfile-qwen35-claude <<'MODELFILE'
FROM qwen3.5:9b-q4_K_M

# Context: 65K fits entirely in VRAM (8 attention layers × q8_0 KV ≈ 4.2GB)
# Can push to 131072 but KV starts spilling to RAM above ~114K
PARAMETER num_ctx 65536

# Disable thinking mode — Claude Code needs fast tool calls, not reasoning chains
# Thinking inflates tokens 10-20x and wastes time on simple tool selections
SYSTEM "/no_think"

# Keep model defaults for generation quality
# (temperature=1, top_k=20, top_p=0.95, presence_penalty=1.5)
MODELFILE

echo "=== Restarting Ollama ==="
sudo systemctl restart ollama
sleep 3

echo "=== Creating qwen35-claude model ==="
ollama create qwen35-claude -f /tmp/Modelfile-qwen35-claude

echo "=== Verifying ==="
ollama ps
echo ""
echo "Done. Use from any machine on the LAN:"
echo "  export OLLAMA_HOST=http://timmy:11434"
echo "  ollama launch claude --model qwen35-claude"
