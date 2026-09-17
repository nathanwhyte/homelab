#!/usr/bin/env python3
"""Re-derive FIM contention statistics from a probe result directory.

Reads the RAW per-request samples that fim-contention-probe.py persists and
recomputes everything, so no conclusion depends on an order statistic chosen at
capture time. Takes one or more result directories:

    benchmarks/ollama/tools/analyze-fim-overlap.py benchmarks/results/fim-*/

Three views, in increasing order of how much they can be trusted:

1. `--per-sample` -- both phase classifications side by side. The probe's own
   window label, and an arrival-instant label computed against the tighter
   `[t_first - prompt_eval_s, t_first]` server-prefill window. They disagree,
   which is the point: the window version selects for long waits (a slow request
   is likelier to touch some prefill), the arrival version ignores load that
   starts during the wait. Neither identifies the cause of any single sample.
   Printed for diagnosis, never as a finding.

2. Default per-condition table -- percentiles recomputed from raw samples plus
   the INFO-1091 perceptual thresholds (~200-500 ms "feels instant", >1 s "feels
   broken"). Sound within a condition.

3. `--control` -- the between-condition comparison, which is the only view that
   supports a mechanism claim. Contrasts decode-pressure conditions (B/C, tiny
   prompts) against prefill-heavy ones (F/G/H) and reports aggregate background
   decode throughput alongside, so "more decode yet kinder to FIM" is visible.
"""

import argparse
import json
import math
import pathlib
import statistics
import sys


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, math.ceil(p * len(xs)) - 1))]


def bg_windows(cond, tight):
    """Background request windows. tight=True uses the server's prefill duration."""
    out = []
    for b in cond.get("bg") or []:
        for r in b.get("runs_raw") or []:
            t_first = r["t_first"]
            p0 = (
                max(r["t_start"], t_first - r.get("prompt_eval_s", 0))
                if tight
                else r["t_start"]
            )
            out.append((p0, t_first, r["t_end"]))
    return out


def classify_at(t, wins):
    if any(p0 <= t < p1 for p0, p1, _ in wins):
        return "prefill"
    if any(p1 <= t < e for _, p1, e in wins):
        return "decode"
    return "idle"


def group(samples, wins):
    g = {}
    for s in samples:
        g.setdefault(classify_at(s["t_start"], wins), []).append(s["ttft"])
    return g


def fmt(x):
    return "  -  " if x is None else f"{x:5.2f}"


def summarize(vals):
    return (len(vals), statistics.median(vals), max(vals)) if vals else (0, None, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+")
    ap.add_argument(
        "--per-sample", action="store_true", help="both phase labels (diagnostic only)"
    )
    ap.add_argument(
        "--control", action="store_true", help="decode-pressure vs prefill-heavy"
    )
    args = ap.parse_args()

    for d in args.dirs:
        for path in sorted(pathlib.Path(d).glob("*.json")):
            if path.name == "ollama-version.json":
                continue
            data = json.loads(path.read_text())
            conds = data.get("conditions") or {}
            if not conds:
                continue
            print(f"\n=== {path.parent.name}/{path.stem}  ({data.get('model')}) ===")
            print(
                "cond            n  | p50   p90   p95   max   | >0.5s >1s | bg decode tok/s  prefill_p50"
            )
            for label, c in conds.items():
                s = c.get("fim_samples") or []
                if not s:
                    continue
                t = [x["ttft"] for x in s]
                bgs = [b for b in (c.get("bg") or []) if b.get("runs")]
                tps = sum(b["decode_p50"] for b in bgs) if bgs else None
                pre = (
                    statistics.median([b["prefill_p50"] for b in bgs]) if bgs else None
                )
                flag = "" if c.get("valid", True) else "  INVALID"
                print(
                    f"{label:15s}{len(t):2d} | {fmt(statistics.median(t))} {fmt(q(t, 0.90))} "
                    f"{fmt(q(t, 0.95))} {fmt(max(t))} | "
                    f"{sum(x > 0.5 for x in t) / len(t):4.0%} {sum(x > 1.0 for x in t) / len(t):4.0%} | "
                    f"{fmt(tps)}          {fmt(pre)}{flag}"
                )
                if args.per_sample:
                    loose = group(s, bg_windows(c, tight=False))
                    tight = group(s, bg_windows(c, tight=True))
                    for name, g in (("window/loose", loose), ("arrival/tight", tight)):
                        parts = []
                        for ph in ("prefill", "decode", "idle"):
                            n, p50, mx = summarize(g.get(ph, []))
                            if n:
                                parts.append(f"{ph}: n={n} p50={p50:.2f} max={mx:.2f}")
                        print(f"      {name:14s} " + " | ".join(parts))
            if args.control:
                decode_like = [k for k in conds if k[0] in "BC"]
                prefill_like = [k for k in conds if k[0] in "FGH"]
                if decode_like and prefill_like:
                    print(
                        "\n  control: decode-pressure conditions vs prefill-heavy conditions.\n"
                        "  If a decode condition sustains comparable or higher aggregate tok/s\n"
                        "  while leaving FIM near idle, decode contention is not the driver."
                    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
