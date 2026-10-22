#!/usr/bin/env python3
"""Check what each OpenViking stack mounts against the one source: the files on disk.

IMPR-1413. homelab#250 added ov_pref_gate_patch.py to the ConfigMap but not to the
Deployments' ``items:`` lists, and the pod ran without the gate until homelab#252.
The patch set is ``sitecustomize.py`` plus every ``ov_*_patch.py`` in
viking/manifests/test/nav-patch/.

The checker reads what the consumers produce, not the files that feed them: it
renders each stack with ``kubectl kustomize`` (so generators, patches, List
resources and duplicate definitions are resolved the way kustomize resolves them)
and runs sitecustomize.py to read the TARGETS Python builds. For the prod stack
(viking/manifests/) and the test stack (viking/manifests/test/) it requires:

  * exactly one nav-patch ConfigMap, holding exactly the patch set, each value
    byte-identical to its file;
  * exactly one Deployment whose ``nav-patch`` volume mounts that ConfigMap whole
    (no ``items:``), and every container that mounts it does so without subPath,
    at a directory on its literal PYTHONPATH, with no other mount at or under it;

and that sitecustomize.py's TARGETS names every patch module, and only those, with
no target key repeated.

Run by ``regen-nav-patch-configmap.sh --check`` (a pre-commit hook). Needs PyYAML
and kubectl (for ``kubectl kustomize``, which needs no cluster).
Usage: check-nav-patch-files.py [--root <repo root>]
"""

import argparse
import ast
import importlib.util
import shutil
import subprocess
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
REGEN = "viking/scripts/regen-nav-patch-configmap.sh"
# (kustomization dir, nav-patch ConfigMap, Deployment)
STACKS = (
    ("viking/manifests", "openviking-nav-patch", "openviking"),
    ("viking/manifests/test", "ov-nav-patch", "openviking-test"),
)
NAMESPACE = "viking"
VOLUME = "nav-patch"


def patch_files(root):
    """The patch set: sitecustomize.py and every ov_*_patch.py, by file name."""
    nav = root / NAV
    names = ["sitecustomize.py"] + sorted(p.name for p in nav.glob("ov_*_patch.py"))
    return [n for n in names if (nav / n).is_file()]


def diff_sets(label, want, got):
    errors = []
    missing, extra = sorted(set(want) - set(got)), sorted(set(got) - set(want))
    if missing:
        errors.append(f"{label}: missing {', '.join(missing)}")
    if extra:
        errors.append(f"{label}: not a patch file {', '.join(extra)}")
    return errors


