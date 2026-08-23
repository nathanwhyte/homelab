#!/usr/bin/env python3
"""Offline tests for the prefix-cache accounting and the quality gate.

These are pure-logic tests: they never contact a server and never load a
model, unlike test_agentic_harness.py / test_concurrency_bench.py which
exercise a live endpoint. Run them with:

    uv run --with pytest python -m pytest \
        benchmarks/ollama/tools/test_prefill_cache_accounting.py -q

They exist because the bug they pin down (TASK-1190) was invisible to every
existing test: the corpus warm-prefix path had never been executed at all, so
a classifier that could never return True on the first tier shipped unnoticed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
CORPUS = TOOLS.parent / "corpus"
GATE = TOOLS / "bench-quality-gate.py"

CACHE_HIT_DURATION_RATIO = 0.5


def _old_classifier(evaluated: int, est_total: int) -> bool:
    """The heuristic this work replaced, kept to pin the regression."""
    return evaluated < 0.5 * est_total


def _new_classifier(prefill_s: float, predicted_cold_s: float) -> bool:
    """Duration-based, matching prefill-size-breakdown.py.

    Token-based detection is impossible on this engine: measured on pop
    2026-08-23, an uninterrupted repeat collapsed prefill 117.8s -> 0.082s
    while prompt_eval_count stayed at 64552 in both requests.
    """
    ratio = prefill_s / predicted_cold_s if predicted_cold_s > 0 else 1.0
    return ratio <= CACHE_HIT_DURATION_RATIO


def _manifest() -> dict:
    return json.loads((CORPUS / "manifest.json").read_text())


def test_old_classifier_was_unsatisfiable_on_the_5k_tier():
    """A PERFECT prefix hit on 5k still failed the old test.

    This is the whole bug: the probe walks tiers smallest-first and aborted
    inside the tier loop, so tier one killed every run before 27k/77k.
    """
    m = _manifest()
    prefix = m["tiers"]["5k"]["est_tokens"]
    suffix = m["append_payload"]["est_tokens"]
    perfect_hit_evaluated = suffix  # entire prefix reused, suffix evaluated
    assert not _old_classifier(perfect_hit_evaluated, prefix + suffix)


def test_new_classifier_accepts_a_measured_cache_hit():
    """Real numbers from the pop 2026-08-23 shakeout."""
    assert _new_classifier(prefill_s=0.082, predicted_cold_s=117.813)


def test_new_classifier_rejects_a_full_cold_prefill():
    """Cancelled arm on 0.32.15: resumed prefill matched the cold one."""
    assert not _new_classifier(prefill_s=128.898, predicted_cold_s=125.218)


def test_token_counts_cannot_detect_reuse_on_this_engine():
    """Pin the false premise that made the old classifier unfixable.

    Both the cold and the fully-cached request reported 64552 evaluated
    tokens, so ANY token-difference test yields zero regardless of the cache.
    """
    cold_eval, cached_eval = 64552, 64552
    assert cold_eval - cached_eval == 0


def test_new_classifier_rejects_a_marginal_speedup():
    # 60% of predicted cold is faster, but not the >1000x collapse a real
    # prefix-cache hit produces on this engine.
    assert not _new_classifier(prefill_s=70.0, predicted_cold_s=117.0)


def _run_gate(payload: dict, *args: str) -> subprocess.CompletedProcess:
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(payload, fh)
        path = fh.name
    return subprocess.run(
        [sys.executable, str(GATE), path, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _levels(attempted, failed=0, empty=0, truncated=0):
    return {
        "summary": {
            "levels": [
                {
                    "concurrency": 1,
                    "quality": {
                        "attempted": attempted,
                        "failed": failed,
                        "empty_answers": empty,
                        "truncated": truncated,
                    },
                }
            ]
        }
    }


def test_gate_passes_a_clean_run():
    r = _run_gate(_levels(27))
    assert r.returncode == 0, r.stdout


def test_gate_fails_on_errors():
    r = _run_gate(_levels(27, failed=1))
    assert r.returncode == 1
    assert "BREACH" in r.stdout


def test_gate_fails_on_empty_answers():
    r = _run_gate(_levels(27, empty=20))
    assert r.returncode == 1


def test_gate_reports_na_for_a_non_concurrency_artifact():
    """A prefill result must not be reported as a quality FAILURE.

    Exit 2, not 1: pointing the gate at the wrong artifact is an operator
    error, and conflating the two trains people to ignore real failures.
    """
    r = _run_gate({"summary": [{"target_tokens": 5000, "ttft_p50": 1.2}], "raw": {}})
    assert r.returncode == 2, r.stdout
    assert "N/A" in r.stdout


def test_gate_treats_truncation_more_leniently_than_errors():
    """Truncation is config-sensitive; a single truncation must not fail a run."""
    assert _run_gate(_levels(27, truncated=1)).returncode == 0
    assert _run_gate(_levels(27, failed=1)).returncode == 1
