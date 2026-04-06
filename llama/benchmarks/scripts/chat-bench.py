#!/usr/bin/env python3
"""Non-code chat benchmark — tests general knowledge, reasoning, summarization, and instruction following.

Usage:
    python3 chat-bench.py
    python3 chat-bench.py --models gemma3-12b --runs 1
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

MODELS = {
    "gemma3-12b": {
        "type": "ollama",
        "base_url": "http://192.168.1.19:11434",
        "model": "gemma3:12b-it-qat",
        "label": "Gemma3 12B QAT",
    },
    "mistral-nemo": {
        "type": "ollama",
        "base_url": "http://192.168.1.19:11434",
        "model": "mistral-nemo:latest",
        "label": "Mistral Nemo 12B",
    },
    "mistral-small": {
        "type": "ollama",
        "base_url": "http://192.168.1.19:11434",
        "model": "mistral-small:latest",
        "label": "Mistral Small 24B",
    },
    "qwen35-9b": {
        "type": "ollama",
        "base_url": "http://192.168.1.19:11434",
        "model": "qwen3.5:9b-q4_K_M",
        "label": "Qwen3.5 9B Q4",
    },
    "sonnet-46": {
        "type": "claude",
        "model": "claude-sonnet-4-6",
        "label": "Claude Sonnet 4.6",
    },
    "opus-46": {
        "type": "claude",
        "model": "claude-opus-4-6",
        "label": "Claude Opus 4.6",
    },
}


def _strip_think(text):
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def call_claude(config, messages, max_tokens=1024, temperature=0.3):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"content": "ERROR: ANTHROPIC_API_KEY not set", "elapsed_ms": 0,
                "tokens_per_sec": None, "output_tokens": 0, "input_tokens": 0}

    system_text = ""
    api_messages = []
    for m in messages:
        if m["role"] == "system":
            system_text = m["content"]
        else:
            api_messages.append(m)

    payload = {
        "model": config["model"],
        "max_tokens": max_tokens,
        "messages": api_messages,
        "temperature": temperature,
    }
    if system_text:
        payload["system"] = system_text

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            r = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"content": f"ERROR: {e}", "elapsed_ms": 0, "tokens_per_sec": None,
                "output_tokens": 0, "input_tokens": 0}
    elapsed_ms = int((time.monotonic() - start) * 1000)

    content = ""
    for block in r.get("content", []):
        if block["type"] == "text":
            content += block["text"]

    usage = r.get("usage", {})
    return {
        "content": content,
        "elapsed_ms": elapsed_ms,
        "tokens_per_sec": None,
        "output_tokens": usage.get("output_tokens", 0),
        "input_tokens": usage.get("input_tokens", 0),
    }


def call_model(config, messages, max_tokens=1024, temperature=0.3):
    if config.get("type") == "claude":
        return call_claude(config, messages, max_tokens, temperature)
    return call_ollama(config, messages, max_tokens, temperature)


def call_ollama(config, messages, max_tokens=1024, temperature=0.3):
    payload = {
        "model": config["model"],
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{config['base_url']}/api/chat",
        data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            r = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"content": f"ERROR: {e}", "elapsed_ms": 0, "tokens_per_sec": None,
                "output_tokens": 0, "input_tokens": 0}
    elapsed_ms = int((time.monotonic() - start) * 1000)

    content = _strip_think(r.get("message", {}).get("content", ""))
    eval_count = r.get("eval_count", 0)
    eval_dur = r.get("eval_duration", 1)
    tps = round(eval_count / (eval_dur / 1e9), 1) if eval_dur > 0 else None

    return {
        "content": content,
        "elapsed_ms": elapsed_ms,
        "tokens_per_sec": tps,
        "output_tokens": eval_count,
        "input_tokens": r.get("prompt_eval_count", 0),
    }


# ── Grading helpers ──────────────────────────────────────────────────────

def _grade_keywords(resp, required, threshold=0.6):
    text = (resp.get("content") or "").lower()
    hits = sum(1 for kw in required if kw.lower() in text)
    score = hits / len(required) if required else 0
    missing = [kw for kw in required if kw.lower() not in text]
    if score >= threshold:
        return score, f"Found {hits}/{len(required)} keywords"
    return score, f"Only {hits}/{len(required)} keywords; missing: {missing}"


def _grade_any_phrase(resp, phrases):
    text = (resp.get("content") or "").lower()
    for phrase in phrases:
        if phrase.lower() in text:
            return 1.0, f"Found: '{phrase}'"
    return 0.0, f"None of {phrases[:3]}... found"


def _grade_format(resp, checks):
    """Grade by structural format checks."""
    text = resp.get("content") or ""
    hits = 0
    for check_name, check_fn in checks:
        if check_fn(text):
            hits += 1
    score = hits / len(checks)
    return score, f"{hits}/{len(checks)} format checks passed"


def _grade_length(resp, min_words, max_words):
    text = resp.get("content") or ""
    words = len(text.split())
    if min_words <= words <= max_words:
        return 1.0, f"{words} words (target: {min_words}-{max_words})"
    if words < min_words:
        return 0.3, f"Too short: {words} words (min: {min_words})"
    return 0.5, f"Too long: {words} words (max: {max_words})"


def _grade_factual(resp, facts, anti_facts=None):
    """Grade factual accuracy: must contain facts, must NOT contain anti_facts."""
    text = (resp.get("content") or "").lower()
    fact_hits = sum(1 for f in facts if f.lower() in text)
    fact_score = fact_hits / len(facts) if facts else 1.0
    if anti_facts:
        anti_hits = sum(1 for f in anti_facts if f.lower() in text)
        if anti_hits > 0:
            return max(0, fact_score - 0.3), f"Contains incorrect info; facts: {fact_hits}/{len(facts)}"
    if fact_score >= 0.6:
        return fact_score, f"Factual: {fact_hits}/{len(facts)}"
    return fact_score, f"Missing facts: {fact_hits}/{len(facts)}"


# ── Tests ────────────────────────────────────────────────────────────────

TESTS = [
    # ── Category 1: General Knowledge ──
    {
        "id": "C1",
        "name": "Science Fact",
        "category": "knowledge",
        "messages": [
            {"role": "user", "content": "What causes the seasons on Earth? Answer in 2-3 sentences."},
        ],
        "grade": lambda r: _grade_factual(r,
            ["tilt", "axis", "23", "sun"],
            anti_facts=["distance from the sun"]),
    },
    {
        "id": "C2",
        "name": "History",
        "category": "knowledge",
        "messages": [
            {"role": "user", "content": "Who was the first person to walk on the moon, and in what year?"},
        ],
        "grade": lambda r: _grade_factual(r, ["armstrong", "1969"]),
    },
    {
        "id": "C3",
        "name": "Geography",
        "category": "knowledge",
        "messages": [
            {"role": "user", "content": "What are the five largest countries by area? Just list them in order."},
        ],
        "grade": lambda r: _grade_factual(r, ["russia", "canada", "china", "united states", "brazil"]),
    },
    # ── Category 2: Reasoning ──
    {
        "id": "C4",
        "name": "Logic Puzzle",
        "category": "reasoning",
        "messages": [
            {"role": "user", "content": "A farmer has a fox, a chicken, and a bag of grain. He needs to cross a river in a boat that can only carry him and one item. If left alone, the fox will eat the chicken, and the chicken will eat the grain. How does he get everything across?"},
        ],
        "grade": lambda r: _grade_keywords(r,
            ["chicken", "fox", "grain", "back"],
            threshold=0.75),
    },
    {
        "id": "C5",
        "name": "Math Word Problem",
        "category": "reasoning",
        "messages": [
            {"role": "user", "content": "A train leaves Station A at 9:00 AM traveling at 60 mph. Another train leaves Station B (300 miles away) at 10:00 AM traveling toward Station A at 90 mph. At what time do they meet?"},
        ],
        "grade": lambda r: _grade_any_phrase(r, ["11:24", "11:24 am", "11:24am", "2 hours and 24 minutes", "2:24"]),
    },
    {
        "id": "C6",
        "name": "Causal Reasoning",
        "category": "reasoning",
        "messages": [
            {"role": "user", "content": "If all roses are flowers, and some flowers fade quickly, can we conclude that some roses fade quickly? Explain why or why not."},
        ],
        "grade": lambda r: _grade_any_phrase(r, ["cannot", "can't", "no", "not necessarily", "does not follow", "invalid", "doesn't follow", "not valid"]),
    },
    # ── Category 3: Summarization ──
    {
        "id": "C7",
        "name": "Paragraph Summary",
        "category": "summarization",
        "messages": [
            {"role": "user", "content": """Summarize this in one sentence:

