# Round 3 — Config D
**Time:** 11s | **Tokens out:** 0

## Output
## Summarization API Service

**Purpose**: HTTP server providing LLM-powered summarization for context, conversations, and code changes. Exposes `/summarize` and `/v1/summarize` endpoints.

**Key Functions**:
- `llm_request(messages, max_tokens, temperature)` — Makes synchronous HTTP POST to LLM backend (`LLM_URL`), sends JSON payload with model name, messages, sampling params (`min_p=0.05`, `repeat_penalty=1.1`), returns parsed response with content and usage metrics
- `SummarizerHandler.do_POST()` — Extracts JSON body, selects system prompt by mode ("context"/"conversation"/"code"), constructs user+system message pair, calls LLM, returns JSON response with `ok` and `summary` fields

**Configuration** (environment variables):
- `LLM_URL` — defaults to `http://qwen-summarizer-llm.llama.svc.cluster.local/v1/chat/completions`
- `LLM_MODEL` — defaults to `qwen3-14b`
- `BIND_PORT` — defaults to 8082
- `DEFAULT_MAX_TOKENS` — defaults to 1024

**Notable Patterns**:
- Uses `/no_think` directive in all system prompts to suppress LLM reasoning tokens
- `SYSTEM_PROMPTS` dict maps modes to pre-written prompts
- Allows request-level `system_prompt` override
- Temperature fixed at 0.3 (low randomness)
- No explicit healthz handler visible in snippet (GET /healthz mentioned in docstring but handler incomplete)

**Design Decisions**: 
- Stdlib-only (no external deps except LLM HTTP backend)
- ThreadingHTTPServer for concurrent request handling
- 120s timeout on LLM calls
- Temperature 0.3 + min_p 0.05 for deterministic, focused summaries
