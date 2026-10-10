"""Score the 90-case batch gate per configuration: lane passes, median latency, paired exact McNemar.

Usage: python3 score.py <reference config> [other configs...]
Each configuration is compared case by case with the reference, with and without the two
cases homelab#193 marks ambiguous.
"""

import glob
import json
import os
import statistics
import sys
from math import comb

T = os.path.expanduser("~/code/moe-offload-trial")
LANES = ["summary", "fence", "blocker", "staleness", "compaction"]
AMBIGUOUS = {"IDEA-1027->PROJ-1018#0", "BUG-152"}


def load(cfg):
    rows = {}
    for lane in LANES:
        paths = glob.glob(f"{T}/shadow/{cfg}-*/{lane}-results.json")
        if len(paths) != 1:
            sys.exit(f"{cfg} {lane}: expected one results file, found {paths}")
        with open(paths[0]) as f:
            for r in json.load(f):
                rows[(lane, r["id"])] = r
    return rows


def mcnemar(b, c):
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2**n)


def main():
    ref, *others = sys.argv[1:]
    data = {cfg: load(cfg) for cfg in [ref, *others]}
    keys = sorted(data[ref])
    for cfg, rows in data.items():
        if sorted(rows) != keys:
            sys.exit(f"{cfg}: case set differs from {ref}")
    print(
        "| config | "
        + " | ".join(LANES)
        + " | total | excl. ambiguous | median s/case | json fail |"
    )
    print("|" + " --- |" * (len(LANES) + 5))
    for cfg, rows in data.items():
        lane = [sum(rows[k]["pass"] for k in keys if k[0] == ln) for ln in LANES]
        n_lane = [sum(1 for k in keys if k[0] == ln) for ln in LANES]
        total = sum(rows[k]["pass"] for k in keys)
        excl = [k for k in keys if k[1] not in AMBIGUOUS]
        walls = [rows[k]["wall"] for k in keys if rows[k].get("wall") is not None]
        print(
            f"| {cfg} | "
            + " | ".join(f"{p}/{n}" for p, n in zip(lane, n_lane))
            + f" | {total}/{len(keys)} | {sum(rows[k]['pass'] for k in excl)}/{len(excl)}"
            + f" | {statistics.median(walls):.1f} | {sum(not rows[k].get('json_ok', True) for k in keys)} |"
        )
    print()
    print(
        f"| {ref} vs | only {ref} right | only other right | exact p | p excl. ambiguous |"
    )
    print("| --- | --- | --- | --- | --- |")
    for cfg in others:
        res = []
        for subset in (keys, [k for k in keys if k[1] not in AMBIGUOUS]):
            b = sum(data[ref][k]["pass"] and not data[cfg][k]["pass"] for k in subset)
            c = sum(data[cfg][k]["pass"] and not data[ref][k]["pass"] for k in subset)
            res.append((b, c, mcnemar(b, c)))
        print(
            f"| {cfg} | {res[0][0]} | {res[0][1]} | {res[0][2]:.3f} | {res[1][2]:.3f} |"
        )
    print()
    print(
        f"{ref} misses: "
        + ", ".join(f"{k[0]}:{k[1]}" for k in keys if not data[ref][k]["pass"])
    )


if __name__ == "__main__":
    main()