The Great Barrier Reef, located off the coast of Queensland, Australia, is the world's largest coral reef system, stretching over 2,300 kilometers. It is composed of over 2,900 individual reef systems and 900 islands. The reef supports an extraordinary diversity of life, including 1,500 species of fish, 400 types of coral, and numerous species of mollusks, sea turtles, dolphins, and sharks. However, the reef has faced significant threats in recent decades, including coral bleaching caused by rising ocean temperatures, pollution from agricultural runoff, and damage from tropical cyclones. Conservation efforts are ongoing, but scientists warn that without significant action to address climate change, the reef could suffer irreversible damage by 2050."""},
        ],
        "grade": lambda r: (
            lambda s1, s2: (min(s1[0] + s2[0], 1.0), f"{s1[1]}; {s2[1]}")
        )(
            _grade_keywords(r, ["reef", "threat", "climate"], threshold=0.6),
            _grade_length(r, 10, 60),
        ),
    },
    {
        "id": "C8",
        "name": "Key Points Extraction",
        "category": "summarization",
        "messages": [
            {"role": "user", "content": """Extract 3-5 bullet points from this text:

Remote work has transformed the modern workplace in several significant ways. First, it has eliminated commuting for millions of workers, saving an average of 40 minutes per day and reducing carbon emissions. Second, companies have been able to tap into global talent pools, hiring the best candidates regardless of location. Third, studies show that many remote workers report higher productivity due to fewer office distractions, though some struggle with isolation and work-life boundaries. Fourth, commercial real estate in major cities has been impacted, with office vacancy rates reaching historic highs. Finally, new industries have emerged around remote work tools, virtual collaboration platforms, and co-working spaces."""},
        ],
        "grade": lambda r: _grade_format(r, [
            ("has bullets", lambda t: t.count("- ") >= 3 or t.count("• ") >= 3 or t.count("* ") >= 3 or bool(re.search(r"\d\.", t))),
            ("mentions commuting", lambda t: "commut" in t.lower()),
            ("mentions productivity", lambda t: "productiv" in t.lower()),
            ("mentions real estate", lambda t: "real estate" in t.lower() or "office" in t.lower()),
            ("3-7 points", lambda t: 3 <= (t.count("- ") + t.count("• ") + t.count("* ") + len(re.findall(r"^\d\.", t, re.M))) <= 7),
        ]),
    },
    # ── Category 4: Instruction Following ──
    {
        "id": "C9",
        "name": "Format Constraint",
        "category": "instruction",
        "messages": [
            {"role": "user", "content": "List exactly 5 animals that live in the Arctic. Format each as 'N. Animal — one fact'. Do not include any other text."},
        ],
        "grade": lambda r: _grade_format(r, [
            ("has 5 items", lambda t: len(re.findall(r"^\d\.", t, re.M)) == 5),
            ("has dashes", lambda t: t.count("—") >= 4 or t.count("-") >= 8),
            ("no preamble", lambda t: not t.lower().startswith(("here", "sure", "of course", "certainly"))),
            ("mentions polar bear", lambda t: "polar bear" in t.lower()),
        ]),
    },
    {
        "id": "C10",
        "name": "Role Play",
        "category": "instruction",
        "messages": [
            {"role": "system", "content": "You are a grumpy medieval blacksmith named Bjorn. Stay in character at all times. Use period-appropriate language."},
            {"role": "user", "content": "Can you make me a sword?"},
        ],
        "grade": lambda r: _grade_keywords(r,
            ["sword", "steel", "iron", "forge", "blade", "smith", "hammer", "fire", "gold", "coin", "pay"],
            threshold=0.3),
    },
    {
        "id": "C11",
        "name": "Refusal Test",
        "category": "instruction",
        "messages": [
            {"role": "user", "content": "What is the capital of the fictional country Genovia from 'The Princess Diaries'?"},
        ],
        "grade": lambda r: _grade_any_phrase(r, ["genovia", "fictional", "princess diaries", "movie", "film", "anne hathaway"]),
    },
    {
        "id": "C12",
        "name": "Multi-Constraint",
        "category": "instruction",
        "messages": [
            {"role": "user", "content": "Write a haiku about the ocean. Remember: 5-7-5 syllables. Output only the haiku, nothing else."},
        ],
        "grade": lambda r: _grade_format(r, [
            ("3 lines", lambda t: len([l for l in t.strip().split("\n") if l.strip()]) == 3),
            ("ocean theme", lambda t: any(w in t.lower() for w in ["ocean", "sea", "wave", "tide", "shore", "salt", "deep", "water", "surf"])),
            ("short", lambda t: len(t.split()) <= 25),
            ("no preamble", lambda t: not t.lower().startswith(("here", "sure", "of course"))),
        ]),
    },
    # ── Category 5: Creative ──
    {
        "id": "C13",
        "name": "Story Opening",
        "category": "creative",
        "messages": [
            {"role": "user", "content": "Write a 3-sentence opening to a mystery story set in a lighthouse."},
        ],
        "grade": lambda r: (
            lambda s1, s2: (min(s1[0] * 0.5 + s2[0] * 0.5, 1.0), f"{s1[1]}; {s2[1]}")
        )(
            _grade_keywords(r, ["lighthouse", "light", "storm", "night", "dark", "sea", "cliff", "fog", "mystery", "door", "shadow", "keeper"], threshold=0.15),
            _grade_length(r, 20, 120),
        ),
    },
    {
        "id": "C14",
        "name": "Analogy Generation",
        "category": "creative",
        "messages": [
            {"role": "user", "content": "Explain how a computer's CPU works using a restaurant kitchen as an analogy. Keep it under 100 words."},
        ],
        "grade": lambda r: (
            lambda s1, s2: (min(s1[0] * 0.6 + s2[0] * 0.4, 1.0), f"{s1[1]}; {s2[1]}")
        )(
            _grade_keywords(r, ["cpu", "chef", "cook", "kitchen", "order", "recipe", "instruction"], threshold=0.3),
            _grade_length(r, 30, 150),
        ),
    },
    # ── Category 6: Nuanced / Hard ──
    {
        "id": "C15",
        "name": "Ethical Dilemma",
        "category": "nuanced",
        "messages": [
            {"role": "user", "content": "Is it ever okay to lie? Give a balanced answer considering different perspectives, in under 100 words."},
        ],
        "grade": lambda r: (
            lambda s1, s2: (min(s1[0] * 0.6 + s2[0] * 0.4, 1.0), f"{s1[1]}; {s2[1]}")
        )(
            _grade_keywords(r, ["harm", "protect", "trust", "honesty", "moral", "white lie", "context", "intention", "consequence"], threshold=0.2),
            _grade_length(r, 30, 150),
        ),
    },
    {
        "id": "C16",
        "name": "Ambiguous Question",
        "category": "nuanced",
        "messages": [
            {"role": "user", "content": "How many sides does a circle have?"},
        ],
        "grade": lambda r: _grade_any_phrase(r, ["one", "1", "zero", "0", "none", "infinite", "no sides", "depends", "curved"]),
    },
]


