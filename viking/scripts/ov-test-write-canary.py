#!/usr/bin/env python3
"""ov-test write canary for a vectordb or OpenViking server patch (IMPR-1425 row R5).

One run against openviking-test does what the BUG-1268 canary did by hand: a
replace-write with read-back, the vikingdb observer, and direct vectordb counts
through OpenViking's own HTTP adapter (`count`, `strict_count`, both filtered
forms), the path a missing aggregate route broke on v0.5.1.1.

Two traps from that run are handled here rather than rediscovered:

- The collection is read from the live `openviking-test-config` ConfigMap's
  `ov.conf` (`storage.vectordb.name`, `context_test` on 2026-10-25), never assumed.
  The run refuses before any write when it matches prod's
  `openviking-standalone-config` collection; whether the server's vikingdb observer
  names it is one of the checks.
- A replace sent right after the probe's create gets 409 (path busy) until the
  create is processed, so the replace retries on 409 (`--replace-attempts`, every
  `--replace-wait` seconds).

The vectordb service and port come from the same `ov.conf` (`storage.vectordb.url`),
and the account and user from its `default_account` / `default_user`. Writes go
only to PROBE_URI on ov-test. The HTTP calls are stdlib; the count step needs the
`openviking` package matching the server (a scratch venv), and reports
`skipped` without it, which fails the run unless `--no-counts` is passed.

The read-back must equal the replacement exactly, and the replacement carries a
marker unique to the run.

Exit codes: 0 every check passed, 3 a check failed, 1 the run could not finish
(config, prod refusal, port-forward, a transport error). Every run that ends,
error or not, prints one JSON receipt holding what it saw so far.

Usage: <venv>/bin/python -I ov-test-write-canary.py --label before|after
"""

import argparse
import base64
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

NS = "viking"
TEST_CONFIGMAP = "openviking-test-config"
PROD_CONFIGMAP = "openviking-standalone-config"
SERVER_SVC = "svc/openviking-test"
SERVER_PORT = 1933
SECRET = "openviking-api-key"
PROBE_URI = "viking://resources/ov-test-write-canary/probe.md"
EXIT_OK, EXIT_ERROR, EXIT_FAILED = 0, 1, 3


class CanaryError(Exception):
    """The run cannot start or would not be safe."""


def kubectl(*args):
    p = subprocess.run(["kubectl", "-n", NS, *args], capture_output=True, text=True)
    if p.returncode != 0:
        raise CanaryError(f"kubectl {' '.join(args)}: {p.stderr.strip()[:300]}")
    return p.stdout


def read_ov_conf(configmap):
    try:
        cm = json.loads(kubectl("get", "configmap", configmap, "-o", "json"))
        return json.loads(cm["data"]["ov.conf"])
    except (ValueError, KeyError, TypeError) as exc:
        raise CanaryError(f"cannot read ov.conf from {configmap}: {exc!r}") from exc


def target_from_conf(conf):
    """The collection, vectordb service and port, account and user an ov.conf names."""
    try:
        vdb = conf["storage"]["vectordb"]
        name, url = vdb["name"], urllib.parse.urlsplit(vdb["url"])
        account, user = conf["default_account"], conf["default_user"]
    except (KeyError, TypeError) as exc:
        raise CanaryError(f"ov.conf lacks {exc}") from exc
    if not (isinstance(name, str) and name and url.hostname and url.port):
        raise CanaryError(
            f"ov.conf vectordb name {name!r} or url {vdb['url']!r} unusable"
        )
    return {
        "collection": name,
        "vectordb_svc": "svc/" + url.hostname.split(".")[0],
        "vectordb_port": url.port,
        "account": account,
        "user": user,
    }


def refuse_prod(test_collection, prod_conf):
    prod = (prod_conf.get("storage") or {}).get("vectordb", {}).get("name")
    if not prod:
        raise CanaryError(f"{PROD_CONFIGMAP} names no vectordb collection to compare")
    if prod == test_collection:
        raise CanaryError(
            f"ov-test's collection {test_collection!r} is prod's; refusing"
        )


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def stop(p):
    p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()


