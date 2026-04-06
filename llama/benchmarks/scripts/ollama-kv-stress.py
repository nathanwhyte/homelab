#!/usr/bin/env python3
"""KV Cache Stress Test — measures when context spills from VRAM to RAM.

Progressively fills the context window with conversation turns, measuring
time-to-first-token and generation speed at each step. Detects the
inflection point where KV cache overflows GPU memory.

Usage:
    python3 ollama-kv-stress.py                              # auto-detect host, default model
    python3 ollama-kv-stress.py --model mistral-small
    python3 ollama-kv-stress.py --model qwen35-claude --host http://192.168.1.19:11434
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

WIKIPEDIA_BLOCK = """The Industrial Revolution, which took place from the 18th to 19th centuries, was a period
during which predominantly agrarian, rural societies in Europe and America became industrial and urban.
Prior to the Industrial Revolution, which began in Britain in the late 1700s, manufacturing was often done
in people's homes, using hand tools or basic machines. Industrialization marked a shift to powered, special-
purpose machinery, factories and mass production. The iron and textile industries, along with the development
of the steam engine, played central roles in the Industrial Revolution, which also saw improved systems of
transportation, communication and banking. While industrialization brought about an increased volume and
variety of manufactured goods and an improved standard of living for some, it also resulted in often grim
employment and living conditions for the poor and working classes. The Industrial Revolution began in Great
Britain, and many of the technological and architectural innovations were of British origin. By the mid-18th
century, Britain was the world's leading commercial nation, controlling a global trading empire with colonies
in North America and the Caribbean. Britain had major military and political hegemony on the Indian subcontinent;
particularly with the proto-industrialised Mughal Bengal, through the activities of the East India Company.
The development of trade and the rise of business were among the major causes of the Industrial Revolution.
The revolution was facilitated by the agricultural revolution, where better farming techniques led to increased
food production and population growth. Enclosure of common lands displaced small farmers, creating a labor
pool for factories. The availability of natural resources, particularly coal and iron, in Britain provided
essential raw materials. The British banking system and stable government also contributed to an environment
conducive to industrial growth and innovation."""


def call_ollama(base_url, model, messages, max_tokens=256):
    """Send chat request and return detailed timing."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"num_predict": max_tokens, "temperature": 0.7},
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{base_url}/api/chat",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    wall_start = time.monotonic()
    with urllib.request.urlopen(req, timeout=300) as resp:
        r = json.loads(resp.read())
    wall_ms = int((time.monotonic() - wall_start) * 1000)

    eval_count = r.get("eval_count", 0)
    eval_dur = r.get("eval_duration", 1)
    prompt_count = r.get("prompt_eval_count", 0)
    prompt_dur = r.get("prompt_eval_duration", 1)

    return {
        "content": r.get("message", {}).get("content", ""),
        "wall_ms": wall_ms,
        "prompt_tokens": prompt_count,
        "prompt_ms": int(prompt_dur / 1e6),
        "prompt_tok_s": round(prompt_count / (prompt_dur / 1e9), 1) if prompt_dur > 0 else 0,
        "gen_tokens": eval_count,
        "gen_ms": int(eval_dur / 1e6),
        "gen_tok_s": round(eval_count / (eval_dur / 1e9), 1) if eval_dur > 0 else 0,
    }


