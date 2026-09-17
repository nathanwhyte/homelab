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
    """Phase at a single INSTANT -- no dependence on how long the wait lasted."""
    if any(p0 <= t < p1 for p0, p1, _ in wins):
        return "prefill"
    if any(p1 <= t < e for _, p1, e in wins):
        return "decode"
    return "idle"


def classify_window(t0, t1, wins):
    """Phase touched anywhere in the WHOLE wait [t0, t1].

    This is the length-biased view: a request slowed for any reason has a longer
    window and is likelier to touch some prefill. Kept so the two views can be
    compared, never as a finding on its own.
    """
    if t1 is None:
        return "unknown"
    if any(min(t1, p1) > max(t0, p0) for p0, p1, _ in wins):
        return "prefill"
    if any(min(t1, e) > max(t0, p1) for _, p1, e in wins):
        return "decode"
    return "idle"


def group(samples, wins, mode):
    """mode: 'arrival' classifies at t_start, 'window' over [t_start, t_first]."""
    g = {}
    for s in samples:
        if mode == "window":
            ph = classify_window(s["t_start"], s.get("t_first"), wins)
        else:
            ph = classify_at(s["t_start"], wins)
        g.setdefault(ph, []).append(s["ttft"])
    return g


def aggregate_tps(cond):
    """True background decode throughput: tokens completed / observation span.

    NOT the sum of per-worker median `eval_tps`. Those medians are per-request
    decode rates, measured only while a request is decoding, so they exclude the
    gaps between requests (prefill, queueing) and are not simultaneous
    observations -- summing them overstates what the runner actually sustained.
    """
    toks = 0
    t0, t1 = None, None
    for b in cond.get("bg") or []:
        for r in b.get("runs_raw") or []:
            toks += r.get("eval_count") or 0
            t0 = r["t_start"] if t0 is None else min(t0, r["t_start"])
            t1 = r["t_end"] if t1 is None else max(t1, r["t_end"])
    if not toks or t0 is None or t1 <= t0:
        return None
    return toks / (t1 - t0)


def union_len(spans):
    """Length covered by [start, end] spans, counting overlap ONCE."""
    merged, end = 0.0, None
    for s, e in sorted(spans):
        if end is None or s > end:
            merged += e - s
            end = e
        elif e > end:
            merged += e - end
            end = e
    return merged


def recompute_overlap(fim, runs):
    """Rebuild one sample's `overlap` block from raw timestamps.

    Mirrors fim-contention-probe.attribute(), including the merge: summing raw
    intersections double-counts concurrent background requests and can report a
    fraction above 1 before capping.
    """
    w0, w1 = fim["t_start"], fim.get("t_first")
    if w1 is None:
        return None
    span = max(1e-9, w1 - w0)
    pre, dec, n_p, n_d = [], [], 0, 0
    for r in runs:
        if min(w1, r["t_first"]) > max(w0, r["t_start"]):
            pre.append((max(w0, r["t_start"]), min(w1, r["t_first"])))
            n_p += 1
        if min(w1, r["t_end"]) > max(w0, r["t_first"]):
            dec.append((max(w0, r["t_first"]), min(w1, r["t_end"])))
            n_d += 1
    p_s, d_s = union_len(pre), union_len(dec)
    return {
        "phase": "prefill" if n_p else ("decode" if n_d else "idle"),
        "prefill_overlap_s": p_s,
        "decode_overlap_s": d_s,
        "prefill_frac": min(1.0, p_s / span),
        "n_bg_prefill": n_p,
        "n_bg_decode": n_d,
    }


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
    ap.add_argument(
        "--fix-derived",
        action="store_true",
        help="recompute each sample's `overlap` block from raw timestamps and "
        "rewrite the JSON in place. Only touches DERIVED fields; raw "
        "t_start/t_first/t_end and ttft are never modified. Use after a "
        "correction to the attribution maths on already-captured runs.",
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
            if args.fix_derived:
                changed = 0
                for c in conds.values():
                    runs = [
                        r
                        for b in (c.get("bg") or [])
                        for r in (b.get("runs_raw") or [])
                    ]
                    for s in c.get("fim_samples") or []:
                        new = recompute_overlap(s, runs)
                        if new != s.get("overlap"):
                            s["overlap"] = new
                            changed += 1
                    if c.get("fim_samples"):
                        by = {}
                        for s in c["fim_samples"]:
                            by.setdefault(
                                (s.get("overlap") or {}).get("phase", "unknown"), []
                            ).append(s["ttft"])
                        c["ttft_by_phase"] = {
                            ph: {
                                "n": len(v),
                                "p50": statistics.median(v),
                                "max": max(v),
                            }
                            for ph, v in sorted(by.items())
                        }
                path.write_text(json.dumps(data, indent=2) + "\n")
                print(
                    f"{path.parent.name}/{path.name}: rewrote {changed} overlap blocks"
                )
                continue
            print(f"\n=== {path.parent.name}/{path.stem}  ({data.get('model')}) ===")
            print(
                "cond            n  | p50   p90   p95   max   | >0.5s >1s | bg tok/s(agg)  prefill_p50"
            )
            for label, c in conds.items():
                s = c.get("fim_samples") or []
                if not s:
                    continue
                t = [x["ttft"] for x in s]
                bgs = [b for b in (c.get("bg") or []) if b.get("runs")]
                tps = aggregate_tps(c)
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
                    # Two axes, deliberately: WHEN the sample is classified
                    # (whole wait vs arrival instant) and WHICH background
                    # window is used (client time-to-first-chunk vs the server's
                    # own prompt_eval_duration).
                    loose = group(s, bg_windows(c, tight=False), "window")
                    tight = group(s, bg_windows(c, tight=True), "arrival")
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
