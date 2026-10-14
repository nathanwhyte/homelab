# OpenViking VLM replay harness

Replays OpenViking 0.4.20's own VLM prompts, rendered by OV's prompt manager over live content, against a Claude model, and measures tokens, latency, rate limits, output validity and cost. Built for compendium IMPR-1311 (Claude Haiku 5.5 as OV's VLM on the Max plan's API credit). Every script is read-only against OpenViking.

| Script | What it sends | Checks |
| --- | --- | --- |
| `vlm_replay.py` | `semantic.document_summary` per file, then `semantic.overview_generation` per directory, as the semantic processor does (Messages API) | tokens, latency, 429s, `anthropic-ratelimit-*` headers; writes each overview beside the stored one |
| `memory_replay.py` | Working Memory (WM) for real session archive pairs: the tool-forced `update_working_memory` call, or with `--create` the creation prompt production runs while `viking/manifests/test/nav-patch/ov_wm_update_patch.py` skips the update call | tool-call schema validity, OV's tolerant parser, all seven WM sections, `thinking_tokens` |
| `ov_backend_probe.py` | One WM creation prompt and one tool-forced update through OV's own `OpenAIVLM` at Anthropic's OpenAI-compatible endpoint, with the config a live window deploys | that `extra_headers`, `extra_request_body` (thinking off) and `max_tokens` reach the model, and that OV parses the tool call |

## Before you replay

- **Test the path production runs.** List the runtime patches in `viking/manifests/test/nav-patch/sitecustomize.py` (`TARGETS`) that touch the code path first. The update replay alone measured a call production never makes.
- **Claude 5 models think by default.** Pass `--no-thinking` (and `--max-tokens 16384` for WM) to measure what a `thinking: disabled` production config spends. With thinking on, 3 of 12 WM rewrites were cut off at 8,192 tokens.
- **Outputs carry real vault and session content.** This repo is public: write `--out` to a scratch directory, never under the repo.

## Running

Use the Python that matches the server (`uv tool install openviking==0.4.20`), with the `ov` CLI configured for the cluster. The key is read from a file and never printed; a key that isn't scoped to one workspace also needs `--workspace-id`.

```bash
P=~/.local/share/uv/tools/openviking/bin/python
$P vlm_replay.py --key-file <key file> --workspace-id <wrkspc_…> --no-thinking --out <scratch dir>
$P memory_replay.py --create --no-thinking --max-tokens 16384 --key-file <key file> --workspace-id <wrkspc_…> --out <scratch dir>
$P ov_backend_probe.py --key-file <key file> --workspace-id <wrkspc_…>
```

`--model` targets another Claude model; `--root` and `--sessions` pick what to sample.

## Results, 2026-10-13 (Claude Haiku 5.5)

| Run | Calls | Input tokens, mean | Output tokens, mean | Latency p50 | Result |
| --- | --- | --- | --- | --- | --- |
| Document summaries (thinking on) | 121 | 2,224 | 361 | 1.9 s | all completed |
| Directory overviews (thinking on) | 6 | 7,989 | 2,974 | 15.6 s | one at the 4,096 cap |
| WM update, tool forced (thinking on) | 12 | 36,441 | 3,312 | 13.3 s | 12/12 returned a tool call; 8/12 match the strict schema |
| WM creation, thinking on, cap 8,192 | 12 | 31,439 | 6,788 | 28.2 s | 9/12 complete, 3 cut off |
| WM creation, thinking off, cap 16,384 | 12 | 31,437 | 3,932 | 14.9 s | 12/12 complete |
| Live window on prod OV (45 min, `ov-vlm-swap.py`) | 198 | 3,195 | 491 | ≤ 2.5 s | 0 errors, $0.11 |

Rerun on 2026-10-14 after the review fixes, which check sections with OV's own `_parse_wm_sections` and the update outcome with OV's own branch logic (12 new archive pairs, thinking off, cap 16,384):

| Run | Calls | Input tokens, mean | Output tokens, mean | Latency p50 | Result |
| --- | --- | --- | --- | --- | --- |
| WM creation | 12 | 33,052 | 4,195 | 15.8 s | 12/12 with all seven sections, by OV's parser |
| WM update, tool forced | 12 | 32,656 | 2,591 | 12.9 s | OV merges 10/12; 2/12 sent `sections` as a JSON string, which OV 0.4.20 can't recover, so it reruns the creation prompt; 8/12 match the strict schema |

The three WMs Haiku wrote in production during the live window also pass OV's parser with all seven sections.

Rate limits from the response headers: 10,000 requests, 10M input and 2M output tokens a minute. OV peaks near 10 calls a minute. Projected spend at 15,000 calls a week: about $36 a month. The live window used `viking/scripts/ov-vlm-swap.py` and `viking/manifests/test/nav-patch/ov_claude_params_patch.py`; see `viking/docs/2026-10-13-impr-1311-haiku-vlm-round2.md`.
