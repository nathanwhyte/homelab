# llama

OpenAI-compatible local LLM serving in the `llama` namespace.

This folder uses the llama.cpp deployment path for current GPUs (Pascal `sm_61`).

- `deploy-llamacpp.sh`

## Current endpoint

- Base URL: `http://llama-api.llama.svc.cluster.local/v1`

## Deploy (llama.cpp)

```bash
./llama/deploy-llamacpp.sh
```

## Quick in-cluster test

```bash
kubectl run -it --rm curl --image=curlimages/curl:8.12.1 --restart=Never -- \
  curl -sS http://llama-api.llama.svc.cluster.local/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"current.gguf","messages":[{"role":"user","content":"Write one sentence about homelabs."}],"max_tokens":64}'
```

## Benchmark suite (7B/8B)

Benchmark files:

- `benchmarks/models.json`: model matrix (primary/fallback files + URLs)
- `benchmarks/cases.json`: 12 cases each for `codegen`, `tech_qa`, `basic_qa`
- `benchmarks/run_benchmarks.py`: orchestrates model switch, rollout, scoring, report output
- `run-benchmarks.sh`: convenience launcher

Default scoring weights (accuracy-leaning):

- overall: `60% accuracy + 40% speed`
- accuracy mix: `50% codegen + 30% tech_qa + 20% basic_qa`

Run full suite:

```bash
./llama/run-benchmarks.sh
```

Run only selected models:

```bash
./llama/run-benchmarks.sh --models llama31_8b_q4km,qwen25_7b_q4km
```

Output is written to `llama/benchmarks/results/` as JSON and Markdown ranking reports.

## Files

- `namespace.yaml`: creates namespace `llama`
- `pvc.yaml`: `llama-model-cache` PVC (`100Gi`, `longhorn-ssd`)
- `llamacpp-deployment.yaml`: llama.cpp OpenAI server deployment
- `llamacpp-service.yaml`: ClusterIP service `llama-api`

## Switching model

Update both in `llamacpp-deployment.yaml`:

- init container download URL and output filename
- server `--model` path to the same filename
