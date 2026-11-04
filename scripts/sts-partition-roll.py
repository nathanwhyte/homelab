#!/usr/bin/env python3
"""Release a held StatefulSet one ordinal at a time, highest first.

Usage: sts-partition-roll.py NAMESPACE STATEFULSET [--dry-run] [--timeout SECONDS]
           [--gate-container NAME --gate-command CMD --expect-nodes N]

Run after `helm-deploy.py --upgrade-chart` has left the StatefulSet at
partition == replicas. For each ordinal it lowers the partition to that ordinal
and waits until the pod there is on the StatefulSet's update revision (its
`controller-revision-hash` label equals `.status.updateRevision`, read only once
`.status.observedGeneration` has caught up with `.metadata.generation`) and
Ready. A bare Ready wait matches the old pod before it terminates and returns
early; the old pod carries the old revision, so it never passes. A pod already on
the update revision (an unchanged template) passes without being replaced. With
the gate options it then requires every pod's `CMD` output to list N nodes under
HEALTHY NODES before the next ordinal is released.

On a timeout it stops, leaves the partition where it is and prints the command
to resume. Every kubectl call is bounded (60 s, and inside a wait the time left), so a hung call is a
timeout too. Rerunning is safe: it first re-verifies the ordinal the partition
already released (ordinal 0 when the partition is 0), then continues.
Requires kubectl and Python 3.
"""

import argparse
import json
import shlex
import subprocess
import sys
import time


CALL_TIMEOUT = 60  # seconds; no single kubectl call may run longer


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
        # kube(args, timeout) runs `kubectl -n <namespace> *args` and returns
        # stdout, raising RollTimeout when it runs past `timeout` seconds.
        # gate is (container, command_words, expected_nodes) or None.
        self._kube, self.name, self.gate = kube, name, gate
        self.timeout, self.interval = timeout, interval
        self.sleep, self.clock, self.log = sleep, clock, log
        self.deadline = None

    def kube(self, args):
        """Every kubectl call is bounded: by CALL_TIMEOUT, and inside a wait
        also by the time left (at least a second, so the poll at the deadline
        can still run and the wait loop reports which stage timed out)."""
        limit = CALL_TIMEOUT
        if self.deadline is not None:
            limit = min(limit, max(self.deadline - self.clock(), 1))
        return self._kube(args, limit)

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
                # A pod still starting fails the exec; that is "not healthy
                # yet", retried until the deadline, not a reason to stop.
                return False
            if healthy_nodes(status) != expected:
                return False
        return True

    def update_revision(self):
        """The update revision, or None while the controller has not observed
        the latest spec (its updateRevision may still name the old revision)."""
        sts = self.statefulset()
        status = sts.get("status", {})
        generation = sts.get("metadata", {}).get("generation")
        observed = status.get("observedGeneration")
        if generation is not None and (observed is None or observed < generation):
            return None
        return status.get("updateRevision")

    def wait_released(self, ordinal, replicas):
        self.deadline = self.clock() + self.timeout
        try:
            while True:
                revision = self.update_revision()
                pod = self.pod(ordinal)
                labels = (pod or {}).get("metadata", {}).get("labels", {})
                if revision is None:
                    stage = "the controller to observe the latest spec"
                elif pod is None or labels.get("controller-revision-hash") != revision:
                    uid = (pod or {}).get("metadata", {}).get("uid")
                    stage = f"a pod on revision {revision} (still uid {uid})"
                elif not pod_ready(pod):
                    stage = "the updated pod to be Ready"
                elif not self.gate_open(replicas):
                    stage = "all nodes HEALTHY on every pod"
                else:
                    return
                if self.clock() >= self.deadline:
                    raise RollTimeout(
                        f"{self.name}-{ordinal}: timed out waiting for {stage}"
                    )
                self.sleep(self.interval)
        finally:
            self.deadline = None

    def run(self, dry_run=False):
        sts = self.statefulset()
        replicas = sts["spec"]["replicas"]
        partition = min(self.partition(), replicas)
        plan = list(range(partition - 1, -1, -1))
        self.log(f"{self.name}: partition {partition} of {replicas}; release {plan}")
        if dry_run:
            self.log("dry run: nothing was changed")
            return
        if partition < replicas:
            # The partition already released this ordinal (ordinal 0 when it is
            # 0): a previous run may have stopped before the pod was verified.
            self.log(f"{self.name}: verifying released ordinal {partition}")
            self.wait_released(partition, replicas)
        for ordinal in plan:
            pod = self.pod(ordinal)
            old_uid = pod["metadata"]["uid"] if pod else None
            self.log(
                f"{self.name}: releasing {self.name}-{ordinal} (old uid {old_uid})"
            )
            self.set_partition(ordinal)
            self.wait_released(ordinal, replicas)
            self.log(f"{self.name}-{ordinal}: updated pod Ready and cluster healthy")
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

    def kube(command, timeout=None):
        try:
            return subprocess.check_output(
                ["kubectl", "-n", args.namespace] + command, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            raise RollTimeout(
                f"kubectl {shlex.join(command)} ran past its {timeout}s limit"
            ) from None

    roller = Roller(kube, args.statefulset, gate, args.timeout)
    try:
        roller.run(args.dry_run)
    except (RollTimeout, subprocess.CalledProcessError, OSError, ValueError) as error:
        # The recovery instructions print first, so a hung API still leaves them.
        kind = "" if isinstance(error, RollTimeout) else "kubectl failed: "
        resume = shlex.join(["python3", sys.argv[0]] + (argv or sys.argv[1:]))
        print(f"sts-partition-roll: {kind}{error}", file=sys.stderr)
        print(f"Resume with:\n  {resume}", file=sys.stderr, flush=True)
        try:
            held = roller.partition()  # bounded by CALL_TIMEOUT
        except (RollTimeout, subprocess.CalledProcessError, OSError, ValueError):
            held = "unknown"
        print(f"Partition left at {held}.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
