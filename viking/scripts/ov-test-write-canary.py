#!/usr/bin/env python3
"""ov-test write canary for a vectordb or OpenViking server patch (IMPR-1425 row R5).

One run against openviking-test does what the BUG-1268 canary did by hand, and
takes each verdict from the server's own outcome rather than from an inference:

- a replace-write sent with `wait: true`, so the server reports whether its
  vectors were written (`vector_status`), with an exact read-back;
- the probe's own vector record, read through OpenViking's HTTP adapter before and
  after the write: the write must leave a record newer than the one before it;
- direct vectordb counts through the same adapter (`count`, `strict_count`, and
  `strict_count` filtered to the account and to a value matching nothing), the
  path a missing aggregate route broke on v0.5.1.1;
- the vikingdb observer.

Traps from that run are handled here rather than rediscovered:

- The collection is read from the live `openviking-test-config` ConfigMap's
  `ov.conf` (`storage.vectordb.name`, `context_test` on 2026-10-25), never assumed.
  Before any write, the run refuses when it matches prod's
  `openviking-standalone-config` collection, when either name or the test url holds
  a `$` (OpenViking expands `${VAR}` from the server's environment, which this
  script cannot see), or when the server's observer names a different collection.
  An observer that names none (an unpatched server's error path) is a failed check,
  not a refusal, so the `before` run still writes.
- A replace sent right after the probe's create can get 409 (path busy), so the
  replace retries on 409 (`--replace-attempts`, every `--replace-wait` seconds).

The vectordb service and port come from the same `ov.conf` (`storage.vectordb.url`),
its project and index from `project` / `index_name` (OpenViking's default for each
is `default`), and the account and user from `default_account` / `default_user`.
Writes go only to PROBE_URI on ov-test. The HTTP calls are stdlib; the vectordb step
needs the `openviking` package matching the server (a scratch venv). Without it the
vectordb results are null and the run fails, unless `--no-counts` skips the step;
the replace's `vector_status` is checked either way.

The checks: the observer names the collection and reports healthy; `/health` is
200; the replace returns HTTP 200, `status: ok` and `vector_status: complete`; the
read-back equals the replacement exactly (the replacement carries a marker unique
to the run); the probe's vector record exists after the write and its `updated_at`
is later than before it (or it is new); each count is a non-negative integer, the
filter matching nothing counts 0, and the account filter counts at least 1. No two
counts are compared with each other, since other indexing may run between calls.

Exit codes: 0 every check passed, 3 a check failed, 1 the run could not finish
(bad arguments, config, a refusal, port-forward, a transport error), 130
interrupted, 143 terminated (SIGTERM). Each prints one JSON receipt, on one line,
holding what the run saw so far, and every port-forward child is stopped on the
way out. `--help` prints usage and exits 0.

Usage: <venv>/bin/python -I ov-test-write-canary.py --label before|after
"""

import argparse
import base64
import contextlib
import datetime
import json
import math
import signal
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
PROBE_FILTER = {"op": "must", "field": "uri", "conds": [PROBE_URI]}
EXIT_OK, EXIT_ERROR, EXIT_FAILED = 0, 1, 3
EXIT_INTERRUPTED, EXIT_TERMINATED = 130, 143


class CanaryError(Exception):
    """The run cannot start or would not be safe."""


class Terminated(BaseException):
    """SIGTERM, raised so the same unwinding as an interrupt stops every child."""


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


def templated(value):
    return isinstance(value, str) and "$" in value


def target_from_conf(conf):
    """The collection, vectordb service, port, project and index, account and user."""
    try:
        vdb = conf["storage"]["vectordb"]
        name, raw_url = vdb["name"], vdb["url"]
        account, user = conf["default_account"], conf["default_user"]
    except (KeyError, TypeError) as exc:
        raise CanaryError(f"ov.conf lacks {exc}") from exc
    if templated(name) or templated(raw_url):
        raise CanaryError(
            f"ov.conf vectordb name {name!r} or url {raw_url!r} is templated; the"
            " server expands it from an environment this script cannot see"
        )
    url = urllib.parse.urlsplit(raw_url) if isinstance(raw_url, str) else None
    try:
        port = url.port if url else None
    except ValueError:
        port = None
    if not (isinstance(name, str) and name and url and url.hostname and port):
        raise CanaryError(f"ov.conf vectordb name {name!r} or url {raw_url!r} unusable")
    return {
        "collection": name,
        "vectordb_svc": "svc/" + url.hostname.split(".")[0],
        "vectordb_port": port,
        "project": vdb.get("project") or "default",
        "index": vdb.get("index_name") or "default",
        "account": account,
        "user": user,
    }


