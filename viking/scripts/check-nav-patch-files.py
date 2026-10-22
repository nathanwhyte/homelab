#!/usr/bin/env python3
"""Check every list of nav-patch files against the one source: the files on disk.

IMPR-1413. homelab#250 added ov_pref_gate_patch.py to the ConfigMap but not to the
Deployments' ``items:`` lists, and the pod ran without the gate until homelab#252.
The patch set is ``sitecustomize.py`` plus every ``ov_*_patch.py`` in
viking/manifests/test/nav-patch/. This checks, with a YAML parser and ``ast``:

  * the prod ConfigMap (openviking-nav-patch-configmap.yaml) holds exactly that set,
    each value byte-identical to its file;
  * the test kustomization's ov-nav-patch generator lists exactly that set;
  * both Deployments mount their nav-patch ConfigMap whole, with no ``items:`` list;
  * sitecustomize.py's TARGETS names every patch module, and only those.

Run by ``regen-nav-patch-configmap.sh --check`` (a pre-commit hook). Needs PyYAML.
Usage: check-nav-patch-files.py [--root <repo root>]
"""

import argparse
import ast
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit(
        "check-nav-patch-files: needs PyYAML (pre-commit installs it; by hand: "
        "uv run --no-project --with pyyaml python viking/scripts/check-nav-patch-files.py)"
    )

NAV = "viking/manifests/test/nav-patch"
CONFIGMAP = (
    "viking/manifests/openviking-nav-patch-configmap.yaml",
    "openviking-nav-patch",
)
KUSTOMIZATION = ("viking/manifests/test/kustomization.yaml", "ov-nav-patch")
DEPLOYMENTS = (
    (
        "viking/manifests/openviking-deployment.yaml",
        "openviking",
        "openviking-nav-patch",
    ),
    (
        "viking/manifests/test/openviking-test-deployment.yaml",
        "openviking-test",
        "ov-nav-patch",
    ),
)
VOLUME = "nav-patch"


def patch_files(root):
    """The patch set: sitecustomize.py and every ov_*_patch.py, by file name."""
    nav = root / NAV
    names = ["sitecustomize.py"] + sorted(p.name for p in nav.glob("ov_*_patch.py"))
    return [n for n in names if (nav / n).is_file()]


def load_docs(path):
    with open(path, encoding="utf-8") as fh:
        return [d for d in yaml.safe_load_all(fh) if isinstance(d, dict)]


def diff_sets(label, want, got):
    errors = []
    missing, extra = sorted(set(want) - set(got)), sorted(set(got) - set(want))
    if missing:
        errors.append(f"{label}: missing {', '.join(missing)}")
    if extra:
        errors.append(f"{label}: not a patch file {', '.join(extra)}")
    return errors


def check_configmap(root, files):
    rel, name = CONFIGMAP
    docs = [
        d
        for d in load_docs(root / rel)
        if d.get("kind") == "ConfigMap"
        and (d.get("metadata") or {}).get("name") == name
    ]
    if len(docs) != 1:
        return [f"{rel}: expected one ConfigMap {name}, found {len(docs)}"]
    doc = docs[0]
    data = doc.get("data") or {}
    errors = diff_sets(f"{rel} data", files, data)
    if doc.get("apiVersion") != "v1" or doc["metadata"].get("namespace") != "viking":
        errors.append(f"{rel}: expected apiVersion v1 in namespace viking")
    if doc.get("binaryData"):
        errors.append(f"{rel}: unexpected binaryData")
    for fname in files:
        if fname in data:
            text = (root / NAV / fname).read_bytes().decode("utf-8")
            if data[fname] != text:
                errors.append(f"{rel}: {fname} differs from {NAV}/{fname}")
    if errors:
        errors.append(f"{rel}: run viking/scripts/regen-nav-patch-configmap.sh")
    return errors


def check_kustomization(root, files):
    rel, name = KUSTOMIZATION
    gens = [
        g
        for d in load_docs(root / rel)
        if d.get("kind") == "Kustomization"
        for g in d.get("configMapGenerator") or []
        if g.get("name") == name
    ]
    if len(gens) != 1:
        return [f"{rel}: expected one configMapGenerator {name}, found {len(gens)}"]
    listed = gens[0].get("files") or []
    errors = []
    dupes = sorted({f for f in listed if listed.count(f) > 1})
    if dupes:
        errors.append(f"{rel} {name}: listed twice {', '.join(dupes)}")
    want = [f"nav-patch/{f}" for f in files]
    return errors + diff_sets(f"{rel} {name} files", want, listed)


def check_deployment(root, rel, deployment, configmap):
    docs = [
        d
        for d in load_docs(root / rel)
        if d.get("kind") == "Deployment"
        and (d.get("metadata") or {}).get("name") == deployment
    ]
    if len(docs) != 1:
        return [f"{rel}: expected one Deployment {deployment}, found {len(docs)}"]
    spec = ((docs[0].get("spec") or {}).get("template") or {}).get("spec") or {}
    vols = [v for v in spec.get("volumes") or [] if v.get("name") == VOLUME]
    if len(vols) != 1:
        return [f"{rel}: expected one volume {VOLUME}, found {len(vols)}"]
    cm = vols[0].get("configMap")
    if not isinstance(cm, dict):
        return [f"{rel}: volume {VOLUME} is not a configMap volume"]
    errors = []
    if cm.get("name") != configmap:
        errors.append(
            f"{rel}: volume {VOLUME} mounts {cm.get('name')!r}, not {configmap}"
        )
    if "items" in cm:
        errors.append(
            f"{rel}: volume {VOLUME} has an items: list; mount the whole ConfigMap "
            "so every patch file is on PYTHONPATH (homelab#250)"
        )
    return errors


def target_modules(path):
    """Patch module names in sitecustomize.py's TARGETS dict literal."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "TARGETS" for t in node.targets)
            and isinstance(node.value, ast.Dict)
        ):
            names = set()
            for value in node.value.values:
                pairs = value.elts if isinstance(value, ast.List) else [value]
                for pair in pairs:
                    if not (isinstance(pair, ast.Tuple) and pair.elts):
                        raise ValueError(
                            f"TARGETS value at line {pair.lineno} is not a tuple"
                        )
                    head = pair.elts[0]
                    if not (
                        isinstance(head, ast.Constant) and isinstance(head.value, str)
                    ):
                        raise ValueError(
                            f"TARGETS module at line {head.lineno} is not a string"
                        )
                    names.add(head.value)
            return names
    raise ValueError("no TARGETS dict literal")


def check_targets(root, files):
    rel = f"{NAV}/sitecustomize.py"
    try:
        named = target_modules(root / rel)
    except (SyntaxError, ValueError) as exc:
        return [f"{rel}: cannot read TARGETS: {exc}"]
    want = [f[: -len(".py")] for f in files if f != "sitecustomize.py"]
    return diff_sets(f"{rel} TARGETS", want, named)


def check(root):
    files = patch_files(root)
    errors = check_configmap(root, files) + check_kustomization(root, files)
    for rel, deployment, configmap in DEPLOYMENTS:
        errors += check_deployment(root, rel, deployment, configmap)
    return errors + check_targets(root, files)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parent.parent.parent
    )
    args = parser.parse_args(argv)
    errors = check(args.root)
    for err in errors:
        print(err, file=sys.stderr)
    if errors:
        return 1
    print(f"nav-patch file lists agree ({len(patch_files(args.root))} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
