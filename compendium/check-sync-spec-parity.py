#!/usr/bin/env -S uv run --with pyyaml --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""The CronJob and the manual Job template must run the same pod.

`compendium-sync-cronjob.yaml` (IMPR-1154) duplicates the pod spec of
`compendium-sync-job.template.yaml` because Kubernetes gives no way to share
one. Duplication drifts: a bumped image digest, a new env var, or a changed
ServiceAccount applied to one and not the other would have the scheduled sync
running under different conditions than the manual one — and the difference
would only surface as a confusing production failure.

This asserts the two stay equivalent. Two differences are legitimate and are
allowed by name:

  JOB_NAME   the template takes it from envsubst at dispatch; the CronJob reads
             the controller-set `batch.kubernetes.io/job-name` label.
  SYNC_ARGS  the template leaves it for the operator to pass; the CronJob fixes
             the canonical arguments, since a scheduled run has no operator.

Usage:
  compendium/check-sync-spec-parity.py            # from the repo root
  compendium/check-sync-spec-parity.py --verbose  # show each field checked

Exit 1 on any drift.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "compendium-sync-job.template.yaml"
CRONJOB = HERE / "compendium-sync-cronjob.yaml"

# Differ by design; see the module docstring.
EXEMPT_ENV = {"JOB_NAME", "SYNC_ARGS"}


def render_template() -> dict:
    """Substitute only JOB_NAME, so ${SYNC_ARGS} stays the literal shell
    reference the CronJob also carries and the two scripts compare equal."""
    out = subprocess.run(
        ["envsubst", "${JOB_NAME}"],
        input=TEMPLATE.read_text(),
        capture_output=True,
        text=True,
        check=True,
        # Merge rather than replace: a bare dict would drop PATH and envsubst
        # would not be found.
        env={**os.environ, "JOB_NAME": "parity-check"},
    ).stdout
    return yaml.safe_load(out)


def pod_specs() -> tuple[dict, dict]:
    job = render_template()
    cron = yaml.safe_load(CRONJOB.read_text())
    return (
        job["spec"]["template"]["spec"],
        cron["spec"]["jobTemplate"]["spec"]["template"]["spec"],
    )


def compare(verbose: bool) -> list[str]:
    jp, cp = pod_specs()
    jc, cc = jp["containers"][0], cp["containers"][0]
    fields = [
        ("image digest", jc["image"], cc["image"]),
        ("command", jc["command"], cc["command"]),
        ("args script", jc["args"][0], cc["args"][0]),
        ("resources", jc["resources"], cc["resources"]),
        ("volumeMounts", jc["volumeMounts"], cc["volumeMounts"]),
        ("serviceAccountName", jp["serviceAccountName"], cp["serviceAccountName"]),
        ("restartPolicy", jp["restartPolicy"], cp["restartPolicy"]),
        (
            "PVC claimName",
            jp["volumes"][0]["persistentVolumeClaim"]["claimName"],
            cp["volumes"][0]["persistentVolumeClaim"]["claimName"],
        ),
    ]
    findings = []
    for name, a, b in fields:
        if a == b:
            if verbose:
                print(f"ok    {name}")
        else:
            findings.append(f"{name}: template={a!r} cronjob={b!r}")

    job_env = {e["name"]: e for e in jc["env"]}
    cron_env = {e["name"]: e for e in cc["env"]}
    missing = sorted(set(job_env) - set(cron_env))
    if missing:
        findings.append(f"env vars in the template but not the CronJob: {missing}")
    elif verbose:
        print("ok    env keys (CronJob covers every template key)")

    for name in sorted(set(job_env) & set(cron_env) - EXEMPT_ENV):
        if job_env[name] != cron_env[name]:
            findings.append(
                f"env {name}: template={job_env[name]!r} cronjob={cron_env[name]!r}"
            )
        elif verbose:
            print(f"ok    env {name}")
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--verbose", action="store_true", help="show each field checked")
    args = ap.parse_args()

    for path in (TEMPLATE, CRONJOB):
        if not path.is_file():
            print(f"ERROR: {path} not found", file=sys.stderr)
            return 1

    findings = compare(args.verbose)
    for finding in findings:
        print(f"ERROR: {finding}", file=sys.stderr)
    if findings:
        print(
            f"\n{len(findings)} drift(s) between the sync CronJob and the Job "
            "template. They must run the same pod — update both, or add the "
            "field to EXEMPT_ENV if the difference is deliberate.",
            file=sys.stderr,
        )
        return 1
    print("sync spec parity: CronJob and Job template agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
