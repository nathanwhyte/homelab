#!/usr/bin/env python3
"""Inspect or relinquish the legacy, count-only kubectl-patch ownership."""

import argparse
import json
import subprocess
import sys

NAME = "homelab-arc-compendium"
NAMESPACE = "arc-runners"
API = "actions.github.com/v1alpha1"
COUNTS = {"f:minRunners", "f:maxRunners"}


def handoff_patch(resource):
    """Only remove an obsolete co-owner; never alter values or Helm ownership."""
    metadata = resource["metadata"]
    if (
        resource.get("apiVersion") != API
        or resource.get("kind") != "AutoscalingRunnerSet"
        or metadata.get("name") != NAME
        or metadata.get("namespace") != NAMESPACE
        or metadata.get("annotations", {}).get("meta.helm.sh/release-name")
        != "compendium"
        or metadata.get("annotations", {}).get("meta.helm.sh/release-namespace")
        != NAMESPACE
    ):
        raise ValueError("unexpected scale set or Helm release identity")
    fields = metadata.get("managedFields", [])
    helm_counts = set()
    legacy = []
    for index, owner in enumerate(fields):
        spec = owner.get("fieldsV1", {}).get("f:spec", {})
        counts = COUNTS.intersection(spec)
        if not counts:
            continue
        if owner.get("apiVersion") != API or owner.get("subresource"):
            raise ValueError("unexpected count owner API or subresource")
        if owner.get("manager") == "helm" and owner.get("operation") == "Apply":
            helm_counts.update(counts)
        elif (
            owner.get("manager") == "kubectl-patch"
            and owner.get("operation") == "Update"
            and owner.get("fieldsType") == "FieldsV1"
            and owner["fieldsV1"] == {"f:spec": {key: {} for key in counts}}
        ):
            legacy.append(index)
        else:
            raise ValueError("unexpected count owner; explicit investigation required")
    if helm_counts != COUNTS:
        raise ValueError("Helm Apply must already own both counts; refusing adoption")
    if not legacy:
        return []
    if not metadata.get("resourceVersion") or not metadata.get("uid"):
        raise ValueError("missing resource version or UID")
    return [
        {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": metadata["resourceVersion"],
        },
        *[
            {"op": "remove", "path": f"/metadata/managedFields/{index}"}
            for index in reversed(legacy)
        ],
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply", action="store_true", help="apply the displayed metadata-only handoff"
    )
    mode.add_argument(
        "--check", action="store_true", help="require Helm-only count ownership"
    )
    args = parser.parse_args()
    target = ["autoscalingrunnerset", NAME, "-n", NAMESPACE]
    try:
        resource = json.loads(
            subprocess.check_output(
                ["kubectl", "get", *target, "-o", "json", "--show-managed-fields"],
                text=True,
            )
        )
        patch = handoff_patch(resource)
        if args.check:
            if patch:
                raise ValueError(
                    "legacy count co-owner remains; preview the ownership handoff first"
                )
            print("Helm Apply owns both counts without a foreign co-owner.")
        elif args.apply and patch:
            subprocess.run(
                [
                    "kubectl",
                    "patch",
                    *target,
                    "--type=json",
                    "--patch",
                    json.dumps(patch),
                ],
                check=True,
            )
            print("Legacy count-only co-owner removed; runner values were not changed.")
        else:
            print(json.dumps(patch, indent=2))
        return 0
    except (KeyError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Ownership preflight failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