# ── Runner ───────────────────────────────────────────────────────────────

def warmup(model_id):
    config = MODELS[model_id]
    if config.get("type") == "claude":
        return
    print(f"  Warming {config['label']}...", end="", flush=True)
    try:
        call_ollama(config, [{"role": "user", "content": "hi"}], max_tokens=1)
        print(" ready")
    except Exception:
        print(" failed")


def run_benchmark(model_ids, test_ids, runs):
    tests = TESTS if not test_ids else [t for t in TESTS if t["id"] in test_ids]
    results = []

    for mid in model_ids:
        config = MODELS[mid]
        print(f"\n{'='*60}")
        print(f"  Model: {config['label']} ({mid})")
        print(f"{'='*60}")
        warmup(mid)

        for test in tests:
            for run in range(1, runs + 1):
                resp = call_model(config, test["messages"])
                score, reason = test["grade"](resp)
                passed = score >= 0.6
                tag = "PASS" if passed else "FAIL"
                tps = f"{resp['tokens_per_sec']} t/s" if resp["tokens_per_sec"] else "—"
                print(f"  [{test['id']}/R{run}] {tag} ({resp['elapsed_ms']}ms, {tps}) — {reason[:60]}")

                if config.get("type") == "claude":
                    time.sleep(0.5)

                results.append({
                    "test_id": test["id"],
                    "test_name": test["name"],
                    "category": test["category"],
                    "model": mid,
                    "run": run,
                    "elapsed_ms": resp["elapsed_ms"],
                    "output_tokens": resp["output_tokens"],
                    "tokens_per_sec": resp["tokens_per_sec"],
                    "passed": passed,
                    "score": score,
                    "reason": reason,
                    "content_preview": (resp.get("content") or "")[:200],
                })
    return results


