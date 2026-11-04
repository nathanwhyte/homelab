#!/usr/bin/env python3
"""Apply values using the installed chart version, with explicit first-install resolution.

Usage: helm-deploy.py RELEASE NAMESPACE CHART [--dry-run | --diff] [Helm value flags...]
       helm-deploy.py RELEASE NAMESPACE LOCAL_CHART --upgrade-chart \\
           --hold-statefulset NAME [--dry-run | --diff] [Helm value flags...]
Chart upgrades are separate, deliberate Helm operations: a chart version change
is refused unless --upgrade-chart is given (local charts only). That mode prints
the partition plan, runs a server dry run and a diff, patches the StatefulSet to
hold every pod (partition = replicas), then upgrades. Requires Helm and Python 3.
"""

import json
from pathlib import Path
import re
import subprocess
import sys


VERSION = r"v?[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"


def output(args):
    return subprocess.check_output(args, text=True)


def is_local_chart_ref(chart):
    """A chart ref names a local path only if it says so explicitly: absolute,
    or starting with `./` or `../`. Anything else — e.g. `harbor/harbor` or
    `grafana/loki` — is a repo/alias ref, even if a same-named directory
    happens to exist in the current working directory."""
    return Path(chart).is_absolute() or chart.startswith(("./", "../"))