def render(root, kdir):
    """The stack's objects as ``kubectl kustomize`` renders them."""
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        raise RuntimeError("kubectl not found (needed for kubectl kustomize)")
    proc = subprocess.run(
        [kubectl, "kustomize", str(root / kdir)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"kubectl kustomize failed: {proc.stderr.strip()}")
    return [d for d in yaml.safe_load_all(proc.stdout) if isinstance(d, dict)]


def named(objects, kind, name):
    return [
        o
        for o in objects
        if o.get("kind") == kind
        and (o.get("metadata") or {}).get("name") == name
        and (o.get("metadata") or {}).get("namespace") == NAMESPACE
    ]


def check_configmap(root, label, objects, name, files):
    found = named(objects, "ConfigMap", name)
    if len(found) != 1:
        return [f"{label}: expected one ConfigMap {name}, found {len(found)}"]
    cm = found[0]
    data = cm.get("data") or {}
    errors = diff_sets(f"{label} ConfigMap {name} keys", files, data)
    if cm.get("binaryData"):
        errors.append(f"{label} ConfigMap {name}: unexpected binaryData")
    for fname in files:
        if fname in data:
            text = (root / NAV / fname).read_bytes().decode("utf-8")
            if data[fname] != text:
                errors.append(
                    f"{label} ConfigMap {name}: {fname} differs from {NAV}/{fname}"
                )
    return errors


def pythonpath(container):
    entries = [e for e in container.get("env") or [] if e.get("name") == "PYTHONPATH"]
    if len(entries) != 1 or not isinstance(entries[0].get("value"), str):
        return None
    return [p.rstrip("/") for p in entries[0]["value"].split(":") if p]


def check_deployment(label, objects, name, configmap):
    found = named(objects, "Deployment", name)
    if len(found) != 1:
        return [f"{label}: expected one Deployment {name}, found {len(found)}"]
    spec = ((found[0].get("spec") or {}).get("template") or {}).get("spec") or {}
    vols = [v for v in spec.get("volumes") or [] if v.get("name") == VOLUME]
    if len(vols) != 1:
        return [f"{label}: expected one volume {VOLUME}, found {len(vols)}"]
    cm = vols[0].get("configMap")
    if not isinstance(cm, dict) or set(vols[0]) != {"name", "configMap"}:
        return [f"{label}: volume {VOLUME} is not a plain configMap volume"]
    errors = []
    if cm.get("name") != configmap:
        errors.append(
            f"{label}: volume {VOLUME} mounts {cm.get('name')!r}, not {configmap}"
        )
    if "items" in cm:
        errors.append(
            f"{label}: volume {VOLUME} has an items: list; mount the whole ConfigMap "
            "so every patch file is on PYTHONPATH (homelab#250)"
        )
    containers = (spec.get("initContainers") or []) + (spec.get("containers") or [])
    mounting = 0
    for c in containers:
        mounts = c.get("volumeMounts") or []
        ours = [m for m in mounts if m.get("name") == VOLUME]
        if not ours:
            continue
        mounting += 1
        where = f"{label}: container {c.get('name')}"
        if len(ours) != 1:
            errors.append(f"{where} mounts {VOLUME} {len(ours)} times")
            continue
        mount = ours[0]
        path = str(mount.get("mountPath", "")).rstrip("/")
        if mount.get("subPath") or mount.get("subPathExpr"):
            errors.append(f"{where} mounts {VOLUME} with a subPath")
        paths = pythonpath(c)
        if paths is None or path not in paths:
            errors.append(
                f"{where} mounts {VOLUME} at {path} but its PYTHONPATH is {paths}"
            )
        for other in mounts:
            if other is mount:
                continue
            op = str(other.get("mountPath", "")).rstrip("/")
            if op == path or op.startswith(path + "/"):
                errors.append(
                    f"{where}: mount {other.get('name')} at {op} hides {VOLUME} files"
                )
    if mounting == 0:
        errors.append(f"{label}: no container mounts volume {VOLUME}")
    return errors


def target_modules(path):
    """Patch module names in the TARGETS that sitecustomize.py builds at runtime.

    The loader is executed (under a private module name) rather than parsed, so the
    mapping is what Python evaluates; a repeated key, which Python collapses to its
    last value, is refused first. Its only side effect is a meta-path finder in this
    process.
    """
    source = path.read_text(encoding="utf-8")
    keys = [
        k.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "TARGETS" for t in node.targets)
        and isinstance(node.value, ast.Dict)
        for k in node.value.keys
        if isinstance(k, ast.Constant)
    ]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise ValueError(f"target module listed twice {', '.join(dupes)}")
    spec = importlib.util.spec_from_file_location("_nav_patch_sitecustomize", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    targets = getattr(module, "TARGETS", None)
    if not isinstance(targets, dict):
        raise ValueError("no TARGETS dict")
    names = set()
    for target, entry in targets.items():
        for pair in entry if isinstance(entry, list) else [entry]:
            if not (isinstance(pair, tuple) and len(pair) == 2):
                raise ValueError(f"{target}: entry {pair!r} is not (module, function)")
            names.add(pair[0])
    return names


def check_targets(root, files):
    rel = f"{NAV}/sitecustomize.py"
    try:
        found = target_modules(root / rel)
    except Exception as exc:  # noqa: BLE001 — any failure to load is a finding
        return [f"{rel}: cannot read TARGETS: {exc!r}"]
    want = [f[: -len(".py")] for f in files if f != "sitecustomize.py"]
    return diff_sets(f"{rel} TARGETS", want, found)


def check(root):
    files = patch_files(root)
    errors = []
    for kdir, configmap, deployment in STACKS:
        label = f"{kdir} (kustomize)"
        try:
            objects = render(root, kdir)
        except RuntimeError as exc:
            errors.append(f"{label}: {exc}")
            continue
        errors += check_configmap(root, label, objects, configmap, files)
        errors += check_deployment(label, objects, deployment, configmap)
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
        print(
            f"(a stale prod ConfigMap is fixed by {REGEN}; a new patch also needs "
            "its test kustomization entry and its TARGETS entry)",
            file=sys.stderr,
        )
        return 1
    print(f"nav-patch mounts agree with the {len(patch_files(args.root))} patch files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
