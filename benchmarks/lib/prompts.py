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
    raise ValueError(f"Unknown workload: {name}")
