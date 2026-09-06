#!/usr/bin/env python3
"""Apply values using the installed chart version, with explicit first-install resolution.

Usage: helm-deploy.py RELEASE NAMESPACE CHART [--dry-run | --diff] [Helm value flags...]
Chart upgrades are separate, deliberate Helm operations. Requires Helm and Python 3.
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
    extra = [arg for arg in extra if arg not in ("--dry-run", "--diff")]
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

    local = is_local_chart_ref(chart)
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
    if version is not None and resolved != version:
        raise ValueError(
            f"Chart version mismatch: deployed {version}, supplied {resolved}"
        )
    version = resolved
    print(
        f"{namespace}/{release}: {'first install resolved to' if first_install else 'reusing deployed'} chart {chart_name} {version}",
        flush=True,
    )

    command = ["helm", "diff", "upgrade"] if diff else ["helm", "upgrade", "--install"]
    command += [release, chart, "--namespace", namespace, "--version", version]
    command += extra
    if dry_run:
        command += ["--dry-run=server", "--hide-secret"]
    if diff:
        command += ["--suppress-secrets"]
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
