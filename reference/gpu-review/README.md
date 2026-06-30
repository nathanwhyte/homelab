# GPU & AI Infrastructure Review — index

Split of `GPU_AND_AI_REVIEW.md` (compiled 2026-05-01) into per-topic files.
The original 619-line document is still present at the repo root for now
(treat the four files below as the **authoritative** source going forward —
the root document is a duplicate pending deletion in a follow-up).

## Per-topic files

| File | Original section(s) | Lines | Topic |
| --- | --- | --- | --- |
| [`01-hardware-and-timeline.md`](01-hardware-and-timeline.md) | §1 + §2 | 22–110 | Cluster + GPU hardware inventory, Jan–May 2026 setup timeline |
| [`02-llm-architecture.md`](02-llm-architecture.md) | §3 + §7 + §8 | 113–233, 501–606 | LLM architecture evolution, 2026-05-01 state, key design decisions |
| [`03-benchmarks.md`](03-benchmarks.md) | §4 | 237–321 | Model quality + throughput benchmarks (Mar–Apr 2026) |
| [`04-tools-tuning-and-lessons.md`](04-tools-tuning-and-lessons.md) | §5 + §6 + §9 | 323–498, 608–619 | Performance tuning, AI tools deployed, lessons learned |

## Where to look first

- **Current cluster state** — `CLAUDE.md` (service routing, LLM config, topology), `HARDWARE.md` (node specs + ROCm/RDNA4 guardrails), `reference/llm-config.md` (LLM config table)
- **Current external hosts** — `reference/external-routes.md`
- **Current OV sync targets** — `reference/compendium-index.md`
- **GPU history (this split)** — start with [`01-hardware-and-timeline.md`](01-hardware-and-timeline.md) for what was set up, then [`02-llm-architecture.md`](02-llm-architecture.md) for how it evolved and why, [`03-benchmarks.md`](03-benchmarks.md) for the numbers behind the choices, and [`04-tools-tuning-and-lessons.md`](04-tools-tuning-and-lessons.md) for tuning notes and lessons learned

## Staleness note

The original `GPU_AND_AI_REVIEW.md` header (lines 1–16) flags several changes
since 2026-05-01 (IDEA-009 Phases 2-4) including the embedder move to wemby
CUDA, the VLM move to manu CUDA, the OV coordinator/merge/workers removal,
and the Ollama model lineup change. **All four split files inherit this
staleness** — they're useful for tracing the historical decisions and
benchmarks, not for current service routing.

## Source

Header extracted from `GPU_AND_AI_REVIEW.md` lines 1–16. Per-file source
footers record the original line range for each split.
