"""Shared benchmark prompt workloads.

These prompts are version-controlled so that benchmark results can be compared
across time. Changing a prompt changes the workload identity and should be
reflected in the harness config/version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from tool_calls import tool_calling_prompts


SUB_AGENT_PROMPTS = [
    "Write a Python function that validates an email address. Include a docstring and three test cases.",
    "Explain the trade-offs between REST and gRPC APIs in three short paragraphs.",
    "Design a minimal Prometheus exporter for a fictional job queue. List the metrics and their labels.",
    "Refactor this snippet into a class-based state machine: a door can be open, closed, or locked; transitions must be valid.",
    "Summarize the key differences between symmetric and asymmetric encryption for a junior developer.",
]


AGENTIC_PROMPTS = [
    """Review the following Python function and produce a structured code review. Identify at least three concrete issues (bugs, style, or maintainability) and suggest fixes.

```python
def process_orders(orders, users, apply_discount=False):
    results = []
    for order in orders:
        user = None
        for u in users:
            if u['id'] == order['user_id']:
                user = u
                break
        if user is None:
            print("User not found for order", order['id'])
            continue
        total = order['amount']
        if apply_discount:
            total = total * 0.9
        results.append({
            'order_id': order['id'],
            'user_email': user['email'],
            'total': total,
            'items': order['items']
        })
    return results
```

Provide your review as a JSON object with a top-level key \"issues\" containing an array of objects, each with \"severity\", \"line\", \"description\", and \"suggestion\".""",
    """You are assisting with a system design task. A team wants to build a real-time collaborative note-taking application that supports offline editing, conflict resolution, and sync across mobile and desktop clients. Propose a high-level architecture covering data model, sync protocol, and conflict resolution strategy. Keep your response under 1500 words and use bullet points for trade-offs.""",
    """Summarize the following multi-turn conversation into a concise status update. Extract any decisions made, open questions, and action items with owners.

Alice: We need to pick a vector database for the knowledge base.
Bob: I've used pgvector and Chroma; pgvector scales better with our existing Postgres.
Alice: What about embedding dimension and index type?
Bob: Qwen3-Embedding-4B outputs 2560 dimensions. We'll need ivfflat or hnsw.
Alice: Let's go with pgvector + hnsw. Bob, can you prototype the schema?
Bob: Yes, I'll have a draft by Friday.
Alice: I'll review it against the OpenViking ingest pipeline. One concern: how do we handle re-indexing after bulk updates?
Bob: We'll batch updates and run a maintenance job weekly.

Provide the output as JSON with keys: \"decisions\", \"open_questions\", \"action_items\".""",
]


MEM0_EXTRACTION_PROMPT = """You are a memory extraction assistant. Your task is to read the conversation below and emit a single JSON object with a top-level key "memory" containing an array of memory operations. Each operation must have keys: "data" (string), "event" (string), "category" (one of: habits, preferences, goals, relationships, plans, other), and "score" (number 0.0-1.0). Do not output any text outside the JSON object.

Conversation:
User: I've been trying to wake up at 6am to work on my side project before my day job. I prefer dark mode for all my coding tools. My partner and I are planning a trip to Japan in the spring. I want to improve my Python async skills this year, and I usually review my weekly goals on Sunday evenings. I dislike noisy open-plan offices.

Extract memories as JSON:"""


VLM_VISION_PROMPT = "Describe this image in one sentence, then list three specific details visible in it."


@dataclass
class Workload:
    name: str
    prompts: list[str]
    expected_tools: list[Optional[str]] = field(default_factory=list)


def sub_agent_workload(count: int) -> Workload:
    """Return a workload of distinct short sub-agent prompts."""
    prompts = [SUB_AGENT_PROMPTS[i % len(SUB_AGENT_PROMPTS)] for i in range(count)]
    return Workload(name="sub_agent", prompts=prompts)


def prefix_cache_workload(count: int) -> Workload:
    """Return a workload that repeats the same prompt to exercise prefix caching."""
    return Workload(name="prefix_cache", prompts=[SUB_AGENT_PROMPTS[0]] * count)


def mixed_mem0_workload(count: int) -> Workload:
    """Return a workload mixing one mem0-style extraction with sub-agent prompts."""
    prompts = [MEM0_EXTRACTION_PROMPT]
    i = 0
    while len(prompts) < count:
        prompts.append(SUB_AGENT_PROMPTS[i % len(SUB_AGENT_PROMPTS)])
        i += 1
    return Workload(name="mixed_mem0", prompts=prompts)


def vlm_vision_workload() -> Workload:
    """Return the placeholder VLM vision-understanding workload."""
    return Workload(name="vlm_vision", prompts=[VLM_VISION_PROMPT])


def agentic_workload(count: int) -> Workload:
    """Return a workload of longer, complex agentic prompts."""
    prompts = []
    i = 0
    while len(prompts) < count:
        prompts.append(AGENTIC_PROMPTS[i % len(AGENTIC_PROMPTS)])
        i += 1
    return Workload(name="agentic", prompts=prompts)


def tool_calling_workload(count: int) -> Workload:
    """Return a workload of tool-calling prompts with expected tool names."""
    prompts, expected_tools = tool_calling_prompts(count)
    return Workload(name="tool_calling", prompts=prompts, expected_tools=expected_tools)


