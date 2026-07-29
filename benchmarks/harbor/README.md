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

| Path                                 | Purpose                                                                |
| ------------------------------------ | ---------------------------------------------------------------------- |
| `configs/hello-world-smoke.yaml`     | IMPR-1109 steps 1-2: hello-world once per matrix model, serial         |
| `configs/gemma4-26b-quant-pair.yaml` | gemma4-26b focus push: nvfp4 vs mxfp8 twins, 3 attempts each           |
| `configs/tb10-gemma4-26b-pair.yaml`  | TB-2.1 10-task quant pair; carries the 3-series condition history      |
| `run-metadata-*.txt`                 | Tracked per-series provenance snapshots (one per series, mandatory)    |
| `tasks/hello-world/`                 | Downloaded via `hb download harbor/hello-world`                        |
| `tb-2-1/`                            | Downloaded TB-2.1 dataset (gitignored)                                 |
| `jobs/`                              | Run output (gitignored; trajectories, verifier stdout/stderr, rewards) |

## Matrix models — verified parameters (2026-07-29)

Effective serving config (`~/.config/ollama/com.user.ollama-serve.plist`):
`OLLAMA_CONTEXT_LENGTH=131072`, `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_NUM_PARALLEL=2`.
No coding tag bakes `num_ctx`, so **131072 is the effective window for all six**.

| Model tag                 | Arch            | Params | Quant              | Sampling (Modelfile, server-side)                 |
| ------------------------- | --------------- | ------ | ------------------ | ------------------------------------------------- |
| `qwen3.6:coding`          | qwen3_5_moe     | 35.1B  | nvfp4 (MLX-origin) | temp 0.6, top_p 0.95, top_k 20, min_p 0, repeat 1 |
| `qwen3.6:coding-gguf`     | qwen35moe       | 36.0B  | Q4_K_M             | temp 0.6, top_p 0.95, top_k 20, min_p 0, repeat 1 |
| `laguna:coding`           | laguna          | 33.4B  | nvfp4              | temp 1, top_p 1, top_k 20                         |
| `nemotron3:coding`        | nemotron_h_omni | 33.0B  | Q4_K_M             | temp 0.6, top_p 0.95                              |
| `gemma4:coding-12b`       | gemma4_unified  | 12.4B  | nvfp4              | temp 1, top_p 0.95, top_k 64                      |
| `gemma4:coding-26b`       | gemma4          | 26.2B  | mxfp8              | temp 1, top_p 0.95, top_k 64                      |
| `gemma4:coding-26b-nvfp4` | gemma4          | 26.2B  | nvfp4 (~17 GB)     | temp 1, top_p 0.95, top_k 64                      |

The gemma4-26b pair are true quant twins (created 2026-07-29 from
`gemma4:26b-mxfp8` / `gemma4:26b-mlx`, same pins — see
`dotfiles/ollama/gemma4-coding-26b-nvfp4.Modelfile`). gemma4 26B is the MoE
size of the family (26B/4B active); MTP is automatic on the MLX path
(Ollama >= 0.31.1, decode-only). ⚠ Never co-load the two 26b tags (27.7 GB +
17 GB on 64 GB); serial trials swap them safely.

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
2. **Agent sampling — pin `temperature` explicitly** (corrected 2026-07-29
   review): the upstream docs list a 0.7 default, but installed Harbor 0.20.0
   defines `temperature=None` — unset temperature is NOT forwarded to the
   model, so the Modelfile value would apply. Explicit pins are still the
   standard (verified to propagate: LiteLLM 1.94.0 emits
   `options: {"temperature": ...}` to Ollama) because they make the effective
   sampling reproducible and independent of Harbor default drift. Pin per
   model: 0.6 qwen/nemotron, 1.0 gemma/laguna.
3. **Remote host (timmy) — RESOLVED**: Terminus-2 accepts an `api_base` kwarg
   (custom endpoint override, INFO-1129) — prefer it over the unconfirmed
   `OLLAMA_API_BASE` env pass-through.
4. Docker Desktop must be running. **arm64 finding (2026-07-29)**: TB-2.1
   prebuilt images are amd64-only and run under Rosetta emulation on pop —
   functional, but task-side compute is emulated and durations aren't
   comparable to x86 runs. All 89 TB tasks ship `environment/Dockerfile` on
   multi-arch bases: set `force_build: true` for native arm64 builds. Never
   mix pulled/emulated and locally-built/native trials in one comparison
   series.
5. **Parser-warning watch**: gemma4 wraps JSON tool calls in prose —
   Terminus-2's json parser recovers with "Extra text detected" warnings.
   Count per model/quant at run end; `parser_name: "xml"` is the fallback
   lever (INFO-1129).
6. **Thinking mode is a series axis — control it with `reasoning_effort`**
   (2026-07-29): Terminus-2's `reasoning_effort` kwarg reaches Ollama as a
   top-level `think` flag via LiteLLM (`"low"/"medium"/"high"` → true;
   anything else incl. `"none"` → false — litellm 1.94.0 ollama
   transformations, verified statically; ERR-1004-correct placement).
   Thinking-on burned ~7k tokens (~7 min) per turn in the TB pilot and
   timed out both polyglot-c-py trials at 4 turns/30 min; `-nothink`
   series runs `reasoning_effort: "none"`. Never mix think modes in one
   comparison series.
7. **Always set `max_turns`** (2026-07-29 loop finding): Terminus-2 stops only
   when the model declares `task_complete: true` twice (declare + confirm) or
   a limit binds; default `max_turns` is unlimited. gemma4 never declares
   completion — it solved regex-log at step 1 then looped 20+ turns hunting
   for the verifier-side test corpus (reproduced in both series). A turn-capped
   agent stops cleanly and still gets verified; a timeout still scores too
   (series-1 trial 1: AgentTimeoutError with reward 1.0) but wastes the full
   ceiling. `max_turns: 20` is the current standard for local models.

Docs-ingest notes (INFO-1128/1129/1130, GUIDE-1075 — snapshot 2026-07-29):

- **BUG-1067 third parser axis**: Terminus-2's `parser_name` kwarg
  (`"json"`/`"xml"`, default json) selects how the agent parses tool calls out
  of raw output — independent of the Ollama wire-format split. Include it in
  any Terminus-2-based tool-call comparison.
- **Raw-output forensics**: `TrajectoryConfig(raw_content=True)` stores raw
  (pre-parse) LLM output in trajectories — use for BUG-1067 whitespace
  evidence through Harbor.
- **Regrade** (`hb job regrade`) re-verifies completed trials with a fixed
  verifier without re-running the agent — but only for separate-mode verifiers
  (`[verifier] environment_mode = "separate"`); worth authoring custom tasks in
  separate mode from the start.
- Registered datasets can run directly: `hb run -d terminal-bench/terminal-bench-2-1 -m <model> -a terminus-2`.

## Next steps (IMPR-1109 adoption sequence)

1. hello-world smoke, GGUF model → 2. MLX twin → 3. BUG-1067 parser-path
   pre-check (`/api/chat` vs `/v1/chat/completions`) → 4. ten Terminal-Bench
   tasks → 5. ten SWE-bench Verified via mini-SWE-agent → 6. BFCL
   native-vs-emulated in Inspect → 7. expand.
