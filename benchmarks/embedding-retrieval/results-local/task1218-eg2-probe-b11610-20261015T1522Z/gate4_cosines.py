"""Gate 4: re-embed a probe window's exact inputs on pop's reference server, compare cosines."""

import json
import math
import statistics
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:18093"


def post(path, body, timeout=300):
    req = urllib.request.Request(
        BASE + path, json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def wait_healthy(limit=180):
    end = time.time() + limit
    while time.time() < end:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=5) as r:
                if json.load(r).get("status") == "ok":
                    return
        except Exception:
            pass
        time.sleep(2)
    sys.exit("reference server not healthy")


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def main(vectors_path, out_path):
    wait_healthy()
    rows = [json.loads(line) for line in open(vectors_path) if line.strip()]
    seen, results = {}, []
    for row in rows:
        key = json.dumps(row["input"])
        if key not in seen:
            resp = post("/v1/embeddings", {"model": "ref", "input": [row["input"]]})
            seen[key] = (
                resp["data"][0]["embedding"],
                (resp.get("usage") or {}).get("prompt_tokens"),
            )
        ref, ref_tokens = seen[key]
        if len(ref) != len(row["vector"]):
            sys.exit(f"{row['key']}: dimension {len(ref)} vs {len(row['vector'])}")
        results.append(
            {
                "key": row["key"],
                "tokens": row["tokens"],
                "ref_tokens": ref_tokens,
                "cosine": cosine(ref, row["vector"]),
            }
        )
    cos = [r["cosine"] for r in results]
    worst = sorted(results, key=lambda r: r["cosine"])[:5]
    summary = {
        "min": min(cos),
        "median": statistics.median(cos),
        "n": len(cos),
        "unique_inputs": len(seen),
        "token_mismatches": sum(1 for r in results if r["ref_tokens"] != r["tokens"]),
        "worst": worst,
    }
    json.dump(summary, open(out_path, "w"), indent=2)
    print(json.dumps({k: v for k, v in summary.items() if k != "worst"}))
    for r in worst:
        print(r["key"], r["tokens"], round(r["cosine"], 6))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
