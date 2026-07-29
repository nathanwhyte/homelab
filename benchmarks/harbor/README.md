# Harbor benchmark harness (IMPR-1109)

Harbor (the benchmark framework, <https://github.com/harbor-framework/harbor>) setup
for the PROJ-1003 model matrix on pop. Not to be confused with the goharbor
container registry (`registry.nathanwhyte.dev`).

## ⚠ CLI name collision — use `hb`

`harbor` on PATH resolves to the **goharbor registry CLI** (Homebrew, also
`~/.go/bin/harbor`). The benchmark framework was installed via
`uv tool install harbor`, which shipped three equivalent executables; use `hb`:

```bash
hb run -c configs/hello-world-smoke.yaml --print-config   # validate, no run
hb run -c configs/hello-world-smoke.yaml                  # actual run (see gates)
hb view jobs                                              # results viewer, 127.0.0.1:8080
```

## Layout

| Path                             | Purpose                                                                |
| -------------------------------- | ---------------------------------------------------------------------- |
| `configs/hello-world-smoke.yaml` | IMPR-1109 steps 1-2: hello-world once per matrix model, serial         |
| `tasks/hello-world/`             | Downloaded via `hb download harbor/hello-world`                        |
| `jobs/`                          | Run output (gitignored; trajectories, verifier stdout/stderr, rewards) |

## Matrix models — verified parameters (2026-07-29)

Effective serving config (`~/.config/ollama/com.user.ollama-serve.plist`):
`OLLAMA_CONTEXT_LENGTH=131072`, `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_NUM_PARALLEL=2`.
No coding tag bakes `num_ctx`, so **131072 is the effective window for all six**.

| Model tag             | Arch            | Params | Quant              | Sampling (Modelfile, server-side)                 |
| --------------------- | --------------- | ------ | ------------------ | ------------------------------------------------- |
| `qwen3.6:coding`      | qwen3_5_moe     | 35.1B  | nvfp4 (MLX-origin) | temp 0.6, top_p 0.95, top_k 20, min_p 0, repeat 1 |
| `qwen3.6:coding-gguf` | qwen35moe       | 36.0B  | Q4_K_M             | temp 0.6, top_p 0.95, top_k 20, min_p 0, repeat 1 |
| `laguna:coding`       | laguna          | 33.4B  | nvfp4              | temp 1, top_p 1, top_k 20                         |
| `nemotron3:coding`    | nemotron_h_omni | 33.0B  | Q4_K_M             | temp 0.6, top_p 0.95                              |
| `gemma4:coding-12b`   | gemma4_unified  | 12.4B  | nvfp4              | temp 1, top_p 0.95, top_k 64                      |
| `gemma4:coding-26b`   | gemma4          | 26.2B  | mxfp8              | temp 1, top_p 0.95, top_k 64                      |

Harbor's `model_info` carries **token limits and cost accounting only** — it does
not record or control quantization, sampling, or `num_ctx` (validated against
upstream docs 2026-07-29, see IMPR-1109). Sampling comes from the Modelfiles and
is applied by Ollama server-side. Per IMPR-1109's operational rules, record per
run: Ollama version, manifest digest (`ollama show <tag>` / `ollama list`),
quantization, effective sampling, thinking mode, and loaded context.

## Pre-run gates

1. **One local-model consumer at a time** — never start a Harbor run while a
   PROJ-1003 matrix run (or any other Ollama/MLX consumer) is active. Check:
   `curl -s localhost:11434/api/ps` and `ps aux | grep agentic-coding-bench`.
2. **Agent sampling override check** (open item): confirm whether Terminus-2
   sends an explicit `temperature` in requests — if it does, it overrides the
   Modelfile values above and must be pinned to match the matrix harness.
3. **Remote host (timmy)**: `OLLAMA_API_BASE=http://<timmy>:11434` is the
   LiteLLM-standard override but unconfirmed as passed through by Harbor —
   smoke-test before relying on it.
4. Docker Desktop must be running (arm64 Linux containers; hello-world verified
   config-resolves, not yet executed).

## Next steps (IMPR-1109 adoption sequence)

1. hello-world smoke, GGUF model → 2. MLX twin → 3. BUG-1067 parser-path
   pre-check (`/api/chat` vs `/v1/chat/completions`) → 4. ten Terminal-Bench
   tasks → 5. ten SWE-bench Verified via mini-SWE-agent → 6. BFCL
   native-vs-emulated in Inspect → 7. expand.