def refuse_prod(test_collection, prod_conf):
    prod = (prod_conf.get("storage") or {}).get("vectordb", {}).get("name")
    if not prod or templated(prod):
        raise CanaryError(f"{PROD_CONFIGMAP} names no literal collection to compare")
    if prod == test_collection:
        raise CanaryError(
            f"ov-test's collection {test_collection!r} is prod's; refusing"
        )


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def stop(p):
    """Terminate `p`; kill it if it outlives 5 s or the wait is interrupted."""
    try:
        p.terminate()
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    finally:
        if p.poll() is None:
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
    """POST a waited replace, retrying while the path is busy (409).

    Returns (status, body, tries).
    """
    for n in range(1, attempts + 1):
        status, body = call(
            "POST", "/api/v1/content/write",
            body={"uri": PROBE_URI, "content": content, "mode": "replace", "wait": True},
        )  # fmt: skip
        if status != 409 or n == attempts:
            return status, body, n
        sleep(wait)


def observer_collection(body):
    """The collection a vikingdb observer response names on its own line, or None."""
    result = body.get("result") if isinstance(body, dict) else None
    status = result.get("status") if isinstance(result, dict) else None
    if not isinstance(status, str):
        return None
    for line in status.splitlines():
        if line.startswith("Collection: "):
            return line[len("Collection: ") :].strip() or None
    return None


def observer_names(body, collection):
    """Whether a vikingdb observer response reports `collection` and a healthy component."""
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict):
        return False, False
    return observer_collection(body) == collection, result.get("is_healthy") is True


def adapter_class():
    """OpenViking's HTTP collection adapter, or None when it cannot be imported."""
    try:
        from openviking.storage.vectordb_adapters.http_adapter import (
            HttpCollectionAdapter,
        )
    except ImportError:
        return None
    return HttpCollectionAdapter


def probe_record(adapter):
    """The probe's vector record(s) as {rows, updated_at}, or {error}."""
    try:
        rows = adapter.query(filter=PROBE_FILTER, limit=5,
                             output_fields=["uri", "updated_at"])  # fmt: skip
    except Exception as e:  # noqa: BLE001 — the canary reports every outcome
        return {"error": f"ERROR {e!r}"[:300]}
    stamps = [r.get("updated_at") for r in rows if isinstance(r, dict)]
    stamps = [s for s in stamps if isinstance(s, str)]
    return {"rows": len(rows), "updated_at": max(stamps) if stamps else None}


def vectordb_counts(adapter, account):
    """Direct counts through OpenViking's own adapter."""
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


def parse_stamp(value):
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None


def probe_vector_fresh(before, after):
    """Whether the write left a probe record newer than the one before it.

    Compares the server's own two stamps, so neither machine's clock matters. A
    `before` with no rows means the record is new; an errored or unparsable
    `before` cannot show freshness.
    """
    if not isinstance(after, dict) or not after.get("rows"):
        return False
    new = parse_stamp(after.get("updated_at"))
    if new is None or not isinstance(before, dict) or "error" in before:
        return False
    if not before.get("rows"):
        return True
    old = parse_stamp(before.get("updated_at"))
    return old is not None and new > old


def failures(out, need_vectordb):
    """The names of the checks that did not pass."""
    bad = []
    if out.get("health") != 200:
        bad.append("health")
    if not out.get("observer_names_collection"):
        bad.append("observer_names_collection")
    if not out.get("observer_healthy"):
        bad.append("observer_healthy")
    replace = out.get("replace") or {}
    body = replace.get("body")
    result = body.get("result") if isinstance(body, dict) else None
    if replace.get("http") != 200 or not (
        isinstance(body, dict) and body.get("status") == "ok"
    ):
        bad.append("replace")
    elif not (isinstance(result, dict) and result.get("vector_status") == "complete"):
        bad.append("replace (vector_status is not complete)")
    if not out.get("read_back_matches"):
        bad.append("read_back_matches")
    counts = out.get("vectordb")
    if counts is None:
        if need_vectordb:
            bad.append("vectordb (openviking not importable; pass --no-counts to skip)")
        return bad
    if not out.get("probe_vector_fresh"):
        bad.append("probe_vector (no record newer than before the write)")
    return bad + count_failures(counts)


