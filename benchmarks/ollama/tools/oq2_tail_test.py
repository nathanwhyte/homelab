"""Decisive: with a marker at the END of a prompt far exceeding the resident
context, can the model report it? If yes, the tail was in context and there was
no truncation. If it reports only head content, the prompt was truncated.
"""

import json
import time
import urllib.request

M = "gemma4:12b-mlx"
BASE = "http://localhost:11434"


def post(path, payload, timeout=600):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def ps():
    with urllib.request.urlopen(f"{BASE}/api/ps", timeout=30) as r:
        j = json.loads(r.read())
    return ",".join(f"{m['name']}@{m.get('context_length')}" for m in j.get("models", [])) or "(none)"


def stop():
    post("/api/generate", {"model": M, "keep_alive": 0})
    time.sleep(8)


HEAD_CODE = "ALPHA-7731"
TAIL_CODE = "OMEGA-4482"

stop()
print("start ps:", ps())

print("[A] COLD load at num_ctx=8192")
post("/api/generate", {"model": M, "prompt": "hi", "stream": False, "think": False,
                       "keep_alive": "5m", "options": {"num_ctx": 8192, "num_predict": 8}})
print("   ps:", ps())

filler = "The quick brown fox jumps over the lazy dog. " * 2600
prompt = (
    f"The HEAD code is {HEAD_CODE}. Remember it.\n\n{filler}\n\n"
    f"The TAIL code is {TAIL_CODE}.\n\n"
    "Reply with exactly two lines:\nHEAD=<the head code>\nTAIL=<the tail code>"
)

print(f"\n[B] WARM request, prompt far exceeds resident 8192")
s, b = post("/api/generate", {"model": M, "prompt": prompt, "stream": False, "think": False,
                              "keep_alive": "5m", "options": {"num_ctx": 65536, "num_predict": 64}})
resp = (b.get("response") or "").strip()
print(f"   http={s} done_reason={b.get('done_reason')} error={b.get('error', 'none')}")
print(f"   prompt_eval_count={b.get('prompt_eval_count')}")
print(f"   response={json.dumps(resp[:200])}")
print(f"   ps: {ps()}")
print()
print(f"   HEAD ({HEAD_CODE}) recalled: {HEAD_CODE in resp}")
print(f"   TAIL ({TAIL_CODE}) recalled: {TAIL_CODE in resp}")
print("   => TAIL present means the whole prompt was in context (NO truncation)")
print("   => TAIL absent but HEAD present means the tail was dropped (truncation)")

stop()
print("\ncleaned up, ps:", ps())
