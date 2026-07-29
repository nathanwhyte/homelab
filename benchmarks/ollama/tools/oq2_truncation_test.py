"""OQ-2 follow-up: does a warm request whose INPUT exceeds the resident
context get silently truncated, or rejected with an error?

The original test used num_predict=4, so its `done_reason: length` was fully
explained by the output budget and said nothing about context handling.
"""

import json
import time
import urllib.request

M = "gemma4:12b-mlx"  # small/fast, same MLX runner path
BASE = "http://localhost:11434"


def post(path, payload, timeout=300):
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


stop()
print("start ps:", ps())

print("\n[A] COLD load at num_ctx=8192, short prompt, num_predict=32")
s, b = post("/api/generate", {
    "model": M, "prompt": "hi", "stream": False, "think": False,
    "keep_alive": "5m", "options": {"num_ctx": 8192, "num_predict": 32}})
print(f"   http={s} done_reason={b.get('done_reason')} "
      f"prompt_eval={b.get('prompt_eval_count')} eval={b.get('eval_count')} "
      f"error={b.get('error', 'none')}")
print("   ps:", ps())

# ~24k tokens of filler — far beyond the resident 8192
long_prompt = "The quick brown fox jumps over the lazy dog. " * 2600
print(f"\n[B] WARM request, input ~{len(long_prompt.split())} words "
      f"(>> resident 8192), num_ctx=65536, num_predict=32")
s, b = post("/api/generate", {
    "model": M, "prompt": long_prompt, "stream": False, "think": False,
    "keep_alive": "5m", "options": {"num_ctx": 65536, "num_predict": 32}})
print(f"   http={s}")
print(f"   done_reason={b.get('done_reason')}  error={json.dumps(b.get('error', 'none'))}")
print(f"   prompt_eval_count={b.get('prompt_eval_count')}  eval_count={b.get('eval_count')}")
print(f"   response={json.dumps((b.get('response') or '')[:100])}")
print("   ps:", ps())

stop()
print("\ncleaned up, ps:", ps())
