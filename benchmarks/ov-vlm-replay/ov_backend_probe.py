"""Drive OpenViking 0.4.20's own `OpenAIVLM` at Anthropic's OpenAI-compatible endpoint
with the config a live window would deploy (IMPR-1311 round 2 prep).

Checks that the workspace header, `thinking: disabled` and `max_tokens` reach the
model, compares completion tokens with thinking on and off on one WM creation prompt,
and sends a tool-forced `update_working_memory` call through OV's own async path and
parser. `temperature` is passed as None here; on the cluster homelab's
`ov_claude_params_patch.py` drops it, because Claude 4.7+ models answer 400 to it.
The key is read from a file and never printed.
"""

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path

from openviking.models.vlm.backends.openai_vlm import OpenAIVLM
from openviking.session.session import WM_UPDATE_TOOL

sys.path.insert(0, str(Path(__file__).parent))
from memory_replay import build, candidates, sections_missing

MODEL = "claude-haiku-5-5"  # --model


def make_vlm(key: str, workspace: str, body: dict | None) -> OpenAIVLM:
    return OpenAIVLM(
        {
            "provider": "openai",
            "model": MODEL,
            "api_key": key,
            "api_base": "https://api.anthropic.com/v1/",
            "extra_headers": {"anthropic-workspace-id": workspace},
            "extra_request_body": body or {},
            "max_tokens": 16384,
            "timeout": 300,
            # Haiku 5.5 rejects `temperature`; VLMConfig cannot express null, so this
            # stands in for the patch round 2 needs.
            "temperature": None,
        }
    )


def raw_call(vlm: OpenAIVLM, prompt: str, tools=None, tool_choice=None) -> dict:
    """Same kwargs OpenViking sends, but keep the raw usage block."""
    kwargs = vlm._build_text_kwargs(prompt, tools, tool_choice, None, vlm.thinking)
    r = vlm.get_client().chat.completions.create(**kwargs)
    c = r.choices[0]
    return {
        "sent": {k: kwargs[k] for k in ("max_tokens", "extra_body") if k in kwargs},
        "finish": c.finish_reason,
        "usage": r.usage.model_dump() if r.usage else None,
        "text": c.message.content or "",
        "tool_calls": c.message.tool_calls,
    }


async def main():
    global MODEL
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-file", required=True, type=Path)
    ap.add_argument("--workspace-id", required=True)
    ap.add_argument("--model", default=MODEL)
    a = ap.parse_args()
    MODEL = a.model
    key = a.key_file.read_text().strip()

    pairs = candidates(20, random.Random(1311))
    job = next(j for _, p, c in pairs if (j := build(p, c, True)))
    print("prompt chars", len(job["prompt"]))

    for label, body in (
        ("default", None),
        ("thinking disabled", {"thinking": {"type": "disabled"}}),
    ):
        r = raw_call(make_vlm(key, a.workspace_id, body), job["prompt"])
        print(
            f"\n[{label}] sent={r['sent']} finish={r['finish']} sections_missing={sections_missing(r['text'])}"
        )
        print("  usage", json.dumps(r["usage"]))
        print(
            "  text chars",
            len(r["text"]),
            "chars/completion token",
            round(len(r["text"]) / r["usage"]["completion_tokens"], 2),
        )

    # Tool-forced update call through OpenViking's own async path and parser.
    upd = build(*next((p, c) for _, p, c in pairs if build(p, c, False)), False)
    vlm = make_vlm(key, a.workspace_id, {"thinking": {"type": "disabled"}})
    resp = await vlm.get_completion_async(
        prompt=upd["prompt"],
        tools=[WM_UPDATE_TOOL],
        tool_choice={"type": "function", "function": {"name": "update_working_memory"}},
    )
    tc = resp.tool_calls[0] if resp.tool_calls else None
    args = tc.arguments if tc else None
    print(
        "\n[tool-forced update via get_completion_async] has_tool_calls",
        resp.has_tool_calls,
        "finish",
        resp.finish_reason,
        "usage",
        resp.usage,
        "args keys",
        list(args)[:3] if isinstance(args, dict) else type(args).__name__,
        "sections",
        len(args.get("sections", {})) if isinstance(args, dict) else None,
    )


if __name__ == "__main__":
    asyncio.run(main())
