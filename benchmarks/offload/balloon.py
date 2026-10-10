"""Hold wired memory so the rest of the machine has a fixed budget for one inference engine.

The engine under test (its own allocations plus the file cache its SSD reads land in) can then
use only about `--leave-gib` of RAM, whichever engine it is and whatever else is running. The
balloon allocates anonymous memory in 1 GiB chunks and mlock()s each one, so macOS can neither
compress nor page it out, until the reclaimable memory (free + inactive + file-backed pages
beyond inactive) is at or below the target. It then prints one JSON line and holds until
SIGTERM or SIGINT, releasing everything on exit.

Usage: python3 balloon.py --leave-gib 47 [--max-gib 50]
"""

import argparse
import ctypes
import json
import mmap
import re
import signal
import subprocess
import sys
import time

GIB = 1024**3
libc = ctypes.CDLL(None, use_errno=True)
libc.mlock.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
libc.munlock.argtypes = [ctypes.c_void_p, ctypes.c_size_t]


def reclaimable_gib():
    vm = subprocess.run(["vm_stat"], capture_output=True, text=True, check=True).stdout
    page = int(re.search(r"page size of (\d+)", vm).group(1))

    def pages(name):
        return int(re.search(rf"{name}:\s+(\d+)", vm).group(1))

    free = pages("Pages free")
    inactive = pages("Pages inactive")
    file_backed = pages("File-backed pages")
    return (free + inactive + max(0, file_backed - inactive)) * page / GIB


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--leave-gib", type=float, required=True)
    ap.add_argument("--max-gib", type=float, default=50.0)
    a = ap.parse_args()
    chunks = []

    def release(*_):
        for m in chunks:
            addr = ctypes.addressof(ctypes.c_char.from_buffer(m))
            libc.munlock(addr, GIB)
            m.close()
        print(json.dumps({"event": "released", "gib": len(chunks)}), flush=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, release)
    signal.signal(signal.SIGINT, release)
    before = reclaimable_gib()
    while reclaimable_gib() - 1 >= a.leave_gib and len(chunks) < a.max_gib:
        m = mmap.mmap(-1, GIB)
        addr = ctypes.addressof(ctypes.c_char.from_buffer(m))
        if libc.mlock(addr, GIB) != 0:
            m.close()
            print(json.dumps({"event": "mlock_failed", "errno": ctypes.get_errno()}))
            break
        chunks.append(m)
        time.sleep(0.05)
    print(
        json.dumps(
            {
                "event": "holding",
                "balloon_gib": len(chunks),
                "reclaimable_before_gib": round(before, 1),
                "reclaimable_after_gib": round(reclaimable_gib(), 1),
                "target_gib": a.leave_gib,
            }
        ),
        flush=True,
    )
    while True:
        signal.pause()


if __name__ == "__main__":
    main()
