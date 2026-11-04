#!/usr/bin/env python3
"""Release a held StatefulSet one ordinal at a time, highest first.

Usage: sts-partition-roll.py NAMESPACE STATEFULSET [--dry-run] [--timeout SECONDS]
           [--gate-container NAME --gate-command CMD --expect-nodes N]

Run after `helm-deploy.py --upgrade-chart` has left the StatefulSet at
partition == replicas. For each ordinal it records the pod's UID, lowers the
partition to that ordinal, and waits for a pod with a DIFFERENT UID that is
Ready. A bare Ready wait matches the old pod before it terminates and returns
early. With the gate options it then requires every pod's `CMD` output to list
N nodes under HEALTHY NODES before the next ordinal is released.

On a timeout it stops, leaves the partition where it is and prints the command
to resume. Rerunning is safe: it first re-verifies the ordinal already released.
Requires kubectl and Python 3.
"""

import argparse
import json
import shlex
import subprocess
import sys
import time


class RollTimeout(Exception):
    pass


def healthy_nodes(status):
    """Count node rows in the HEALTHY NODES section of `garage status`."""
    rows, inside = 0, False
    for line in status.splitlines():
        if line.startswith("===="):
            inside = "HEALTHY NODES" in line
            continue
        if inside and line.strip():
            rows += 1
    return max(rows - 1, 0)  # the first row is the column header


def pod_ready(pod):
    if pod["metadata"].get("deletionTimestamp"):
        return False
    conditions = pod.get("status", {}).get("conditions", [])
    return any(c["type"] == "Ready" and c["status"] == "True" for c in conditions)


class Roller:
    def __init__(
        self,
        kube,
        name,
        gate=None,
        timeout=900,
        interval=5,
        sleep=time.sleep,
        clock=time.monotonic,
        log=print,
    ):
        # kube(args) runs `kubectl -n <namespace> *args` and returns stdout.
        # gate is (container, command_words, expected_nodes) or None.
        self.kube, self.name, self.gate = kube, name, gate
        self.timeout, self.interval = timeout, interval
        self.sleep, self.clock, self.log = sleep, clock, log

    def statefulset(self):
        return json.loads(self.kube(["get", "statefulset", self.name, "-o", "json"]))

    def partition(self):
        strategy = self.statefulset()["spec"].get("updateStrategy", {})
        return (strategy.get("rollingUpdate") or {}).get("partition", 0)

    def pod(self, ordinal):
        text = self.kube(
            ["get", "pod", f"{self.name}-{ordinal}", "--ignore-not-found", "-o", "json"]
        )
        return json.loads(text) if text.strip() else None

    def set_partition(self, value):
        patch = {"spec": {"updateStrategy": {"rollingUpdate": {"partition": value}}}}
        self.kube(
            ["patch", "statefulset", self.name, "--type", "merge"]
            + ["-p", json.dumps(patch)]
        )

    def gate_open(self, replicas):
        if self.gate is None:
            return True
        container, command, expected = self.gate
        for ordinal in range(replicas):
            try:
                status = self.kube(
                    ["exec", f"{self.name}-{ordinal}", "-c", container, "--"] + command
                )
            except subprocess.CalledProcessError:
                return False
            if healthy_nodes(status) != expected:
                return False
        return True

    def wait_released(self, ordinal, old_uid, replicas):
        deadline = self.clock() + self.timeout
        stage = "a new pod"
        while True:
            pod = self.pod(ordinal)
            if pod is None or pod["metadata"]["uid"] == old_uid:
                stage = f"a new pod (still uid {old_uid})"
            elif not pod_ready(pod):
                stage = "the new pod to be Ready"
            elif not self.gate_open(replicas):
                stage = "all nodes HEALTHY on every pod"
            else:
                return
            if self.clock() >= deadline:
                raise RollTimeout(
                    f"{self.name}-{ordinal}: timed out waiting for {stage}"
                )
            self.sleep(self.interval)

    def run(self, dry_run=False):
        sts = self.statefulset()
        replicas = sts["spec"]["replicas"]
        partition = self.partition()
        if partition == 0:
            self.log(f"{self.name}: partition is 0; nothing is held")
            return
        plan = list(range(min(partition, replicas) - 1, -1, -1))
        self.log(f"{self.name}: partition {partition} of {replicas}; release {plan}")
        if dry_run:
            self.log("dry run: nothing was changed")
            return
        if partition < replicas:
            # A previous run released this ordinal and stopped waiting for it.
            pod = self.pod(partition)
            current = pod is not None and (
                pod["metadata"].get("labels", {}).get("controller-revision-hash")
                == sts["status"].get("updateRevision")
            )
            old_uid = (
                None if current else (pod or {"metadata": {}})["metadata"].get("uid")
            )
            self.log(f"{self.name}: re-verifying released ordinal {partition}")
            self.wait_released(partition, old_uid, replicas)
        for ordinal in plan:
            pod = self.pod(ordinal)
            old_uid = pod["metadata"]["uid"] if pod else None
            self.log(
                f"{self.name}: releasing {self.name}-{ordinal} (old uid {old_uid})"
            )
            self.set_partition(ordinal)
            self.wait_released(ordinal, old_uid, replicas)
            self.log(f"{self.name}-{ordinal}: new pod Ready and cluster healthy")
        self.log(f"{self.name}: all ordinals released")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("namespace")
    parser.add_argument("statefulset")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--gate-container")
    parser.add_argument("--gate-command")
    parser.add_argument("--expect-nodes", type=int)
    args = parser.parse_args(argv)
    gate = None
    if args.gate_command:
        if not args.gate_container or not args.expect_nodes:
            parser.error("--gate-command needs --gate-container and --expect-nodes")
        gate = (args.gate_container, shlex.split(args.gate_command), args.expect_nodes)

    def kube(command):
        return subprocess.check_output(
            ["kubectl", "-n", args.namespace] + command, text=True
        )

    roller = Roller(kube, args.statefulset, gate, args.timeout)
    try:
        roller.run(args.dry_run)
    except RollTimeout as error:
        try:
            held = roller.partition()
        except (subprocess.CalledProcessError, ValueError):
            held = "unknown"
        resume = shlex.join(["python3", sys.argv[0]] + (argv or sys.argv[1:]))
        print(f"sts-partition-roll: {error}", file=sys.stderr)
        print(f"Partition left at {held}. Resume with:\n  {resume}", file=sys.stderr)
        return 1
    except (subprocess.CalledProcessError, OSError, ValueError) as error:
        print(f"sts-partition-roll: kubectl failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
