"""IMPR-1185 canary: seed real vault directories into the TEST instance and check nav.

Seeds two directories from the compendium vault into openviking-test:
  * bugs/resolved       — >50 direct entries with long summaries: the BATCHED path
                          (BUG-1172's loss case), with its subdirectories (1 file each)
  * features/completed  — mixed files + subdirectories on the SINGLE-SHOT path
                          (BUG-1169's 0/6 case), subdirectories 1 file each
Then waits for semantic generation to settle and checks, per directory:
  * every `ov ls` child (files AND subdirectories) is a link target in Quick Navigation
  * no `input_sample` placeholder text anywhere in the overview
  * overview length <= overview_max_chars (20000)
  * the L0 abstract is prose, not the nav list
and in the pod log: the patch applied once per worker, never "NOT applied", no
"assemble failed". Writes a JSON + text report. Serial writes; never touches prod.

Usage: uv run --no-project --with httpx python canary.py --vault ~/code/compendium --out <dir>
"""

import argparse
import base64
import json
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

NS = "viking"
SVC = "svc/openviking-test"
LOCAL_PORT = 19333
ROOT = "viking://resources/compendium"
CAP = 20000
DIRS = ["bugs/resolved", "features/completed"]
PER_SUBDIR = 1

lines: list[str] = []


def log(msg: str) -> None:
    s = f"[{datetime.now(UTC).strftime('%H:%M:%SZ')}] {msg}"
    print(s, flush=True)
    lines.append(s)


