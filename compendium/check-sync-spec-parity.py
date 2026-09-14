#!/usr/bin/env -S uv run --with pyyaml --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""The CronJob and the manual Job template must run the same pod.

`compendium-sync-cronjob.yaml` (IMPR-1154) duplicates the pod spec of
`compendium-sync-job.template.yaml` because Kubernetes gives no way to share
one. Duplication drifts: a bumped image digest, a new env var, a stray init
container, or a changed ServiceAccount applied to one and not the other would
have the scheduled sync running under different conditions than the manual one
— and the difference would only surface as a confusing production failure.

This compares the two pod specs WHOLE, structurally, rather than checking a
list of fields someone remembered to enumerate. A field-whitelist version of
this script shipped first and silently accepted an added init container, an
impossible nodeSelector, and a CronJob-only env var, because none of those were
on the list. Anything not explicitly exempted below is compared.

Exempt, because they differ by design:

  env JOB_NAME    the template takes it from envsubst at dispatch; the CronJob
                  reads the controller-set `batch.kubernetes.io/job-name` label.
  env SYNC_ARGS   the template leaves it for the operator to pass; the CronJob
                  fixes the canonical arguments, since a scheduled run has no
                  operator.
  backoffLimit    Job-level. The template retries 3 times; a scheduled run's
                  next attempt is one slot away, so the CronJob retries once.

Env lists are normalized to name-keyed maps before comparison, so ordering is
not treated as drift — but an entry present on only one side is.

Usage:
  compendium/check-sync-spec-parity.py            # from the repo root
  compendium/check-sync-spec-parity.py --verbose  # show what was compared

Exit 1 on any drift.
"""

from __future__ import annotations

import argparse
import copy
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "compendium-sync-job.template.yaml"
CRONJOB = HERE / "compendium-sync-cronjob.yaml"

EXEMPT_ENV = {"JOB_NAME", "SYNC_ARGS"}
EXEMPT_JOB_FIELDS = {"backoffLimit"}

# Guards that must be present and identical on both sides. Equality alone would
# be satisfied by both sides dropping them, so they are asserted by value.
REQUIRED_ENV = {
    "OV_SYNC_STATE": "/state/compendium-sync-state.json",
    "OV_SYNC_LOCK": "/state/compendium-sync.lock",
}


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


def normalize_pod_spec(spec: dict) -> dict:
    """Env lists -> name-keyed maps, minus the exempt names.

    Applied to every container list a pod spec can carry, so an init or
    ephemeral container added to one side is compared like any other field.
    """
    spec = copy.deepcopy(spec)
    for key in ("containers", "initContainers", "ephemeralContainers"):
        for container in spec.get(key) or []:
            env = container.pop("env", None)
            if env is not None:
                container["env"] = {
                    e["name"]: e for e in env if e["name"] not in EXEMPT_ENV
                }
    return spec


def diff(a: Any, b: Any, path: str = "") -> list[str]:
    """Structural diff reporting dotted paths to each difference."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b)):
            where = f"{path}.{key}" if path else key
            if key not in a:
                out.append(
                    f"{where}: missing from the Job template, CronJob has {b[key]!r}"
                )
            elif key not in b:
                out.append(
                    f"{where}: missing from the CronJob, Job template has {a[key]!r}"
                )
            else:
                out.extend(diff(a[key], b[key], where))
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: length differs — template {len(a)}, CronJob {len(b)}"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out.extend(diff(x, y, f"{path}[{i}]"))
        return out
    if a != b:
        return [f"{path}: template={a!r} cronjob={b!r}"]
    return []


def check_required_env(spec: dict, side: str) -> list[str]:
    container = (spec.get("containers") or [{}])[0]
    env = container.get("env") or {}
    findings = []
    for name, want in REQUIRED_ENV.items():
        entry = env.get(name)
        if entry is None:
            findings.append(f"{side}: required env {name} is missing")
        elif entry.get("value") != want:
            findings.append(
                f"{side}: env {name} must be {want!r}, found {entry.get('value')!r}"
            )
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--verbose", action="store_true", help="show what was compared")
    args = ap.parse_args()

    for path in (TEMPLATE, CRONJOB):
        if not path.is_file():
            print(f"ERROR: {path} not found", file=sys.stderr)
            return 1

    job = render_template()
    cron = yaml.safe_load(CRONJOB.read_text())
    job_spec = job["spec"]
    cron_job_spec = cron["spec"]["jobTemplate"]["spec"]

    jp = normalize_pod_spec(job_spec["template"]["spec"])
    cp = normalize_pod_spec(cron_job_spec["template"]["spec"])

    findings = diff(jp, cp, "podSpec")

    # Job-level settings, minus the ones that differ by design and the pod
    # template already compared above.
    skip = EXEMPT_JOB_FIELDS | {"template"}
    findings += diff(
        {k: v for k, v in job_spec.items() if k not in skip},
        {k: v for k, v in cron_job_spec.items() if k not in skip},
        "jobSpec",
    )

    findings += check_required_env(jp, "Job template")
    findings += check_required_env(cp, "CronJob")

    if args.verbose and not findings:
        containers = len(jp.get("containers") or [])
        env_count = len((jp.get("containers") or [{}])[0].get("env") or {})
        print(
            f"compared: whole pod spec ({containers} container(s), {env_count} env vars)"
        )
        print(f"compared: job spec minus {sorted(skip)}")
        print(
            f"asserted: {', '.join(sorted(REQUIRED_ENV))} present and correct on both"
        )
        print(f"exempt:   env {sorted(EXEMPT_ENV)}, job {sorted(EXEMPT_JOB_FIELDS)}")

    for finding in findings:
        print(f"ERROR: {finding}", file=sys.stderr)
    if findings:
        print(
            f"\n{len(findings)} drift(s) between the sync CronJob and the Job "
            "template. They must run the same pod — update both, or add the "
            "field to the exempt sets if the difference is deliberate.",
            file=sys.stderr,
        )
        return 1
    print("sync spec parity: CronJob and Job template agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
