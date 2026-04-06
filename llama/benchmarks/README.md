# Benchmarks

This directory contains all benchmark-related scripts, results, and reports for the homelab LLM infrastructure.

## Directory Structure

```
benchmarks/
├── scripts/          # Benchmark execution scripts
├── results/          # Raw benchmark results (JSON)
│   ├── kv-stress/    # KV cache stress test results
│   ├── speed-bench/  # Speed benchmark results
│   ├── agentic/      # Agentic benchmark results
│   ├── chat/         # Chat benchmark results
│   └── claude-code/ # Claude Code benchmark results
└── reports/          # Generated markdown reports
```

## Scripts

| Script | Description |
|--------|-------------|
| `ollama-kv-stress.py` | Tests KV cache behavior under increasing context sizes |
| `ollama-speed-bench.py` | Measures token generation speed |
| `chat-bench.py` | Chat completion benchmarking |
| `claude-code-bench.py` | Claude Code integration benchmarks |
| `bench-*.sh` | Shell wrappers for various benchmark scenarios |

## Running Benchmarks

```bash
# KV Stress test
python scripts/ollama-kv-stress.py --model qwen3.5:9b --output results/kv-stress/

# Speed benchmark
python scripts/ollama-speed-bench.py --model qwen3.5:9b --output results/speed-bench/
```

## Results Format

Benchmark results are stored as timestamped JSON files:
- Format: `<benchmark-type>-<date>_<hhmmss>.json`
- Example: `kv-stress-qwen35-claude-20260328_143022.json`

## Adding New Benchmarks

1. Add scripts to `scripts/`
2. Configure output to write to appropriate `results/` subdirectory
3. Update this README with usage instructions