def root_key() -> str:
    out = subprocess.run(
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
    return base64.b64decode(out).decode()


def entry_uri(rel_dir: str, path: Path) -> str:
    m = re.match(r"^([A-Z]+-\d+)", path.name)
    leaf = (m.group(1).lower() if m else path.stem.lower()) + ".md"
    return f"{ROOT}/{rel_dir}/{leaf}"


def fixtures(vault: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for rel in DIRS:
        base = vault / rel
        for f in sorted(
            p
            for p in base.iterdir()
            if p.is_file() and p.suffix == ".md" and p.name != "index.md"
        ):
            out.append((entry_uri(rel, f), f.read_text()))
        for sub in sorted(p for p in base.iterdir() if p.is_dir()):
            picks = sorted(p for p in sub.rglob("*.md") if p.name != "index.md")[
                :PER_SUBDIR
            ]
            for f in picks:
                out.append((entry_uri(f"{rel}/{sub.name}", f), f.read_text()))
    return out


def ok(r: httpx.Response) -> dict:
    body = r.json()
    if r.status_code >= 400 or body.get("status") == "error":
        raise RuntimeError(
            f"{r.request.method} {r.request.url.path} -> {r.status_code} {str(body)[:200]}"
        )
    return body.get("result", body)


def nav_targets(overview: str) -> set[str]:
    m = re.search(
        r"^## Quick Navigation\n(.*?)(?=^## |\Z)", overview, re.DOTALL | re.MULTILINE
    )
    block = m.group(1) if m else ""
    return set(re.findall(r"\]\((viking://[^)\s]+)\)", block))


BOILERPLATE_GLOSS = re.compile(
    r"^(?:this |bug report|technical bug report|document)", re.IGNORECASE
)
DANGLING_TAIL = re.compile(
    r"\b(?:a|an|and|as|at|by|due|during|for|from|in|into|its|of|on|or|that|the|to|"
    r"where|which|while|with|within|named|called)$",
    re.IGNORECASE,
)


def gloss_quality(overview: str) -> dict:
    """Informational counts over Quick Navigation glosses (not part of pass/fail)."""
    m = re.search(
        r"^## Quick Navigation\n(.*?)(?=^## |\Z)", overview, re.DOTALL | re.MULTILINE
    )
    glosses = []
    for line in (m.group(1) if m else "").splitlines():
        g = re.match(r"^- \[([^\]]+)\]\([^)]*\)(?: — (.*?))?\.$", line)
        if g:
            glosses.append((g.group(1), g.group(2) or ""))
    return {
        "nav_lines": len(glosses),
        "gloss_empty": sum(not g for _, g in glosses),
        "gloss_boilerplate": sum(bool(BOILERPLATE_GLOSS.match(g)) for _, g in glosses),
        "gloss_dangling": sum(bool(DANGLING_TAIL.search(g)) for _, g in glosses if g),
        "gloss_name_only": sum(
            g.lower() == n.lower().rstrip("/") for n, g in glosses if g
        ),
    }


def children(c: httpx.Client, uri: str) -> set[str]:
    res = ok(
        c.get(
            "/api/v1/fs/ls",
            params={"uri": uri, "output": "original", "node_limit": 1000},
        )
    )
    items = res if isinstance(res, list) else res.get("items", res.get("nodes", []))
    kids = set()
    for it in items:
        u = it.get("uri") if isinstance(it, dict) else None
        name = (it.get("name") if isinstance(it, dict) else str(it)) or ""
        if name.startswith("."):
            continue
        kids.add(u or f"{uri}/{name}")
    return kids


def regenerate(c: httpx.Client, timeout: int) -> None:
    """ADMIN-reindex each directory (semantic_and_vectors, recursive=false), serially.

    The reindex rebuilds a directory's overview without deleting anything first, and
    its work may not go through the observer queue, so wait on each task's own
    status rather than on queue depth. One directory at a time keeps a single gemma
    consumer in flight.
    """
    admin = {"X-OpenViking-Role": "ADMIN"}
    for d in DIRS:
        res = ok(
            c.post(
                "/api/v1/content/reindex",
                headers=admin,
                json={
                    "uri": f"{ROOT}/{d}",
                    "mode": "semantic_and_vectors",
                    "recursive": False,
                    "wait": False,
                },
            )
        )
        task = res.get("task_id")
        log(f"reindex {d}: task {task} {res.get('status')}")
        t0 = time.monotonic()
        status = None
        while task and time.monotonic() - t0 < timeout:
            r = c.get(f"/api/v1/tasks/{task}", headers=admin)
            body = r.json() if r.status_code == 200 else {}
            status = (body.get("result") or {}).get("status")
            if status not in (None, "accepted", "pending", "queued", "running"):
                break
            time.sleep(30)
        log(
            f"reindex {d}: task finished status={status} after {int(time.monotonic() - t0)} s"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--settle-timeout", type=int, default=5400)
    ap.add_argument(
        "--regenerate",
        action="store_true",
        help="entries already exist: ADMIN-reindex each directory instead of seeding",
    )
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    pf = subprocess.Popen(
        ["kubectl", "-n", NS, "port-forward", SVC, f"{LOCAL_PORT}:1933"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)
    headers = {
        "X-API-Key": root_key(),
        "X-OpenViking-Account": "default",
        "X-OpenViking-User": "noot",
    }
    report: dict = {"started": datetime.now(UTC).isoformat(), "dirs": {}}
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{LOCAL_PORT}", headers=headers, timeout=120
        ) as c:
            h = c.get("/health")
            log(f"test instance /health -> {h.status_code}")
            if h.status_code != 200:
                log("ABORT: test instance not healthy")
                return 2
            t0 = time.monotonic()
            if args.regenerate:
                regenerate(c, args.settle_timeout)
            else:
                fx = fixtures(args.vault)
                log(f"seeding {len(fx)} entries into {ROOT}/{{{','.join(DIRS)}}}")
                for i, (uri, content) in enumerate(fx, 1):
                    parent = uri.rsplit("/", 1)[0]
                    c.post("/api/v1/fs/mkdir", json={"uri": parent})
                    ok(
                        c.post(
                            "/api/v1/content/write",
                            json={"uri": uri, "content": content, "mode": "create"},
                        )
                    )
                    if i % 25 == 0:
                        log(f"  wrote {i}/{len(fx)}")
            log("seeded; waiting for semantic generation to settle")

            stable = 0
            while time.monotonic() - t0 < args.settle_timeout:
                q = c.get("/api/v1/observer/queue").text
                pending = [
                    int(x) for x in re.findall(r"Semantic[\w-]*\s*\|\s*(\d+)\s*\|", q)
                ]
                ovs = [
                    ok(c.get("/api/v1/content/overview", params={"uri": f"{ROOT}/{d}"}))
                    for d in DIRS
                ]
                ready = all(isinstance(o, str) and "is not ready" not in o for o in ovs)
                if ready and pending and sum(pending) == 0:
                    stable += 1
                    if stable >= 3:
                        break
                else:
                    stable = 0
                if int(time.monotonic() - t0) % 300 < 60:
                    log(
                        f"  waiting: semantic pending={pending} overviews_ready={ready}"
                    )
                time.sleep(60)
            log(f"settled after {int(time.monotonic() - t0)} s (stable={stable})")

            failed = False
            for d in DIRS:
                uri = f"{ROOT}/{d}"
                ov = ok(c.get("/api/v1/content/overview", params={"uri": uri}))
                ab = ok(c.get("/api/v1/content/abstract", params={"uri": uri}))
                kids = children(c, uri)
                nav = nav_targets(ov)
                missing = sorted(k for k in kids if k not in nav)
                checks = {
                    "children": len(kids),
                    "nav_targets": len(nav),
                    "missing_from_nav": missing,
                    "placeholder_left": "input_sample" in ov,
                    "length": len(ov),
                    "under_cap": len(ov) <= CAP,
                    "abstract_is_prose": bool(ab)
                    and not ab.lstrip().startswith("-")
                    and "viking://" not in ab,
                    "has_detail": "## Detailed Description" in ov,
                    **gloss_quality(ov),
                }
                passed = (
                    not missing
                    and not checks["placeholder_left"]
                    and checks["under_cap"]
                    and checks["abstract_is_prose"]
                )
                failed |= not passed
                report["dirs"][d] = {**checks, "pass": passed}
                (args.out / f"overview-{d.replace('/', '_')}.md").write_text(ov)
                log(
                    f"{d}: {'PASS' if passed else 'FAIL'} {json.dumps({k: v for k, v in checks.items() if k != 'missing_from_nav'})} missing={missing[:10]}"
                )

        logs = subprocess.run(
            [
                "kubectl",
                "-n",
                NS,
                "logs",
                "deploy/openviking-test",
                "-c",
                "openviking-test",
            ],
            capture_output=True,
            check=True,
        ).stdout.decode(
            "utf-8", "replace"
        )  # pod logs are binary-classified; decode leniently
        counts = {
            "applied": logs.count("ov-nav-patch: applied"),
            "not_applied": logs.count("ov-nav-patch: NOT applied"),
            "assemble_failed": logs.count("ov-nav-patch: assemble failed"),
            "batched_events": len(
                re.findall(r"Generating overview for \S+ in \d+ batches", logs)
            ),
            "overview_failures": logs.count("Failed to generate overview for"),
        }
        report["pod_log"] = counts
        log(f"pod log: {counts}")
        patch_ok = (
            counts["applied"] >= 1
            and counts["not_applied"] == 0
            and counts["assemble_failed"] == 0
        )
        failed |= not patch_ok
        report["result"] = "FAIL" if failed else "PASS"
        log(f"RESULT: {report['result']}")
        return 1 if failed else 0
    finally:
        pf.terminate()
        report["finished"] = datetime.now(UTC).isoformat()
        (args.out / "canary-report.json").write_text(json.dumps(report, indent=2))
        (args.out / "canary-log.txt").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