def print_summary(results, model_ids):
    categories = ["knowledge", "reasoning", "summarization", "instruction", "creative", "nuanced"]
    cat_short = {"knowledge": "Know", "reasoning": "Reas", "summarization": "Summ",
                 "instruction": "Inst", "creative": "Crea", "nuanced": "Nuanc"}

    print(f"\n{'='*80}")
    print("  CHAT BENCHMARK — RESULTS")
    print(f"{'='*80}\n")

    header = f"  {'Model':<22} {'Pass':>6} {'Avg ms':>8} {'Avg t/s':>9}"
    for cat in categories:
        header += f" {cat_short[cat]:>6}"
    print(header)
    print(f"  {'─'*22} {'─'*6} {'─'*8} {'─'*9}" + " ".join(f"{'─'*6}" for _ in categories))

    for mid in model_ids:
        mr = [r for r in results if r["model"] == mid]
        if not mr:
            continue
        total = len(mr)
        passed = sum(1 for r in mr if r["passed"])
        avg_ms = int(sum(r["elapsed_ms"] for r in mr) / total) if total else 0
        tps_vals = [r["tokens_per_sec"] for r in mr if r["tokens_per_sec"]]
        avg_tps = f"{sum(tps_vals) / len(tps_vals):.0f}" if tps_vals else "—"

        row = f"  {MODELS[mid]['label']:<22} {passed}/{total:>3} {avg_ms:>7}ms {avg_tps:>8}"
        for cat in categories:
            cr = [r for r in mr if r["category"] == cat]
            cp = sum(1 for r in cr if r["passed"])
            row += f" {cp}/{len(cr):>3}"
        print(row)
    print()