def get_model_info(base_url, model):
    """Get loaded model VRAM info."""
    try:
        req = urllib.request.Request(f"{base_url}/api/ps", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        for m in data.get("models", []):
            if model in m.get("name", ""):
                return {
                    "size_gb": round(m.get("size", 0) / 1e9, 1),
                    "vram_gb": round(m.get("size_vram", 0) / 1e9, 1),
                }
    except Exception:
        pass
    return None


CONVERSATION_TURNS = [
    # Turn 1: Short question
    {"role": "user", "content": "What were the three most important inventions of the 20th century? Explain each briefly."},
    # Turn 2: Follow-up
    {"role": "user", "content": "How did those inventions change everyday life for ordinary people? Give specific examples."},
    # Turn 3: Context bomb — paste text + analysis
    {"role": "user", "content": f"Read this passage and identify the key themes:\n\n{WIKIPEDIA_BLOCK}\n\nList 5 themes with one sentence each."},
    # Turn 4: Cross-reference (requires remembering turn 1)
    {"role": "user", "content": "Now connect the themes from that passage to the three inventions you described in your first answer. What patterns emerge?"},
    # Turn 5: Second context bomb
    {"role": "user", "content": f"Here's a second passage on a related topic:\n\n{WIKIPEDIA_BLOCK.replace('Industrial Revolution', 'Information Age').replace('18th to 19th', '20th to 21st').replace('steam engine', 'microprocessor')}\n\nCompare and contrast this with the first passage."},
    # Turn 6: Deep cross-reference (requires all prior context)
    {"role": "user", "content": "Synthesize everything we've discussed — the three inventions, both passages, and the patterns you identified. Write a unified thesis statement with supporting evidence from our entire conversation."},
    # Turn 7: More padding
    {"role": "user", "content": f"Expand on your thesis by examining how {WIKIPEDIA_BLOCK[:500]}\n\nrelates to modern technology companies. Name specific companies and draw parallels."},
    # Turn 8: Final deep recall
    {"role": "user", "content": "List every specific example, name, and date you've mentioned across this entire conversation, organized chronologically. This tests your recall of our full discussion."},
]


def main():
    parser = argparse.ArgumentParser(description="KV Cache Stress Test")
    parser.add_argument("--model", default="mistral-small", help="Model name")
    parser.add_argument("--host", default="http://localhost:11434", help="Ollama URL")
    parser.add_argument("--max-turns", type=int, default=8, help="Max conversation turns")
    args = parser.parse_args()

    base_url = args.host.rstrip("/")
    model = args.model

    # Try remote if localhost fails
    try:
        urllib.request.urlopen(f"{base_url}/api/version", timeout=2)
    except Exception:
        base_url = "http://192.168.1.19:11434"
        try:
            urllib.request.urlopen(f"{base_url}/api/version", timeout=2)
        except Exception:
            print("Error: Ollama not reachable", file=sys.stderr)
            sys.exit(1)

    print(f"KV Cache Stress Test")
    print(f"  Host: {base_url}")
    print(f"  Model: {model}")

    # Warmup
    print(f"  Warming up...", end="", flush=True)
    try:
        call_ollama(base_url, model, [{"role": "user", "content": "hi"}], max_tokens=1)
        print(" ready")
    except Exception as e:
        print(f" failed: {e}")
        sys.exit(1)

    info = get_model_info(base_url, model)
    if info:
        print(f"  Model VRAM: {info['vram_gb']} GB / Size: {info['size_gb']} GB")
        print(f"  Free for KV: ~{round(16 - info['vram_gb'], 1)} GB")

    print()
    print(f"  {'Turn':<6} {'Prompt':>7} {'TTFT':>8} {'Prompt t/s':>11} {'Gen t/s':>8} {'Gen tok':>8} {'Wall':>8} {'Ctx':>8}")
    print(f"  {'─'*6} {'─'*7} {'─'*8} {'─'*11} {'─'*8} {'─'*8} {'─'*8} {'─'*8}")

    messages = []
    results = []
    baseline_gen_tps = None
    spill_detected = False
    max_turns = min(args.max_turns, len(CONVERSATION_TURNS))

    for i in range(max_turns):
        turn = CONVERSATION_TURNS[i]
        messages.append(turn)

        try:
            r = call_ollama(base_url, model, messages, max_tokens=256)
        except Exception as e:
            print(f"  {i+1:<6} ERROR: {e}")
            break

        # Add assistant response to conversation
        messages.append({"role": "assistant", "content": r["content"]})

        # Track total context size
        total_ctx = r["prompt_tokens"] + r["gen_tokens"]

        # Detect spill: >30% drop in gen speed from baseline
        if baseline_gen_tps is None and r["gen_tok_s"] > 0:
            baseline_gen_tps = r["gen_tok_s"]

        spill_marker = ""
        if baseline_gen_tps and r["gen_tok_s"] > 0:
            drop = (baseline_gen_tps - r["gen_tok_s"]) / baseline_gen_tps
            if drop > 0.3 and not spill_detected:
                spill_marker = " ← SPILL"
                spill_detected = True
            elif drop > 0.1:
                spill_marker = " ← slow"

        print(
            f"  {i+1:<6} {r['prompt_tokens']:>6}t {r['prompt_ms']:>7}ms {r['prompt_tok_s']:>10} {r['gen_tok_s']:>7} "
            f"{r['gen_tokens']:>7}t {r['wall_ms']:>7}ms {total_ctx:>7}t{spill_marker}"
        )

        results.append({
            "turn": i + 1,
            "prompt_tokens": r["prompt_tokens"],
            "prompt_ms": r["prompt_ms"],
            "prompt_tok_s": r["prompt_tok_s"],
            "gen_tokens": r["gen_tokens"],
            "gen_ms": r["gen_ms"],
            "gen_tok_s": r["gen_tok_s"],
            "wall_ms": r["wall_ms"],
            "total_ctx": total_ctx,
        })

    # Summary
    print()
    if results:
        first = results[0]
        last = results[-1]
        print(f"  Summary:")
        print(f"    Baseline gen speed:  {first['gen_tok_s']} tok/s")
        print(f"    Final gen speed:     {last['gen_tok_s']} tok/s")
        if first["gen_tok_s"] > 0:
            change = ((last["gen_tok_s"] - first["gen_tok_s"]) / first["gen_tok_s"]) * 100
            print(f"    Speed change:       {change:+.1f}%")
        print(f"    Final context size:  {last['total_ctx']} tokens")
        print(f"    Final prompt time:   {last['prompt_ms']}ms")

        if spill_detected:
            spill_turn = next(r for r in results if baseline_gen_tps and r["gen_tok_s"] > 0 and (baseline_gen_tps - r["gen_tok_s"]) / baseline_gen_tps > 0.3)
            print(f"    KV spill detected:   Turn {spill_turn['turn']} (~{spill_turn['total_ctx']} tokens)")
        else:
            print(f"    KV spill detected:   No (all fits in VRAM)")

    # Save results
    outfile = f"kv-stress-{model.replace(':', '-').replace('/', '-')}.json"
    with open(outfile, "w") as f:
        json.dump({
            "model": model,
            "base_url": base_url,
            "model_info": info,
            "results": results,
            "spill_detected": spill_detected,
        }, f, indent=2)
    print(f"    Results saved:       {outfile}")


if __name__ == "__main__":
    main()
