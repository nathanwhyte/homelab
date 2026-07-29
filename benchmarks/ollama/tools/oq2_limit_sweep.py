"""OQ-2 closing test: what is the *effective* context limit on the MLX runner,
and what happens when a prompt exceeds it — error, truncation, or silent
quality loss?

`oq2_tail_test.py` established that a 26k-token prompt is processed in full by a
runner resident at 8192, so `/api/ps`'s `context_length` is not an enforced cap.
It did not establish where the real ceiling is.

This sweeps prompt size upward against a runner deliberately kept resident at a
small `num_ctx`, with markers at the HEAD, MIDDLE and TAIL of every prompt. For
each size it records:

  * whether the request errored (HTTP status / `error` field)
  * `prompt_eval_count` vs the number of tokens actually sent (a shortfall is
    truncation, whether or not it is reported)
  * which of the three markers came back (recall is the quality signal — a
    dropped marker with no error is silent degradation)
  * whether the runner survived (`/api/ps` still lists it)

The model's architectural context (`gemma4_unified.context_length` = 262144) is
crossed deliberately by the last step, which is the point where an
architecture-level limit, if enforced anywhere, has to show up.

Run with the server idle: `python3 oq2_limit_sweep.py`.
"""

import json
import time
import urllib.error
import urllib.request

M = "gemma4:12b-mlx"
BASE = "http://localhost:11434"
RESIDENT_CTX = 8192
ARCH_CTX = 262144

# Approximate token counts. The filler sentence measured 10.02 tok/repetition
# against `prompt_eval_count` in oq2_tail_test.py, so reps = target / 10.
TARGETS = [13_000, 26_000, 52_000, 104_000, 200_000, 300_000]

FILLER = "The quick brown fox jumps over the lazy dog. "
TOK_PER_REP = 10.02

HEAD_CODE = "ALPHA-7731"
MID_CODE = "SIGMA-5150"
TAIL_CODE = "OMEGA-4482"


def post(path, payload, timeout=1800):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read() or b"{}"
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"error": body.decode(errors="replace")[:400]}
    except Exception as e:  # noqa: BLE001 - connection reset = runner died mid-request
        return 0, {"error": f"{type(e).__name__}: {e}"}


def ps():
    try:
        with urllib.request.urlopen(f"{BASE}/api/ps", timeout=30) as r:
            j = json.loads(r.read())
    except Exception as e:  # noqa: BLE001 - any failure here means the runner is gone
        return f"(ps failed: {type(e).__name__})"
    models = j.get("models", [])
    return (
        ",".join(f"{m['name']}@{m.get('context_length')}" for m in models) or "(none)"
    )


def stop():
    post("/api/generate", {"model": M, "keep_alive": 0}, timeout=120)
    time.sleep(8)


def build_prompt(target_tokens):
    reps = int(target_tokens / TOK_PER_REP)
    half = reps // 2
    return (
        f"The HEAD code is {HEAD_CODE}. Remember it.\n\n"
        f"{FILLER * half}\n\nThe MIDDLE code is {MID_CODE}.\n\n{FILLER * (reps - half)}\n\n"
        f"The TAIL code is {TAIL_CODE}.\n\n"
        "Reply with exactly three lines:\n"
        "HEAD=<the head code>\nMIDDLE=<the middle code>\nTAIL=<the tail code>"
    )


def main():
    stop()
    print(f"model={M} resident_ctx={RESIDENT_CTX} arch_ctx={ARCH_CTX}")
    print("start ps:", ps())

    print(f"\n[cold load] num_ctx={RESIDENT_CTX}")
    post(
        "/api/generate",
        {
            "model": M,
            "prompt": "hi",
            "stream": False,
            "think": False,
            "keep_alive": "30m",
            "options": {"num_ctx": RESIDENT_CTX, "num_predict": 8},
        },
        timeout=300,
    )
    print("   ps:", ps())

    rows = []
    for target in TARGETS:
        prompt = build_prompt(target)
        print(
            f"\n[warm] target ~{target:,} tok (arch_ctx exceeded: {target > ARCH_CTX})"
        )
        t0 = time.time()
        status, body = post(
            "/api/generate",
            {
                "model": M,
                "prompt": prompt,
                "stream": False,
                "think": False,
                "keep_alive": "30m",
                # Left at the resident value on purpose: a warm runner ignores it
                # (established by oq2_truncation_test.py), so this isolates prompt
                # size as the only variable.
                "options": {"num_ctx": RESIDENT_CTX, "num_predict": 64},
            },
        )
        elapsed = time.time() - t0
        resp = (body.get("response") or "").strip()
        evaluated = body.get("prompt_eval_count")
        row = {
            "target": target,
            "http": status,
            "done_reason": body.get("done_reason"),
            "error": body.get("error"),
            "prompt_eval_count": evaluated,
            "elapsed_s": round(elapsed, 1),
            "head": HEAD_CODE in resp,
            "mid": MID_CODE in resp,
            "tail": TAIL_CODE in resp,
            "ps": ps(),
        }
        rows.append(row)
        print(
            f"   http={status} done_reason={row['done_reason']} "
            f"error={row['error'] or 'none'} eval={evaluated} in {row['elapsed_s']}s"
        )
        print(f"   recall head={row['head']} mid={row['mid']} tail={row['tail']}")
        print(f"   response={json.dumps(resp[:160])}")
        print(f"   ps: {row['ps']}")

        if status != 200 or row["ps"].startswith("(") or "@" not in row["ps"]:
            print("   -> request failed or runner gone; stopping the sweep here")
            break

    print("\n=== summary ===")
    print(
        f"{'target':>9} {'evaluated':>10} {'http':>5} {'done':>8} {'head':>5} {'mid':>5} {'tail':>5} {'s':>7}"
    )
    for r in rows:
        print(
            f"{r['target']:>9,} {r['prompt_eval_count']!s:>10} {r['http']:>5} "
            f"{r['done_reason']!s:>8} {r['head']!s:>5} {r['mid']!s:>5} "
            f"{r['tail']!s:>5} {r['elapsed_s']:>7}"
        )

    stop()
    print("\ncleaned up, ps:", ps())


if __name__ == "__main__":
    main()
