#!/usr/bin/env bash
# Hermetic preparation, runtime and installer contracts; no live services.
set -euo pipefail
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
cd "$repo"
python3 llama/host/tests/test-ollama-preparation.py
python3 llama/host/tests/test-ollama-runtime.py
python3 llama/host/tests/test-install-host-ollama.py
bash llama/host/tests/test-homelab-ollama-deploy.sh llama/host
python3 llama/host/ollama_prepare.py --recipes llama/ollama render --root "$repo" --check