def forward(svc, remote, timeout=20):
    """A port-forward to `svc`, once its local port accepts connections.

    The child is stopped here on any way out before it is handed back, an
    interrupt included; after that, the caller owns it.
    """
    local = free_port()
    p = subprocess.Popen(
        ["kubectl", "-n", NS, "port-forward", svc, f"{local}:{remote}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if p.poll() is not None:
                raise CanaryError(f"port-forward to {svc} exited {p.returncode}")
            try:
                socket.create_connection(("127.0.0.1", local), timeout=1).close()
                return p, local
            except OSError:
                time.sleep(0.5)
        raise CanaryError(f"port-forward to {svc} not ready after {timeout}s")
    except BaseException:
        stop(p)
        raise


def http(base, headers, method, path, params=None, body=None, timeout=120):
    """(status, parsed JSON or text) for one request; HTTP errors are results, not raises."""
    url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode(errors="replace")[:600]


def replace_with_retry(call, content, attempts, wait, sleep=time.sleep):
    """POST a replace, retrying while the path is busy (409). Returns (status, body, tries)."""
    for n in range(1, attempts + 1):
        status, body = call(
            "POST", "/api/v1/content/write",
            body={"uri": PROBE_URI, "content": content, "mode": "replace"},
        )  # fmt: skip
        if status != 409 or n == attempts:
            return status, body, n
        sleep(wait)


def observer_names(body, collection):
    """Whether a vikingdb observer response reports `collection` and a healthy component."""
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict):
        return False, False
    status = result.get("status")
    names = (
        isinstance(status, str) and f"Collection: {collection}" in status.splitlines()
    )
    return names, result.get("is_healthy") is True


def vectordb_counts(port, collection, account):
    """Direct counts through OpenViking's own adapter, or None when it cannot be imported."""
    try:
        from openviking.storage.vectordb_adapters.http_adapter import (
            HttpCollectionAdapter,
        )
    except ImportError:
        return None
    adapter = HttpCollectionAdapter("127.0.0.1", port, "default", collection, "default")
    calls = (
        ("count", lambda: adapter.count()),
        ("strict_count", lambda: adapter.strict_count()),
        ("strict_count_account", lambda: adapter.strict_count(
            {"op": "must", "field": "account_id", "conds": [account]})),
        ("strict_count_no_match", lambda: adapter.strict_count(
            {"op": "must", "field": "account_id", "conds": ["ov-test-write-canary-none"]})),
    )  # fmt: skip
    counts = {}
    for name, call in calls:
        try:
            counts[name] = call()
        except Exception as e:  # noqa: BLE001 — the canary reports every outcome
            counts[name] = f"ERROR {e!r}"[:300]
    return counts


def read_back_matches(status, body, expected):
    """Whether a content read returned exactly the replacement, not just part of it."""
    text = body.get("result") if status == 200 and isinstance(body, dict) else None
    return text == expected


def failures(out, need_counts):
    """The names of the checks that did not pass."""
    bad = []
    if out.get("health") != 200:
        bad.append("health")
    if not out.get("observer_names_collection"):
        bad.append("observer_names_collection")
    if not out.get("observer_healthy"):
        bad.append("observer_healthy")
    if out.get("replace", {}).get("http") != 200:
        bad.append("replace")
    if not out.get("read_back_matches"):
        bad.append("read_back_matches")
    counts = out.get("vectordb")
    if counts is None:
        if need_counts:
            bad.append("vectordb (openviking not importable; pass --no-counts to skip)")
    else:
        ints = {
            k: v
            for k, v in counts.items()
            if isinstance(v, int) and not isinstance(v, bool)
        }
        if len(ints) != len(counts):
            bad.append("vectordb")
        elif ints["strict_count_no_match"] != 0:
            bad.append("vectordb (a filter matching nothing counted rows)")
    return bad


def run(args, out):
    """Fill the receipt `out` as the checks run, so a run that raises keeps what it saw."""
    conf = read_ov_conf(TEST_CONFIGMAP)
    target = target_from_conf(conf)
    refuse_prod(target["collection"], read_ov_conf(PROD_CONFIGMAP))
    out.update(target=target, probe=PROBE_URI)
    key = base64.b64decode(
        kubectl("get", "secret", SECRET, "-o", "jsonpath={.data.api-key}")
    ).decode()
    procs = []
    try:
        p, server = forward(SERVER_SVC, SERVER_PORT)
        procs.append(p)
        headers = {"X-API-Key": key, "X-OpenViking-Account": target["account"],
                   "X-OpenViking-User": target["user"]}  # fmt: skip

        def call(method, path, params=None, body=None):
            return http(
                f"http://127.0.0.1:{server}", headers, method, path, params, body
            )

        status, body = call("GET", "/api/v1/observer/vikingdb")
        out["observer_vikingdb"] = {"http": status, "body": body}
        # A check, not a refusal: an unpatched server's observer can fail outright,
        # and that is the `before` result. The forward itself names the test service.
        names, healthy = observer_names(body, target["collection"])
        out["observer_names_collection"], out["observer_healthy"] = names, healthy
        out["health"] = call("GET", "/health")[0]
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if call("GET", "/api/v1/content/read", params={"uri": PROBE_URI})[0] != 200:
            status, body = call("POST", "/api/v1/content/write", body={
                "uri": PROBE_URI, "content": f"# canary\n\ncreated {stamp}\n",
                "mode": "create"})  # fmt: skip
            out["create"] = {"http": status, "body": body}
        # A marker unique to this run, so an earlier run's content cannot match.
        expected = f"# canary\n\nreplaced {stamp} {uuid.uuid4().hex} ({args.label})\n"
        status, body, tries = replace_with_retry(
            call, expected, args.replace_attempts, args.replace_wait
        )
        out["replace"], out["replace_attempts"] = {"http": status, "body": body}, tries
        status, body = call("GET", "/api/v1/content/read", params={"uri": PROBE_URI})
        out["read_back_matches"] = read_back_matches(status, body, expected)
        if not out["read_back_matches"]:
            out["read_back"] = {"http": status, "body": body, "expected": expected}
        if args.no_counts:
            out["vectordb"] = "skipped (--no-counts)"
        else:
            p, vport = forward(target["vectordb_svc"], target["vectordb_port"])
            procs.append(p)
            out["vectordb"] = vectordb_counts(
                vport, target["collection"], target["account"]
            )
    finally:
        for p in procs:
            stop(p)
    counts = out["vectordb"]
    out["failed"] = failures(
        {**out, "vectordb": None if isinstance(counts, str) else counts},
        need_counts=not args.no_counts,
    )
    return out


def positive_int(text):
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--label", required=True, help="e.g. before or after, kept in the receipt"
    )
    ap.add_argument("--replace-attempts", type=positive_int, default=30)
    ap.add_argument(
        "--replace-wait", type=float, default=10.0, help="seconds between 409 retries"
    )
    ap.add_argument("--no-counts", action="store_true",
                    help="skip the vectordb counts (no openviking package)")  # fmt: skip
    args = ap.parse_args(argv)
    out = {"label": args.label}
    try:
        run(args, out)
    except Exception as exc:  # noqa: BLE001 — every finished run ends in a receipt
        out["error"] = (
            str(exc) if isinstance(exc, CanaryError) else f"{type(exc).__name__}: {exc}"
        )
        print(json.dumps(out, indent=2, default=str))
        return EXIT_ERROR
    print(json.dumps(out, indent=2, default=str))
    return EXIT_FAILED if out["failed"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