def metadata_scalar(text, field, pattern):
    # Helm serializes chart metadata as YAML. Accept only a simple scalar;
    # reject exotic YAML instead of interpreting it or guessing a version.
    matches = re.findall(r"^" + field + r":\s*(.*?)\s*$", text, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError(f"Expected one chart metadata {field}")
    value = matches[0]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if not re.fullmatch(pattern, value):
        raise ValueError(f"Invalid chart metadata {field}")
    return value


def installed_version(release, namespace, chart_name):
    # Explicit status flags work on Helm 3 and 4 (Helm 4 removed --all).
    rows = json.loads(
        output(
            [
                "helm",
                "list",
                "--namespace",
                namespace,
                "--filter",
                "^" + re.escape(release) + "$",
                "--deployed",
                "--failed",
                "--pending",
                "--uninstalling",
                "--uninstalled",
                "--superseded",
                "--output",
                "json",
            ]
        )
    )
    if not isinstance(rows, list):
        raise ValueError("Helm list did not return a JSON array")
    if not rows:
        return None
    if len(rows) != 1 or rows[0].get("name") != release:
        raise ValueError("Helm release lookup was ambiguous")
    row = rows[0]
    if row.get("namespace") != namespace or row.get("status") != "deployed":
        raise ValueError(
            "Release is not deployed in the expected namespace; resolve its state first"
        )
    match = re.fullmatch(
        re.escape(chart_name) + "-(" + VERSION + ")", row.get("chart", "")
    )
    if not match:
        raise ValueError(
            "Installed chart identity/version does not match the requested chart"
        )
    return match[1]


def take_option(args, name):
    """Remove `name VALUE` or `name=VALUE` from args; return (value, rest)."""
    rest, value, index = [], None, 0
    while index < len(args):
        arg = args[index]
        if arg == name or arg.startswith(name + "="):
            if value is not None:
                raise ValueError(f"{name} given more than once")
            if arg == name:
                if index + 1 >= len(args):
                    raise ValueError(f"{name} needs a value")
                value = args[index + 1]
                index += 1
            else:
                value = arg[len(name) + 1 :]
            if not value:
                raise ValueError(f"{name} needs a non-empty value")
        else:
            rest.append(arg)
        index += 1
    return value, rest


def helm_command(kind, release, chart, namespace, version, extra):
    command = (
        ["helm", "diff", "upgrade"]
        if kind == "diff"
        else ["helm", "upgrade", "--install"]
    )
    command += [release, chart, "--namespace", namespace, "--version", version]
    command += extra
    if kind == "dry":
        command += ["--dry-run=server", "--hide-secret"]
    if kind == "diff":
        command += ["--suppress-secrets"]
    return command


def json_objects(text):
    """The objects in `kubectl … -o json` output, which concatenates one JSON
    object per input document instead of returning a list."""
    decoder, index, objects = json.JSONDecoder(), 0, []
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            return objects
        value, index = decoder.raw_decode(text, index)
        objects.append(value)


def check_rendered_hold(release, chart, namespace, version, extra, statefulset):
    """Refuse an upgrade whose three-way merge could touch the StatefulSet's
    update strategy, and with it the partition hold.

    Helm patches the live object with the difference between the deployed
    manifest and the target one, so both are read: the target from the
    upgrade's own server dry run (`helm upgrade --dry-run=server -o json`,
    rendered in upgrade context: `.Release.IsUpgrade`, `lookup`, unlike `helm
    template`; `--hide-secret` leaves Secrets out), the deployed one from `helm
    get manifest`. Each must render the strategy as absent, `{}` or exactly
    `{type: RollingUpdate}`, and the two must be equal, so the merge sends no
    strategy change at all. Both are parsed by kubectl's own YAML reader
    (`create --dry-run=client -o json`), never here, and neither is printed.
    """
    release_json = output(
        helm_command("dry", release, chart, namespace, version, extra)
        + ["--output", "json"]
    )
    manifest = json.loads(release_json).get("manifest")
    if not isinstance(manifest, str) or not manifest.strip():
        raise ValueError("The upgrade dry run returned no manifest to check")
    target = manifest_strategy(manifest, statefulset, "chart renders")
    # Helm's three-way merge diffs the deployed manifest against the target: a
    # strategy the deployed chart set and the new one drops is sent as a null,
    # which deletes the live partition. So the deployed manifest is checked
    # too, and the two must agree.
    deployed = output(["helm", "get", "manifest", release, "--namespace", namespace])
    if not deployed.strip():
        raise ValueError(f"helm get manifest returned nothing for release {release}")
    current = manifest_strategy(deployed, statefulset, "deployed release has")
    if target != current:
        raise ValueError(
            f"The upgrade changes statefulset/{statefulset}'s updateStrategy from "
            f"{describe(current)} to {describe(target)}, which Helm would "
            "apply over the partition hold; upgrade it by hand"
        )


# What manifest_strategy returns for no updateStrategy key: an object no
# parsed JSON value can equal.
ABSENT = object()


def describe(strategy):
    return "absent" if strategy is ABSENT else json.dumps(strategy)


def manifest_strategy(manifest, statefulset, source):
    """The named StatefulSet's updateStrategy in a rendered manifest, refusing
    any shape that could replace a live partition."""
    converted = subprocess.run(
        ["kubectl", "create", "--dry-run=client", "-o", "json", "-f", "-"],
        input=manifest,
        capture_output=True,
        text=True,
    )
    if converted.returncode != 0:
        # Only kubectl's first error line, cut short: enough to name the
        # problem without echoing the manifest, which (for the deployed
        # release) still holds rendered Secrets.
        lines = converted.stderr.strip().splitlines() or ["no error output"]
        raise ValueError(f"kubectl could not parse the manifest: {lines[0][:300]}")
    matches = [
        item
        for item in json_objects(converted.stdout)
        if item.get("kind") == "StatefulSet"
        and item.get("metadata", {}).get("name") == statefulset
    ]
    if len(matches) != 1:
        raise ValueError(
            f"The {source} {len(matches)} StatefulSet(s) named {statefulset}; "
            "expected exactly one to hold"
        )
    # An allowlist of exact shapes, never a search for bad ones: any other
    # value, a null included (a strategic merge deletes the live field), could
    # replace the hold.
    # Absence stays distinct from `{}`: Helm sends a key the deployed manifest
    # had and the target lacks as a null, whatever its value, so the caller's
    # equality check must see the difference.
    spec = matches[0].get("spec") or {}
    strategy = spec.get("updateStrategy", ABSENT)
    if strategy is not ABSENT and strategy not in ({}, {"type": "RollingUpdate"}):
        raise ValueError(
            f"The {source} updateStrategy {json.dumps(strategy)} for "
            f"statefulset/{statefulset}, which would replace the partition hold; "
            "upgrade it by hand"
        )
    return strategy


def kubectl_scalar(namespace, statefulset, path):
    return output(
        ["kubectl", "-n", namespace, "get", "statefulset", statefulset]
        + ["-o", "jsonpath={" + path + "}"]
    ).strip()


def upgrade_with_hold(
    release, namespace, chart, statefulset, version, deployed, extra, dry_run, diff
):
    """Chart upgrade for a StatefulSet: preflight, then hold every pod, then upgrade.

    The hold is a `kubectl patch` of updateStrategy.rollingUpdate.partition to
    the replica count, applied before `helm upgrade`. Helm's three-way merge only
    sends fields that differ between the old and new chart manifests, so a
    partition set on the live object survives the upgrade as long as both
    manifests leave the strategy alone in the same way, which
    check_rendered_hold enforces beforehand; the hold is also verified after the
    upgrade. A post-renderer would
    be atomic but needs YAML editing, and this helper is standard-library only.
    """
    replicas = int(kubectl_scalar(namespace, statefulset, ".spec.replicas"))
    ordinals = ", ".join(str(n) for n in range(replicas - 1, -1, -1))
    print(
        f"{namespace}/{release}: chart upgrade {deployed} -> {version}; "
        f"statefulset/{statefulset} has {replicas} replicas",
        flush=True,
    )
    print(
        f"{namespace}/{release}: partition plan: hold at {replicas} during the "
        f"upgrade (no pod rolls), then release {ordinals} one at a time",
        flush=True,
    )
    # Both preflights run in every mode, --diff included, and both are reads:
    # the server dry run (which also yields the manifest the hold check reads)
    # and the diff.
    check_rendered_hold(release, chart, namespace, version, extra, statefulset)
    subprocess.run(
        helm_command("diff", release, chart, namespace, version, extra), check=True
    )
    if dry_run or diff:
        print(
            f"{namespace}/{release}: upgrade preflight passed; nothing was changed",
            flush=True,
        )
        return
    patch = {"spec": {"updateStrategy": {"type": "RollingUpdate"}}}
    patch["spec"]["updateStrategy"]["rollingUpdate"] = {"partition": replicas}
    subprocess.run(
        ["kubectl", "-n", namespace, "patch", "statefulset", statefulset]
        + ["--type", "merge", "-p", json.dumps(patch)],
        check=True,
    )
    try:
        subprocess.run(
            helm_command("upgrade", release, chart, namespace, version, extra),
            check=True,
        )
    except subprocess.CalledProcessError:
        # A failed upgrade must still leave the hold; say so either way.
        try:
            held = kubectl_scalar(
                namespace, statefulset, ".spec.updateStrategy.rollingUpdate.partition"
            )
        except subprocess.CalledProcessError:
            held = "unreadable"
        print(
            f"{namespace}/{release}: helm upgrade failed; statefulset/{statefulset} "
            f"partition is {held or 'unset'} (hold {replicas})",
            file=sys.stderr,
            flush=True,
        )
        raise
    held = kubectl_scalar(
        namespace, statefulset, ".spec.updateStrategy.rollingUpdate.partition"
    )
    if held != str(replicas):
        raise ValueError(
            f"Upgrade replaced the partition hold (now {held or 'unset'}, expected "
            f"{replicas}); check which pods rolled before continuing"
        )
    print(
        f"{namespace}/{release}: upgraded to {version}; statefulset/{statefulset} "
        f"held at partition {replicas}. Release pods with sts-partition-roll.py",
        flush=True,
    )


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3:
        raise ValueError(__doc__)
    release, namespace, chart = args[:3]
    extra = args[3:]
    dry_run = "--dry-run" in extra
    diff = "--diff" in extra
    if dry_run and diff:
        raise ValueError("Choose --dry-run or --diff")
    upgrade = "--upgrade-chart" in extra
    extra = [
        arg for arg in extra if arg not in ("--dry-run", "--diff", "--upgrade-chart")
    ]
    hold, extra = take_option(extra, "--hold-statefulset")
    if upgrade != (hold is not None):
        raise ValueError("--upgrade-chart and --hold-statefulset go together")
    # Callers supply values and deployment options, never a competing identity
    # or a flag that turns a requested simulation back into a mutation.
    forbidden = (
        "--version",
        "--namespace",
        "-n",
        "--dry-run",
        "--repo",
        "--kube-context",
        "--kubeconfig",
    )
    if any(
        arg == key or arg.startswith(key + "=") for arg in extra for key in forbidden
    ):
        raise ValueError(
            "Version, namespace, context and dry-run flags are owned by helm-deploy.py"
        )
    # --force (and Helm 4's --force-replace) replaces objects instead of
    # patching them, which would drop the live-only partition hold.
    if upgrade and any(arg.startswith("--force") for arg in extra):
        raise ValueError("--upgrade-chart cannot be combined with --force")

    local = is_local_chart_ref(chart)
    if upgrade and not local:
        raise ValueError("--upgrade-chart needs a local chart path")
    if local and not Path(chart).exists():
        raise ValueError(f"Local chart path does not exist: {chart}")
    metadata = output(["helm", "show", "chart", chart]) if local else None
    chart_name = (
        metadata_scalar(metadata, "name", r"[a-z0-9][a-z0-9.-]*")
        if local
        else chart.rsplit("/", 1)[-1]
    )
    version = installed_version(release, namespace, chart_name)
    first_install = version is None
    if metadata is None:
        command = ["helm", "show", "chart", chart]
        if version:
            command += ["--version", version]
        metadata = output(command)
    if metadata_scalar(metadata, "name", r"[a-z0-9][a-z0-9.-]*") != chart_name:
        raise ValueError("Resolved chart name mismatch")
    resolved = metadata_scalar(metadata, "version", VERSION)
    if upgrade and version is None:
        raise ValueError("--upgrade-chart needs a deployed release; nothing to upgrade")
    if version is not None and resolved != version and not upgrade:
        raise ValueError(
            f"Chart version mismatch: deployed {version}, supplied {resolved}"
        )
    deployed, version = version, resolved
    if upgrade:
        upgrade_with_hold(
            release, namespace, chart, hold, version, deployed, extra, dry_run, diff
        )
        return
    print(
        f"{namespace}/{release}: {'first install resolved to' if first_install else 'reusing deployed'} chart {chart_name} {version}",
        flush=True,
    )

    kind = "diff" if diff else "dry" if dry_run else "upgrade"
    command = helm_command(kind, release, chart, namespace, version, extra)
    # Rendered ConfigMaps and chart NOTES can contain credentials too. Do not
    # emit any successful simulation's manifest output, even with hide-secret.
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL if dry_run else None)
    if dry_run:
        print(f"{namespace}/{release}: server dry-run passed; chart version {version}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, subprocess.CalledProcessError, OSError) as error:
        # CalledProcessError can embed --set credentials from argv.
        print(
            f"helm-deploy: {error if isinstance(error, ValueError) else 'Helm command failed; deployment stopped'}",
            file=sys.stderr,
        )
        sys.exit(1)
