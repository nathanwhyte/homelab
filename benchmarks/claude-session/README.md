# Claude Code session-start latency benchmark

Measures why fresh Claude Code sessions against local Ollama models (e.g.
`oclauderb` → `ollama launch claude --model qwen3.6:35b-mlx`) take a long time
to produce their first response. Unlike `benchmarks/ollama/`, which hits the
Ollama API directly, this harness runs real headless Claude Code sessions and
decomposes first-turn latency into CLI boot, hook execution, model
load/KV-allocation, and injected-context prefill.

## How it measures

Each run spawns `ollama launch claude --model <m> -- -p <prompt>
--output-format stream-json --verbose --max-turns 1` and timestamps the
stream events:

| Metric                | Meaning                                                            |
| --------------------- | ------------------------------------------------------------------ |
| `t_init_s`            | spawn → `system:init` event — CLI boot + SessionStart hooks        |
| `t_first_assistant_s` | spawn → first assistant event — end-to-end TTFT the user perceives |
| `t_total_s`           | spawn → `result` event                                             |
| `input_tokens`        | prefill volume actually sent (system prompt + injections + prompt) |
| `cache_read_*`        | prompt-cache hits reported by the endpoint (if any)                |

`t_first_assistant_s - t_init_s` ≈ model load (cold runs) + prefill time.
Combine `input_tokens` with `benchmarks/ollama/tools/prefill-size-breakdown.py`
throughput numbers to check the prefill math closes.

## Scenarios (`configs/pop-claude-session-qwen36.toml`)

| Scenario                            | Isolates                                                 |
| ----------------------------------- | -------------------------------------------------------- |
| `bare-warm` vs `full-warm`          | injected-context volume (plugins/claude-mem/CLAUDE.md)   |
| `*-warm` vs `*-cold`                | model weight load + KV-cache allocation                  |
| `full-warm-ctx32k` vs `-ctx256k`    | context-window size → KV allocation cost (both run cold) |
| run 1 vs runs 2–3 within a scenario | Ollama KV prefix-cache reuse of the static system prompt |

## Running

```bash
cd ~/code/homelab
uv run python benchmarks/claude-session/session-bench.py \
    benchmarks/claude-session/configs/pop-claude-session-qwen36.toml
# subset / quick pass:
uv run python benchmarks/claude-session/session-bench.py \
    benchmarks/claude-session/configs/pop-claude-session-qwen36.toml \
    --scenario bare-warm --scenario full-warm --repeats 2
```

Results land in `benchmarks/results/claude-session-pop-qwen36-35b-<stamp>.json`.

## Caveats

- **Do not run while other benchmarks are using the model** — cold scenarios
  issue `ollama stop`, and concurrent load skews every timing.
- Within-scenario repeats share the static system-prompt prefix; if Ollama's
  prefix cache engages, runs 2–3 report lower prefill time than run 1. That is
  signal, not noise — report run 1 and the repeat median separately (same
  gotcha that invalidated the first prefill-size-breakdown run on 2026-07-07).
- `full-*` scenarios execute the real SessionStart hooks (claude-mem,
  context-mode) with `--dangerously-skip-permissions` in `~/code/compendium`,
  but with `--max-turns 1` and a "Reply with exactly: OK" prompt, so no vault
  writes occur. claude-mem will still record observations for these runs.
- The bare config dir is materialized under the results workdir with
  `hasCompletedOnboarding` pre-set; delete the workdir to reset it.
