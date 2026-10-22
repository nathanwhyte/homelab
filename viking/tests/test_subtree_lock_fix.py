#!/usr/bin/env python3
"""Reproducer for upstream GH #2015 — SUBTREE lock held through semantic refresh.

Two concurrent `ov add-resource` writes to the same parent directory:

- v0.3.14 (broken): the second write blocks behind the first's semantic
  refresh (minutes, VLM-latency-bound) or fails with `400 resource is busy`.
- v0.4.x (fixed): both writes complete within seconds of each other.

Pass criteria: both exit 0 and |d1 - d2| < 5s.
Fail criteria: either write fails, or one takes > 60s longer than the other.

Uses whatever `ov` CLI config is active (OPENVIKING_URL / config file).
Cleans up the scratch subtree afterwards.
"""

import concurrent.futures
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PARENT = "viking://resources/scratch/lockfix-test"


def run(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ov", *args], capture_output=True, text=True, timeout=timeout
    )


def timed_add(local_path: Path, target_uri: str) -> tuple[float, int, str]:
    t0 = time.monotonic()
    proc = run(["add-resource", str(local_path), "--to", target_uri])
    return time.monotonic() - t0, proc.returncode, (proc.stderr or proc.stdout).strip()


def main() -> int:
    run(["mkdir", PARENT])

    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for i in (1, 2):
            p = Path(tmp) / f"lockfix-child{i}.md"
            p.write_text(
                f"# Lock-fix reproducer child {i}\n\n"
                f"Concurrent-write probe for GH #2015 verification (IMPR-1007 Phase 3).\n"
            )
            files.append(p)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            futures = [
                ex.submit(timed_add, f, f"{PARENT}/child{i}.md")
                for i, f in enumerate(files, start=1)
            ]
            results = [f.result() for f in futures]

    for i, (dur, code, tail) in enumerate(results, start=1):
        status = "ok" if code == 0 else f"FAILED rc={code}"
        print(f"write {i}: {dur:6.1f}s  {status}")
        if code != 0:
            print(f"  output: {tail[-300:]}")

    run(["wait"], timeout=600)
    cleanup = run(["rm", PARENT, "--recursive"])
    print(f"cleanup: rm {PARENT} -> rc={cleanup.returncode}")

    (d1, c1, _), (d2, c2, _) = results
    skew = abs(d1 - d2)
    print(f"skew: {skew:.1f}s")

    if c1 != 0 or c2 != 0:
        print("RESULT: FAIL — a concurrent write errored (busy?)")
        return 1
    if skew > 60:
        print("RESULT: FAIL — writes serialized (lock still held through refresh)")
        return 1
    if skew > 5:
        print("RESULT: MARGINAL — writes overlapped but skew > 5s; inspect manually")
        return 2
    print("RESULT: PASS — concurrent sibling writes are parallel (GH #2015 fixed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