# Edit-prediction workloads (IMPR-1037 / TASK-1111): prefill-heavy raw code
# prompts at three context sizes, small fixed output. Latency shape matters
# (TTFT vs prefill size at concurrency 1), not format-token fidelity, so the
# zeta variant uses plain continuation while deepseek uses its FIM markers.
# Requires raw = true in the [ollama] config so no chat template is applied.
_EP_CODE_BLOCK = '''\
import asyncio
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PipelineRun:
    run_id: str
    pipeline: str
    status: str = "pending"
    retries: int = 0
    errors: list[str] = field(default_factory=list)

    def mark_failed(self, reason: str) -> None:
        self.status = "failed"
        self.errors.append(reason)

    def should_retry(self, max_retries: int = 3) -> bool:
        return self.status == "failed" and self.retries < max_retries


async def poll_run(client, run: PipelineRun, interval_s: float = 2.0) -> str:
    """Poll a pipeline run until it reaches a terminal state."""
    terminal = {"completed", "failed", "cancelled"}
    while run.status not in terminal:
        await asyncio.sleep(interval_s)
        payload = await client.get_run(run.run_id)
        run.status = payload.get("status", run.status)
    return run.status


def summarize_runs(runs: list[PipelineRun]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for run in runs:
        counts[run.status] = counts.get(run.status, 0) + 1
    return counts
'''

# Target prefill sizes in tokens (~4 chars/token heuristic on code).
_EP_PREFILL_TOKENS = [500, 2000, 8000]


def _ep_code_context(target_tokens: int) -> str:
    """Build a deterministic code context of roughly target_tokens tokens."""
    target_chars = target_tokens * 4
    parts: list[str] = []
    i = 0
    while sum(len(p) for p in parts) < target_chars:
        parts.append(f"# module_{i}.py\n{_EP_CODE_BLOCK}")
        i += 1
    return "\n\n".join(parts)


def _ep_split(context: str) -> tuple[str, str]:
    """Split context into prefix/suffix, dropping a ~10% middle chunk.

    The dropped chunk is the FIM "hole": with a contiguous prefix/suffix the
    correct infill is empty and models emit EOS immediately, so the decode
    phase would measure nothing.
    """
    cut1 = context.rfind("\n", 0, int(len(context) * 0.65))
    cut2 = context.find("\n", int(len(context) * 0.75))
    return context[: cut1 + 1], context[cut2 + 1 :]


def edit_prediction_workload(count: int, fim_format: str) -> Workload:
    """Return prefill-heavy edit-prediction prompts cycling context sizes.

    Every prompt starts with a unique deterministic salt line so no two
    prompts share a prefix. Without this, server prompt/prefix caches
    (mlx_lm.server, Ollama) skip prefill on repeats and prefix-extensions
    and the benchmark reports cache latency instead of prefill latency.
    Run these with repeats = 1 — repeating an identical prompt list
    reintroduces the cache hit.
    """
    prompts = []
    for i in range(count):
        salt = f"# benchmark request {i} nonce {i * 2654435761 % 999999937}\n"
        context = salt + _ep_code_context(
            _EP_PREFILL_TOKENS[i % len(_EP_PREFILL_TOKENS)]
        )
        prefix, suffix = _ep_split(context)
        # Per-family PSM sentinels (INFO-1005). raw=true in the config so the
        # chat template doesn't rewrite these. Native /api/generate `suffix`
        # is the production path; raw+markers is the benchmark control path.
        if fim_format == "deepseek":
            prompts.append(
                f"<｜fim▁begin｜>{prefix}<｜fim▁hole｜>{suffix}<｜fim▁end｜>"
            )
        elif fim_format == "qwen":
            prompts.append(
                f"<|fim_prefix|>{prefix}<|fim_suffix|>{suffix}<|fim_middle|>"
            )
        elif fim_format == "starcoder2":
            prompts.append(f"<fim_prefix>{prefix}<fim_suffix>{suffix}<fim_middle>")
        elif fim_format == "codegemma":
            prompts.append(
                f"<|fim_prefix|>{prefix}<|fim_suffix|>{suffix}<|fim_middle|>"
            )
        elif fim_format == "mistral":
            # Codestral/Mistral PSM (INFO-1005): suffix-then-prefix in the
            # sentinel order, no raw mode strictly required (native .Suffix
            # template field works), but raw+markers keeps this comparable
            # to the other families in the matrix.
            prompts.append(f"[SUFFIX]{suffix}[PREFIX] {prefix}")
        else:  # zeta / plain continuation (no FIM sentinels)
            prompts.append(prefix)
    return Workload(name=f"edit_prediction_{fim_format}", prompts=prompts)


def select_workload(name: str, count: int) -> Workload:
    """Select a named workload."""
    if name == "sub_agent":
        return sub_agent_workload(count)
    if name == "prefix_cache":
        return prefix_cache_workload(count)
    if name == "mixed_mem0":
        return mixed_mem0_workload(count)
    if name == "agentic":
        return agentic_workload(count)
    if name == "vlm_vision":
        return vlm_vision_workload()
    if name == "tool_calling":
        return tool_calling_workload(count)
    if name == "edit_prediction_zeta":
        return edit_prediction_workload(count, "zeta")
    if name == "edit_prediction_deepseek":
        return edit_prediction_workload(count, "deepseek")
    if name == "edit_prediction_qwen":
        return edit_prediction_workload(count, "qwen")
    if name == "edit_prediction_starcoder2":
        return edit_prediction_workload(count, "starcoder2")
    if name == "edit_prediction_codegemma":
        return edit_prediction_workload(count, "codegemma")
    if name == "edit_prediction_mistral":
        return edit_prediction_workload(count, "mistral")
    raise ValueError(f"Unknown workload: {name}")
