# MoE expert-offload benchmark harness

Speed and quality tools for comparing local models on pop (M5 Max, 64 GB) under
a fixed machine memory budget: oMLX with SSD expert offload, llama.cpp with
experts on the CPU, Ollama, or any OpenAI-compatible server. Built during
compendium IDEA-1131 (2026-10); the method that strings these scripts together
is compendium GUIDE-1088, which should be loaded before the first run.

The scripts are standard-library Python except `gguf_reorder.py`. Run them from
a scratch directory **outside the repo**: `run_shadow.py` and `score.py` read and
write `<workdir>/shadow/`, and the default workdir is the current directory.

## Scripts

| Script            | Does                                                                                                                                        |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `balloon.py`      | mlocks 1 GiB chunks until reclaimable memory reaches `--leave-gib`, prints one JSON line, holds until SIGTERM/SIGINT                        |
| `ds_probe.py`     | For each resident fraction: oMLX settings, load, wrap-summary line from the serve log, admitted size, unload                                |
| `ds_bench.py`     | Randomized speed sweep (decode, 3k and 12k prompts) with server-side timing and memory snapshots; oMLX admin mode or `--no-admin`           |
| `ollama_proxy.py` | Ollama `/api/chat` → OpenAI `/v1/chat/completions` shim with strict `json_schema`, fixed sampling and one JSON log line per request         |
| `run_shadow.py`   | Runs compendium `_scripts/batch/proposer-shadow.py` over the 90-case batch gate, one lane at a time, failing loudly on any check            |
| `score.py`        | Per-lane passes, totals with and without the ambiguous cases, median s/case, JSON failures, paired exact McNemar against a reference config |
| `gguf_reorder.py` | Rewrites a split GGUF as one file with non-expert tensors first and routed experts last, for llama.cpp `-cmoe` on Metal                     |
| `gate.py`         | The gate definition shared by `run_shadow.py` and `score.py`: case file, lane and case count per lane, plus the ambiguous-case set          |

## Configuration

Nothing is tied to a checkout path. Flags win over environment variables, which
win over the defaults:

| Flag                    | Environment         | Default                               | Used by                                 |
| ----------------------- | ------------------- | ------------------------------------- | --------------------------------------- |
| `--base` / `--upstream` | `OMLX_BASE`         | `http://127.0.0.1:8000`               | `ds_bench`, `ds_probe`, `ollama_proxy`  |
| `--omlx`                | `OMLX_BASE`         | `http://127.0.0.1:8000`               | `run_shadow`                            |
| `--ollama`              | `OLLAMA_BASE`       | `http://127.0.0.1:11434`              | `run_shadow`                            |
| `--proxy`               | `OFFLOAD_PROXY`     | `http://127.0.0.1:11500`              | `run_shadow`                            |
| `--timmy`               | `TIMMY_OLLAMA`      | timmy's Tailscale Ollama, port 11434  | `run_shadow` (`timmy-gemma4-vlm`)       |
| `--workdir`             | `OFFLOAD_WORKDIR`   | current directory                     | `run_shadow`, `score`                   |
| `--vault`               | `COMPENDIUM_VAULT`  | `~/code/compendium`                   | `run_shadow`                            |
| `--cases-dir`           | `OFFLOAD_CASES_DIR` | this repo's `benchmarks/results`      | `run_shadow`                            |
| `--omlx-model`          | `OMLX_MODEL`        | `Jundot--Qwen3.8-Flash-Next-oQ4e-mtp` | `run_shadow` (`omlx-*` presets, unload) |
| `--omlx-log`            | `OMLX_LOG`          | `<workdir>/omlx-serve-ds.log`         | `run_shadow` (`omlx:` wrap check)       |

The gate's case files come from this repo: `benchmarks/results/<case>/cases.json`
for the five lanes in `gate.py`. `homelab#193` marks two cases ambiguous;
`score.py` reports totals both ways.

## Running

Paths are relative to a scratch workdir; `H` is the homelab checkout.

```bash
H=~/code/homelab/main/benchmarks/offload

# Hold the memory budget (GUIDE-1088 Step 2); stop with pkill -f balloon.py.
nohup python3 $H/balloon.py --leave-gib 47 > balloon.log 2>&1 &

# Which resident fractions does oMLX admit? (Step 3)
python3 $H/ds_probe.py --model <id> --log <serve.log> 0.6 0.5 0.4

# Speed sweep (Step 4): oMLX admin mode, or any OpenAI server with --no-admin.
python3 $H/ds_bench.py --model <id> --fractions 0.6,0.5,0.4,0.3 \
    --server-log <serve.log> --expect-layers 48 --engine-pid <pid> > speed.jsonl
python3 $H/ds_bench.py --model <name> --base http://127.0.0.1:8080 \
    --no-admin <label> --engine-pid <pid> > speed.jsonl

# Quality (Step 5): proxy in front of the server, then the 90-case gate.
nohup python3 $H/ollama_proxy.py --model <id> --port 11500 --log proxy.jsonl &
python3 $H/run_shadow.py --omlx-log <serve.log> \
    'omlx:<label>:<model id>:<fraction>:{"_expect_layers": 48}'
python3 $H/run_shadow.py 'proxy:<label>:<model name>'
python3 $H/run_shadow.py 'ollama:<label>:<tag>'

# Score (Step 6) against a reference configuration.
python3 $H/score.py <reference label> <label> [<label> ...]

# llama.cpp -cmoe on Metal: experts last so the GPU-mapped range stays small.
uv run --with gguf python $H/gguf_reorder.py <first shard> <out.gguf>
```

`run_shadow.py` configuration forms:

| Form                                                                               | Runs                                                                                        |
| ---------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `omlx:<label>:<model id>:<fraction>:<json>`                                        | loads the model in oMLX at that resident fraction; `_expect_layers` enforces the wrap count |
| `proxy:<label>:<model name>`                                                       | a server already behind the proxy (e.g. llama-server via `ollama_proxy.py --upstream`)      |
| `ollama:<label>:<tag>`                                                             | an Ollama tag, stopped afterwards                                                           |
| `omlx-60`, `omlx-12`, `ollama-qwen36-35b`, `ollama-qwen38-27b`, `timmy-gemma4-vlm` | IDEA-1131's presets; the default run is the first three                                     |

## Caveats

- **One local-model consumer at a time.** `run_shadow.py` refuses to start an
  oMLX or proxy configuration while Ollama has a model loaded; nothing else
  checks for a second engine.
- `ds_bench.py`, `ds_probe.py` and `balloon.py` read macOS-only tools
  (`vm_stat`, `footprint`, `memory_pressure`, `sysctl vm.swapusage`).
- `ollama_proxy.py` sampling defaults (`top_p` 0.95, `top_k` 20, `min_p` 0,
  `presence_penalty` 1.5, `repeat_penalty` 1) mirror `qwen3.6:35b-mlx`'s
  Modelfile so every configuration decodes the same way; request options
  override them. Check its log for zero `grammar_warning` after a run.
- A results file with the expected row count is treated as done, so rerunning a
  label resumes it; delete `<workdir>/shadow/<label>-*` to start over.
- `ollama-qwen38-27b` needs the local `qwen3.8:27b-mlx-pp15` tag (the 27B
  Modelfile with `presence_penalty 1.5`); it is not built here.
