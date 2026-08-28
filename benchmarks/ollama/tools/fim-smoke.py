#!/usr/bin/env python3
"""Eight-case FIM correctness smoke for an Ollama FIM tag (accept-substring).

A lighter re-implementation of the INFO-1090 12-case re-score (2026-07-08,
pop): constrained cross-language holes, one at a time, temperature 0,
`/v1/completions` with `suffix` (the library template's `{{ .Suffix }}`
branch, i.e. the same path Minuet uses), salted prefix so the prefix cache
never replays. A case passes when the completion contains the accept
substring and no FIM marker leaked. Prints a per-case line and N/8.

Usage: fim-smoke.py <model> [host]   (env OLLAMA_HOST honoured)
"""

import json
import os
import sys
import time
import urllib.request

HOST = (
    sys.argv[2]
    if len(sys.argv) > 2
    else os.environ.get("OLLAMA_HOST", "http://192.168.1.19:11434")
).rstrip("/")
MODEL = sys.argv[1]

CASES = [
    (
        "python-sum",
        'def total(xs):\n    """Return the sum of xs."""\n    ',
        "\n\nprint(total([1, 2, 3]))\n",
        ["sum(xs)", "return"],
    ),
    (
        "python-loop",
        "def count_even(nums):\n    n = 0\n    for x in nums:\n        ",
        "\n    return n\n",
        ["% 2", "n += 1"],
    ),
    (
        "lua-max",
        "local function max(a, b)\n  ",
        "\nend\n\nprint(max(2, 5))\n",
        ["a > b", "a >= b", "math.max"],
    ),
    (
        "bash-loop",
        "#!/usr/bin/env bash\nfor f in *.log; do\n  ",
        "\ndone\n",
        ["$f", "${f}"],
    ),
    (
        "js-filter",
        "const evens = numbers.filter(",
        ");\nconsole.log(evens);\n",
        ["% 2", "=>"],
    ),
    (
        "sql-where",
        "SELECT id, name FROM users\nWHERE ",
        "\nORDER BY name;\n",
        ["=", "IS", "LIKE", ">", "<"],
    ),
    (
        "yaml-key",
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: ollama\n",
        "  type: LoadBalancer\n  ports:\n    - port: 11434\n",
        ["spec:"],
    ),
    (
        "python-dict",
        'config = {\n    "host": "localhost",\n    ',
        '\n}\nprint(config["port"])\n',
        ["port"],
    ),
]

MARKERS = [
    "<|fim",
    "<｜fim",
    "<fim_",
    "<PRE>",
    "<SUF>",
    "<MID>",
    "[PREFIX]",
    "[SUFFIX]",
    "[MIDDLE]",
    "<|file_sep",
    "<|endoftext|>",
]


def complete(prefix, suffix):
    body = {
        "model": MODEL,
        "prompt": f"# {time.time_ns()}\n" + prefix,
        "suffix": suffix,
        "max_tokens": 64,
        "temperature": 0,
        "stream": False,
    }
    req = urllib.request.Request(
        HOST + "/v1/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    r = json.loads(urllib.request.urlopen(req, timeout=120).read())
    return r["choices"][0]["text"], time.perf_counter() - t0


def main():
    ver = json.loads(urllib.request.urlopen(HOST + "/api/version", timeout=10).read())[
        "version"
    ]
    print(f"model={MODEL} ollama={ver}")
    passed = 0
    for name, prefix, suffix, accept in CASES:
        text, wall = complete(prefix, suffix)
        leaked = [m for m in MARKERS if m in text]
        ok = any(a in text for a in accept) and not leaked
        passed += ok
        print(
            f"  {name:12s} {'pass' if ok else 'FAIL'} {wall:.2f}s | {text.strip()[:70]!r}{' LEAK ' + str(leaked) if leaked else ''}"
        )
    print(f"{MODEL}: {passed}/{len(CASES)}")


if __name__ == "__main__":
    main()