def main():
    parser = argparse.ArgumentParser(description="Chat Benchmark")
    parser.add_argument("--models", default=",".join(MODELS.keys()))
    parser.add_argument("--tests", default="")
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--output", default="./results/chat")
    args = parser.parse_args()

    model_ids = [m.strip() for m in args.models.split(",")]
    test_ids = [t.strip() for t in args.tests.split(",") if t.strip()] or None

    for mid in model_ids:
        if mid not in MODELS:
            print(f"Unknown model: {mid}. Available: {list(MODELS.keys())}")
            sys.exit(1)

    print("Chat Benchmark")
    print(f"Models: {', '.join(MODELS[m]['label'] for m in model_ids)}")
    print(f"Tests: {test_ids or f'all {len(TESTS)}'} × {args.runs} runs each")

    results = run_benchmark(model_ids, test_ids, args.runs)
    print_summary(results, model_ids)

    import os
    os.makedirs(args.output, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(args.output, f"chat-results-{ts}.json")
    with open(path, "w") as f:
        json.dump({
            "metadata": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "models": {mid: MODELS[mid] for mid in model_ids},
                "runs_per_test": args.runs,
            },
            "results": results,
        }, f, indent=2, default=str)
    print(f"Results saved to: {path}")


if __name__ == "__main__":
    main()
