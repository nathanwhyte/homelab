#!/usr/bin/env python3
"""Add the fields a live OpenViking vector collection lacks from the installed release's schema (BUG-1271).

The in-place v0.5.1.1 upgrade kept production's `context` collection, which has no
`md5` field. At startup OpenViking adds missing fields itself, but only for the
`local` and `cuvs` backends (`init_context_collection`'s `_update_local_schema`);
production's `http` backend is skipped. This makes the same call that path makes,
`update_collection_schema`'s: it sends only the missing field entries to the
vectordb service's UpdateVikingdbCollection, which appends them to the collection's
field list. Existing records read the field's DefaultValue until a write fills it.

After `--apply`, restart every OpenViking server that writes to the collection. A
running server caches the collection's field list on first use
(`_SingleAccountBackend._get_meta_data`) and `_filter_known_fields` drops any field
missing from that cache. The server refreshes the cache only after its own schema
or description update, and this script updates the schema from outside, so until
a server restarts its writes leave the new field at its default.

A dry run by default: it prints the field entries it would add and changes
nothing. `--apply` sends them, then re-reads the collection's metadata and fails
unless every added field is present with the release's type and no field was
lost; the service reports success even when its own validation rejects an update.
A field whose live type differs from the release's cannot be changed in place, so
any retyped field refuses the whole run.

Target selection, refusals, the port-forward and the receipt contract are
ov-collection-schema-check.py's. `--collection` replaces the ConfigMap's collection,
for a rehearsal against a scratch collection on the test vectordb.

Exit codes: 0 nothing to add, a dry run, or fields added and verified; 3 refused (a
retyped field); 1 error, including an update that did not take; 130 interrupted;
143 terminated. Each prints one JSON receipt on one line. `--help` prints usage
and exits 0.

Usage: <venv>/bin/python -I ov-collection-add-fields.py [--configmap NAME] [--collection NAME] [--apply]
"""

import contextlib
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "ov_collection_schema_check", HERE / "ov-collection-schema-check.py"
)
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)


def run(configmap, collection=None, apply=False):
    canary = sc.load_canary()
    conf = canary.read_ov_conf(configmap)
    target = canary.target_from_conf(conf)
    if collection:
        target = {**target, "collection": collection}
    schema, release = sc.release_schema()
    cls = canary.adapter_class()
    if cls is None:
        raise sc.CheckError("OpenViking's HTTP collection adapter is not importable")
    dimension = conf["storage"]["vectordb"].get("dimension")
    wanted = schema(target["collection"], dimension)["Fields"]
    with contextlib.ExitStack() as children:
        port = canary.forward(target["vectordb_svc"], target["vectordb_port"], children)
        adapter = cls("127.0.0.1", port, target["project"], target["collection"],
                      target["index"])  # fmt: skip
        coll = adapter.get_collection()
        live = sc.live_fields(coll)
        diff = sc.compare(live, wanted)
        refused = ["retyped: " + ", ".join(diff["retyped"])] if diff["retyped"] else []
        add = (
            []
            if refused
            else [f for f in wanted if f.get("FieldName") in diff["missing"]]
        )
        out = {
            "collection": target["collection"],
            "release": release,
            "live_fields": len(sc.field_types(live)),
            **diff,
            "add": add,
            "applied": False,
            "failed": refused,
        }
        if apply and add:
            coll.update(fields=add)
            out["live_fields_after"] = verify(live, add, sc.live_fields(coll))
            out["applied"] = True
    return out


def verify(before, added, after):
    """The field count after the update, refused unless it added `added` and lost nothing."""
    have = sc.field_types(after)
    absent = sorted(
        name for name, kind in sc.field_types(added).items() if have.get(name) != kind
    )
    lost = sorted(set(sc.field_types(before)) - set(have))
    if absent or lost:
        parts = [f"still missing: {', '.join(absent)}"] if absent else []
        parts += [f"lost: {', '.join(lost)}"] if lost else []
        raise sc.CheckError("the update did not take; " + "; ".join(parts))
    return len(have)


def main(argv=None):
    ap = sc.Parser(description=__doc__.splitlines()[0])
    ap.add_argument("--configmap", default=sc.PROD_CONFIGMAP,
                    help=f"the ConfigMap whose ov.conf names the vectordb (default {sc.PROD_CONFIGMAP})")  # fmt: skip
    ap.add_argument(
        "--collection", help="add to this collection instead of the ConfigMap's"
    )
    ap.add_argument(
        "--apply", action="store_true", help="send the fields (default: dry run)"
    )
    args = ap.parse_args(argv)
    out = {"configmap": args.configmap, "collection_override": args.collection,
           "apply": args.apply}  # fmt: skip
    return sc.guarded(out, lambda: run(args.configmap, args.collection, args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
