#!/usr/bin/env python3
"""Compare a live OpenViking vector collection with the installed release's schema (IMPR-1431).

BUG-1271: the in-place v0.5.1.1 upgrade kept production's `context` collection, created
by an older release, so it lacked the `md5` field v0.5.1.1's schema adds, and nothing
checked. The test stack could not show it: ov-test builds `context_test` fresh on the
new schema. This reads the live collection's field list and compares it with
`CollectionSchemas.context_collection` from the `openviking` package this Python
imports, so run it with a venv of the target release (GUIDE-1085 Phase 0.3 before the
window, Phase 4 after).

The collection, vectordb service, port, project and index come from the named
ConfigMap's `ov.conf`, read as the ov-test write canary reads it (its helpers are
reused, with the same refusals for a templated value or a service outside `viking`).
The only request is the collection's metadata read, through a port-forward that is
stopped on the way out; nothing is written.

A field the release defines that the live collection lacks, or one whose type
differs, is drift. A live field the release no longer defines is reported as
`extra` and is not a fault: OpenViking leaves it unwritten.

Exit codes: 0 no drift, 3 drift, 1 the check could not run (config, a refusal, no
importable openviking, an empty metadata read, port-forward or transport). Each prints
one JSON receipt on one line.

Usage: <venv>/bin/python -I ov-collection-schema-check.py [--configmap NAME]
"""

import argparse
import contextlib
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROD_CONFIGMAP = "openviking-standalone-config"
EXIT_OK, EXIT_ERROR, EXIT_DRIFT = 0, 1, 3


class CheckError(Exception):
    """The check cannot run."""


def load_canary():
    """ov-test-write-canary.py as a module, for its ConfigMap, forward and adapter helpers."""
    spec = importlib.util.spec_from_file_location(
        "ov_test_write_canary", HERE / "ov-test-write-canary.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def release_schema():
    """(the release's context-collection schema builder, its version)."""
    try:
        import openviking
        from openviking.storage.collection_schemas import CollectionSchemas
    except ImportError as exc:
        raise CheckError(
            f"openviking is not importable by {sys.executable} ({exc}); run this with"
            " a venv of the target release"
        ) from exc
    return CollectionSchemas.context_collection, str(openviking.__version__)


def field_types(fields):
    """{name: type} for each well-formed field entry."""
    return {
        f["FieldName"]: f.get("FieldType")
        for f in fields
        if isinstance(f, dict) and isinstance(f.get("FieldName"), str)
    }


def compare(live, release):
    """The release's fields the live collection lacks or types differently, and its extras."""
    have, want = field_types(live), field_types(release)
    return {
        "missing": sorted(set(want) - set(have)),
        "extra": sorted(set(have) - set(want)),
        "retyped": {
            name: {"live": have[name], "release": want[name]}
            for name in sorted(set(have) & set(want))
            if have[name] != want[name]
        },
    }


def drift(diff):
    """The failures in a comparison: missing and retyped fields, never extras."""
    bad = []
    if diff["missing"]:
        bad.append("missing: " + ", ".join(diff["missing"]))
    if diff["retyped"]:
        bad.append("retyped: " + ", ".join(diff["retyped"]))
    return bad


def live_fields(collection):
    """The live collection's field entries, refused when the read shows none."""
    meta = collection.get_meta_data()
    fields = meta.get("Fields") if isinstance(meta, dict) else None
    if not (isinstance(fields, list) and fields):
        raise CheckError("the collection's metadata lists no fields")
    return fields


def run(configmap):
    canary = load_canary()
    conf = canary.read_ov_conf(configmap)
    target = canary.target_from_conf(conf)
    schema, release = release_schema()
    cls = canary.adapter_class()
    if cls is None:
        raise CheckError("OpenViking's HTTP collection adapter is not importable")
    with contextlib.ExitStack() as children:
        port = canary.forward(target["vectordb_svc"], target["vectordb_port"], children)
        adapter = cls("127.0.0.1", port, target["project"], target["collection"],
                      target["index"])  # fmt: skip
        live = live_fields(adapter.get_collection())
    dimension = conf["storage"]["vectordb"].get("dimension")
    wanted = schema(target["collection"], dimension)["Fields"]
    diff = compare(live, wanted)
    return {
        "collection": target["collection"],
        "release": release,
        "live_fields": len(field_types(live)),
        "release_fields": len(field_types(wanted)),
        **diff,
        "failed": drift(diff),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--configmap", default=PROD_CONFIGMAP,
                    help=f"the ConfigMap whose ov.conf names the collection (default {PROD_CONFIGMAP})")  # fmt: skip
    args = ap.parse_args(argv)
    out = {"configmap": args.configmap}
    try:
        out.update(run(args.configmap))
        code = EXIT_DRIFT if out["failed"] else EXIT_OK
    except Exception as exc:  # noqa: BLE001 — every run that ends prints a receipt
        out["error"] = (
            str(exc) if isinstance(exc, CheckError) else f"{type(exc).__name__}: {exc}"
        )
        code = EXIT_ERROR
    print(json.dumps(out, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
