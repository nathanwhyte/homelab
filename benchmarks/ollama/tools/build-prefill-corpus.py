#!/usr/bin/env python3
"""Assemble the real-payload prefill corpus for TASK-1186's prefill pass.

The corpus is the actual prefill use case: agent-session context ingestion.
Each tier concatenates real system-prompt-shaped files (CLAUDE.md, AGENTS.md,
on-demand instruction docs, opencode command prompts) into one prompt file at
a target token size matching a measured production profile:

  5k  — slim lclaude profile (user-global CLAUDE.md alone)
  27k — Claude Code structural floor (global + project instruction stack)
  77k — full turn-one profile per the OQ-3 scope boundary (2026-07-28)

Tiers nest: each larger tier starts with the smaller tier's sources in the
same order, so a warm-prefix run of a larger tier reuses the smaller tier's
cached prefix. Token counts are estimated at 4 chars/token; the benchmark
reports the tokenizer-true count per model (prompt_eval_count), so the
estimate only needs to land the tier in the right size class.

The assembled corpus is COMMITTED so pop and timmy run byte-identical tiers
(TASK-1186 acceptance criterion). Re-run this script only to rebuild the
corpus deliberately — that changes the measurement payload and breaks
comparability with prior runs; bump the manifest's built_at and say so in the
run notes.

Also emits append-payload.txt (a realistic mid-session file-read payload,
~6k tokens) used by prefill-size-breakdown.py --mode warm-prefix.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

HOME = Path.home()
CORPUS_DIR = Path(__file__).resolve().parents[1] / "corpus"
CHARS_PER_TOKEN = 4

# Ordered source lists. Later tiers extend earlier ones (nested prefixes).
TIER_5K = [
    HOME / ".claude/CLAUDE.md",
]
TIER_27K = TIER_5K + [
    HOME / "code/compendium/CLAUDE.md",
    HOME / "code/dotfiles/CLAUDE.md",
    HOME / "code/homelab/main/CLAUDE.md",
    HOME / "code/homelab/main/AGENTS.md",
    HOME / "code/dotfiles/opencode/AGENTS.md",
]
TIER_77K = TIER_27K + [
    HOME / ".claude/GUARDRAILS.md",
    HOME / ".claude/QWEN.md",
    HOME / ".claude/TOOL_USAGE.md",
    HOME / "code/compendium/.claude/instructions/vault-structure.md",
    HOME / "code/compendium/.claude/instructions/bugs.md",
    HOME / "code/compendium/.claude/instructions/features.md",
    HOME / "code/compendium/.claude/instructions/tasks.md",
    HOME / "code/compendium/.claude/instructions/improvements.md",
    HOME / "code/compendium/.claude/instructions/projects.md",
    HOME / "code/compendium/.claude/instructions/info.md",
    HOME / "code/compendium/.claude/instructions/guides.md",
    HOME / "code/compendium/.claude/instructions/errors.md",
    HOME / "code/dotfiles/opencode/commands/compendium.md",
    HOME / "code/dotfiles/opencode/commands/compendium-compact.md",
    HOME / "code/dotfiles/opencode/commands/compendium-complete.md",
    HOME / "code/dotfiles/opencode/commands/compendium-lint.md",
]
APPEND_SOURCES = [
    HOME / "code/compendium/guides/tooling/GUIDE-021-compendium-authoring-standard.md",
    HOME
    / "code/compendium/guides/tooling/GUIDE-022-compendium-shipping-skill-changes-end-to-end.md",
]

TIERS = {"5k": (TIER_5K, 5_000), "27k": (TIER_27K, 27_000), "77k": (TIER_77K, 77_000)}


def concat(sources: list[Path]) -> tuple[str, list[dict]]:
    parts, manifest = [], []
    for src in sources:
        if not src.is_file():
            print(f"missing source: {src}", file=sys.stderr)
            raise SystemExit(1)
        text = src.read_text()
        parts.append(f"# ==== FILE: {src} ====\n\n{text}")
        manifest.append(
            {
                "path": str(src),
                "bytes": len(text.encode()),
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        )
    return "\n\n".join(parts), manifest


def trim_to_tokens(text: str, target_tokens: int) -> str:
    limit = target_tokens * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text
    # Cut at a line boundary so the payload stays well-formed markdown-ish.
    return text[:limit].rsplit("\n", 1)[0] + "\n"


def main() -> int:
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "chars_per_token_estimate": CHARS_PER_TOKEN,
        "tiers": {},
    }
    for name, (sources, target) in TIERS.items():
        text, srcs = concat(sources)
        text = trim_to_tokens(text, target)
        out = CORPUS_DIR / f"prefill-corpus-{name}.txt"
        out.write_text(text)
        est = len(text) // CHARS_PER_TOKEN
        manifest["tiers"][name] = {
            "file": out.name,
            "target_tokens": target,
            "est_tokens": est,
            "bytes": len(text.encode()),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "sources": srcs,
        }
        print(
            f"{out.name}: {len(text.encode())} bytes ~{est} tokens ({len(srcs)} sources)"
        )

    text, srcs = concat(APPEND_SOURCES)
    out = CORPUS_DIR / "append-payload.txt"
    out.write_text(text)
    manifest["append_payload"] = {
        "file": out.name,
        "est_tokens": len(text) // CHARS_PER_TOKEN,
        "bytes": len(text.encode()),
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "sources": srcs,
    }
    print(
        f"{out.name}: {len(text.encode())} bytes ~{len(text) // CHARS_PER_TOKEN} tokens"
    )

    (CORPUS_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"manifest -> {CORPUS_DIR / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
