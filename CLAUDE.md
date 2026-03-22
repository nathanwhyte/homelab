# context-mode — MANDATORY routing rules

You have context-mode MCP tools available. These rules are NOT optional — they protect your context window from flooding. A single unrouted command can dump 56 KB into context and waste the entire session.

## BLOCKED commands — do NOT attempt these

### curl / wget — BLOCKED

Any Bash command containing `curl` or `wget` is intercepted and replaced with an error message. Do NOT retry.
Instead use:

- `ctx_fetch_and_index(url, source)` to fetch and index web pages
- `ctx_execute(language: "javascript", code: "const r = await fetch(...)")` to run HTTP calls in sandbox

### Inline HTTP — BLOCKED

Any Bash command containing `fetch('http`, `requests.get(`, `requests.post(`, `http.get(`, or `http.request(` is intercepted and replaced with an error message. Do NOT retry with Bash.
Instead use:

- `ctx_execute(language, code)` to run HTTP calls in sandbox — only stdout enters context

### WebFetch — BLOCKED

WebFetch calls are denied entirely. The URL is extracted and you are told to use `ctx_fetch_and_index` instead.
Instead use:

- `ctx_fetch_and_index(url, source)` then `ctx_search(queries)` to query the indexed content

## REDIRECTED tools — use sandbox equivalents

### Bash (>20 lines output)

Bash is ONLY for: `git`, `mkdir`, `rm`, `mv`, `cd`, `ls`, `npm install`, `pip install`, and other short-output commands.
For everything else, use:

- `ctx_batch_execute(commands, queries)` — run multiple commands + search in ONE call
- `ctx_execute(language: "shell", code: "...")` — run in sandbox, only stdout enters context

### Read (for analysis)

If you are reading a file to **Edit** it → Read is correct (Edit needs content in context).
If you are reading to **analyze, explore, or summarize** → use `ctx_execute_file(path, language, code)` instead. Only your printed summary enters context. The raw file content stays in the sandbox.

### Grep (large results)

Grep results can flood context. Use `ctx_execute(language: "shell", code: "grep ...")` to run searches in sandbox. Only your printed summary enters context.

## Tool selection hierarchy

1. **GATHER**: `ctx_batch_execute(commands, queries)` — Primary tool. Runs all commands, auto-indexes output, returns search results. ONE call replaces 30+ individual calls.
2. **FOLLOW-UP**: `ctx_search(queries: ["q1", "q2", ...])` — Query indexed content. Pass ALL questions as array in ONE call.
3. **PROCESSING**: `ctx_execute(language, code)` | `ctx_execute_file(path, language, code)` — Sandbox execution. Only stdout enters context.
4. **WEB**: `ctx_fetch_and_index(url, source)` then `ctx_search(queries)` — Fetch, chunk, index, query. Raw HTML never enters context.
5. **INDEX**: `ctx_index(content, source)` — Store content in FTS5 knowledge base for later search.

## Subagent routing

When spawning subagents (Agent/Task tool), the routing block is automatically injected into their prompt. Bash-type subagents are upgraded to general-purpose so they have access to MCP tools. You do NOT need to manually instruct subagents about context-mode.

## Output constraints

- Keep responses under 500 words.
- Write artifacts (code, configs, PRDs) to FILES — never return them as inline text. Return only: file path + 1-line description.
- When indexing content, use descriptive source labels so others can `ctx_search(source: "label")` later.

## ctx commands

| Command       | Action                                                                                |
| ------------- | ------------------------------------------------------------------------------------- |
| `ctx stats`   | Call the `ctx_stats` MCP tool and display the full output verbatim                    |
| `ctx doctor`  | Call the `ctx_doctor` MCP tool, run the returned shell command, display as checklist  |
| `ctx upgrade` | Call the `ctx_upgrade` MCP tool, run the returned shell command, display as checklist |

# LLM infrastructure

The cluster runs a shared Qwen3-8B model on timmy's RX 9070 XT (16GB VRAM) serving both OpenViking VLM and interactive tasks.

## Service routing

| Service | Endpoint | Purpose |
|---------|----------|---------|
| Shared LLM | `qwen-summarizer-llm.llama.svc:80` → `llamacpp-rocm:8000` on timmy | All LLM inference (4 parallel slots, 8192 ctx/slot, 32768 total) |
| Embedder | `embedder-llamacpp.viking.svc:8080` on timmy | nomic-embed-text-v1.5 f16 (768-dim, CPU-only, single replica) |
| OpenViking | `openviking.viking.svc:1933` on wemby | Knowledge base API |
| Agent API | `summarizer-api.llama.svc:80` → `:8082` on wemby | Agentic tool-calling loop with OpenViking |

## Agent endpoint

`POST summarizer-api.llama.svc/v1/agent` runs a tool-calling loop: LLM generates tool calls → summarizer-api executes them against OpenViking API → results fed back → repeats until final answer.

Available tools: `viking_search`, `viking_read`, `viking_find`, `viking_ls`, `viking_add_text`.

## Failover

If timmy is down, restore manu's qwen-summarizer by: setting `llama/qwen-summarizer-deployment.yaml` replicas to 1, reverting `llama/llm-alias-service.yaml` selector to `app: qwen-summarizer` / targetPort 8001.

# OpenViking knowledge base organization

See [OPENVIKING.md](OPENVIKING.md) for the full organization guide, including L0/L1/L2 tiered loading, directory structure rules, and cleanup cadence.
