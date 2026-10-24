"""Capture and compare OpenViking search baselines across an upgrade (IMPR-1412).

`capture` sends every question in the frozen 38-query ground truth
(benchmarks/embedding-retrieval/eval_groundtruth_2026-07-04.json) to
/api/v1/search/find the way the MCP shim's viking_find sends it: `query` and
`target_uri` only, no `limit` and no `score_threshold`, so the server's defaults
apply on both sides. It records every hit (section, URI, raw score) through a
kubectl port-forward to the named Service, with the root key from the
`openviking-api-key` Secret. Search is read-only, though each request adds a
`request_audit` row.

`compare` pairs the hits two captures share, per query and URI, and checks each
new score against a mapping of the old one:

  * `half` (the default, v0.4.20 -> v0.5.x): new = (old + 1) / 2, the cosine
    [-1,1] -> [0,1] rescale. The formula is accepted when every query's top-3 URIs
    match and the largest absolute deviation is <= --tolerance (0.01, int8
    quantization jitter). Otherwise each floor is set from the measured mapping:
    the median new score of shared hits whose old score lies within --window
    (0.02) of that old floor.
  * `identity` (same release, e.g. before and after a restore-based rollback):
    new = old, and the top-3 URIs must match.

It prints the floor table and exits 0 when it can state every floor (formula or
measured) and, for `identity`, when top-3 match everywhere; 1 when an `identity`
check fails; 2 when the formula is rejected and some floor has no shared hit in
its window to measure from.

Usage:
  uv run --no-project --with httpx python ov-search-baseline.py capture --svc openviking --out base.json
  uv run --no-project python ov-search-baseline.py compare base.json new.json --floors 0.45,0.50,0.65
"""

import argparse
import base64
import json
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

NS = "viking"
GROUND_TRUTH = (
    Path(__file__).resolve().parents[2]
    / "benchmarks/embedding-retrieval/eval_groundtruth_2026-07-04.json"
)
SECTIONS = ("resources", "memories", "skills")


def top3(hits: list[dict]) -> list[str]:
    return [h["uri"] for h in sorted(hits, key=lambda h: -h["score"])[:3]]


def capture(svc: str, out: Path, target_uri: str, port: int) -> int:
    import httpx

    questions = json.loads(GROUND_TRUTH.read_text())["questions"]
    key = base64.b64decode(
        subprocess.run(
            [
                "kubectl",
                "-n",
                NS,
                "get",
                "secret",
                "openviking-api-key",
                "-o",
                "jsonpath={.data.api-key}",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    ).decode()
    pf = subprocess.Popen(
        ["kubectl", "-n", NS, "port-forward", f"svc/{svc}", f"{port}:1933"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(3)
        headers = {
            "X-API-Key": key,
            "X-OpenViking-Account": "default",
            "X-OpenViking-User": "noot",
        }
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", headers=headers, timeout=120
        ) as c:
            health = c.get("/health").json()
            queries = {}
            for q in questions:
                r = c.post(
                    "/api/v1/search/find",
                    json={"query": q["question"], "target_uri": target_uri},
                )
                r.raise_for_status()
                res = r.json().get("result", {})
                hits = [
                    {"section": s, "uri": it["uri"], "score": float(it.get("score", 0))}
                    for s in SECTIONS
                    for it in res.get(s, [])
                ]
                queries[q["qid"]] = {
                    "question": q["question"],
                    "hits": hits,
                    "top3": top3(hits),
                }
        out.write_text(
            json.dumps(
                {
                    "captured": datetime.now(UTC).isoformat(),
                    "svc": svc,
                    "health": health,
                    "target_uri": target_uri,
                    "queries": queries,
                },
                indent=2,
            )
            + "\n"
        )
        print(
            f"captured {len(queries)} queries from svc/{svc} ({health.get('version')}) -> {out}"
        )
        return 0
    finally:
        pf.terminate()


def compare(
    old: dict,
    new: dict,
    floors: list[float],
    mapping: str,
    tolerance: float,
    window: float,
) -> tuple[int, str]:
    pairs, top3_diff = [], []
    for qid, oq in old["queries"].items():
        nq = new["queries"].get(qid)
        if nq is None:
            top3_diff.append(qid)
            continue
        if oq["top3"] != nq["top3"]:
            top3_diff.append(qid)
        nscore = {h["uri"]: h["score"] for h in nq["hits"]}
        pairs += [
            (h["score"], nscore[h["uri"]]) for h in oq["hits"] if h["uri"] in nscore
        ]
    f = (lambda o: (o + 1) / 2) if mapping == "half" else (lambda o: o)
    max_dev = max((abs(n - f(o)) for o, n in pairs), default=float("inf"))
    lines = [
        f"queries {len(old['queries'])}, shared hits {len(pairs)}, mapping {mapping}",
        f"max |new - f(old)| = {max_dev:.4f} (tolerance {tolerance})",
        f"top-3 differs on {len(top3_diff)} queries"
        + (f": {', '.join(top3_diff)}" if top3_diff else ""),
    ]
    accepted = bool(pairs) and not top3_diff and max_dev <= tolerance
    if mapping == "identity":
        lines.append("RESULT: " + ("PASS" if accepted else "FAIL"))
        return (0 if accepted else 1), "\n".join(lines)
    lines.append(
        f"formula (old+1)/2: {'ACCEPTED' if accepted else 'REJECTED, floors measured'}"
    )
    lines += [
        "",
        "| old floor | formula | measured median | n in ±window | use |",
        "| --- | --- | --- | --- | --- |",
    ]
    code = 0
    for fl in floors:
        near = [n for o, n in pairs if abs(o - fl) <= window]
        med = statistics.median(near) if near else None
        use = f(fl) if accepted else med
        if use is None:
            code = 2
        lines.append(
            f"| {fl:.3f} | {f(fl):.3f} | {'—' if med is None else f'{med:.3f}'} | {len(near)} | "
            f"{'NO DATA' if use is None else f'{use:.3f}'} |"
        )
    return code, "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--svc", required=True, choices=["openviking", "openviking-test"])
    c.add_argument("--out", required=True, type=Path)
    c.add_argument("--target-uri", default="viking://")
    c.add_argument("--port", type=int, default=19336)
    m = sub.add_parser("compare")
    m.add_argument("old", type=Path)
    m.add_argument("new", type=Path)
    m.add_argument("--floors", default="0.45,0.50,0.65")
    m.add_argument("--mapping", choices=["half", "identity"], default="half")
    m.add_argument("--tolerance", type=float, default=0.01)
    m.add_argument("--window", type=float, default=0.02)
    a = ap.parse_args()
    if a.cmd == "capture":
        return capture(a.svc, a.out, a.target_uri, a.port)
    code, report = compare(
        json.loads(a.old.read_text()),
        json.loads(a.new.read_text()),
        [float(x) for x in a.floors.split(",")],
        a.mapping,
        a.tolerance,
        a.window,
    )
    print(report)
    return code


if __name__ == "__main__":
    sys.exit(main())
