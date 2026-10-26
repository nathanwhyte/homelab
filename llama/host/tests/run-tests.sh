#!/usr/bin/env bash
# Hermetic preparation, runtime and installer contracts; no live services.
set -euo pipefail
[[ $EUID -ne 0 ]] || { echo 'ERROR: run hermetic tests as an unprivileged user; root disables the deployment fixture seam' >&2; exit 1; }
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
cd "$repo"
python3 llama/host/tests/test-ollama-preparation.py
python3 llama/host/tests/test-ollama-runtime.py
python3 llama/host/tests/test-ollama-journal.py
python3 llama/host/tests/test-ollama-attestation.py
python3 llama/host/tests/test-install-host-ollama.py
bash llama/host/tests/test-homelab-ollama-deploy.sh llama/host
python3 llama/host/ollama_prepare.py --recipes llama/ollama render --root "$repo" --check