def count_failures(counts):
    """What is wrong with the four counts, judged only on what must hold for any data.

    Each is a non-negative integer, the filter matching nothing counts 0, and the
    account filter counts at least 1: a write that completed left the probe's
    record under that account. No comparison between two counts holds, since the
    calls run one after another while other indexing may run (homelab#266 round 3).
    """
    ok = {
        k: v
        for k, v in counts.items()
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0
    }
    if set(ok) != {"count", "strict_count", "strict_count_account",
                   "strict_count_no_match"}:  # fmt: skip
        return ["vectordb (a count errored or is not a non-negative integer)"]
    bad = []
    if ok["strict_count_no_match"] != 0:
        bad.append("vectordb (a filter matching nothing counted rows)")
    if ok["strict_count_account"] < 1:
        bad.append("vectordb (the account holds no record after a completed write)")
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
    # ExitStack runs every child's stop even when an earlier one raises.
    with contextlib.ExitStack() as children:
        p, server = forward(SERVER_SVC, SERVER_PORT)
        children.callback(stop, p)
        headers = {"X-API-Key": key, "X-OpenViking-Account": target["account"],
                   "X-OpenViking-User": target["user"]}  # fmt: skip

        def call(method, path, params=None, body=None):
            timeout = args.write_timeout if method == "POST" else 120
            base = f"http://127.0.0.1:{server}"
            return http(base, headers, method, path, params, body, timeout)

        status, body = call("GET", "/api/v1/observer/vikingdb")
        out["observer_vikingdb"] = {"http": status, "body": body}
        observed = observer_collection(body)
        if observed is not None and observed != target["collection"]:
            raise CanaryError(
                f"the server's observer names collection {observed!r}, not"
                f" {target['collection']!r}; refusing to write"
            )
        # An observer that names nothing is a check, not a refusal: an unpatched
        # server's observer can fail outright, and that is the `before` result.
        names, healthy = observer_names(body, target["collection"])
        out["observer_names_collection"], out["observer_healthy"] = names, healthy
        out["health"] = call("GET", "/health")[0]
        adapter = None
        cls = None if args.no_counts else adapter_class()
        if cls is not None:
            p, vport = forward(target["vectordb_svc"], target["vectordb_port"])
            children.callback(stop, p)
            adapter = cls("127.0.0.1", vport, target["project"], target["collection"],
                          target["index"])  # fmt: skip
            before = probe_record(adapter)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if call("GET", "/api/v1/content/read", params={"uri": PROBE_URI})[0] != 200:
            status, body = call("POST", "/api/v1/content/write", body={
                "uri": PROBE_URI, "content": f"# canary\n\ncreated {stamp}\n",
                "mode": "create", "wait": True})  # fmt: skip
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
        elif adapter is None:
            out["vectordb"] = None
        else:
            after = probe_record(adapter)
            out["probe_vector"] = {"before": before, "after": after}
            out["probe_vector_fresh"] = probe_vector_fresh(before, after)
            out["vectordb"] = vectordb_counts(adapter, target["account"])
    counts = out["vectordb"]
    out["failed"] = failures(
        {**out, "vectordb": None if isinstance(counts, str) else counts},
        need_vectordb=not args.no_counts,
    )
    return out


def positive_int(text):
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def seconds(text, minimum):
    n = float(text)
    if not math.isfinite(n) or n < minimum:
        raise argparse.ArgumentTypeError(
            f"must be a finite number of at least {minimum}"
        )
    return n


class Parser(argparse.ArgumentParser):
    """Bad arguments end in a receipt and exit 1, like any run that cannot start."""

    def error(self, message):
        self.print_usage(sys.stderr)
        emit({"label": None, "error": f"bad arguments: {message}"})
        sys.exit(EXIT_ERROR)


def on_sigterm(signum, frame):
    raise Terminated


def main(argv=None):
    ap = Parser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--label", required=True, help="e.g. before or after, kept in the receipt"
    )
    ap.add_argument("--replace-attempts", type=positive_int, default=30)
    ap.add_argument("--replace-wait", type=lambda t: seconds(t, 0), default=10.0,
                    help="seconds between 409 retries")  # fmt: skip
    ap.add_argument("--write-timeout", type=lambda t: seconds(t, 1), default=600.0,
                    help="seconds a waited write may take")  # fmt: skip
    ap.add_argument("--no-counts", action="store_true",
                    help="skip the vectordb step (no openviking package)")  # fmt: skip
    args = ap.parse_args(argv)
    out = {"label": args.label}
    previous = signal.signal(signal.SIGTERM, on_sigterm)
    try:
        run(args, out)
    except KeyboardInterrupt:
        out["error"] = "interrupted"
        emit(out)
        return EXIT_INTERRUPTED
    except Terminated:
        out["error"] = "terminated"
        emit(out)
        return EXIT_TERMINATED
    except Exception as exc:  # noqa: BLE001 — every run that ends prints a receipt
        out["error"] = (
            str(exc) if isinstance(exc, CanaryError) else f"{type(exc).__name__}: {exc}"
        )
        emit(out)
        return EXIT_ERROR
    finally:
        signal.signal(signal.SIGTERM, previous)
    emit(out)
    return EXIT_FAILED if out["failed"] else EXIT_OK


def emit(out):
    """The receipt, as one JSON line."""
    print(json.dumps(out, default=str))


if __name__ == "__main__":
    sys.exit(main())
