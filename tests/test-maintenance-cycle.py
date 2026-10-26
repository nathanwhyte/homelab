#!/usr/bin/env python3
"""Exercise both maintenance commands against one persistent fake cluster."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import json
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = ROOT / "scripts/node-maintenance.sh"
OUTAGE = ROOT / "scripts/timmy-offline.sh"
OLD_BOOT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
NEW_BOOT = "11111111-2222-3333-4444-555555555555"
THIRD_BOOT = "99999999-8888-7777-6666-555555555555"
SILENCE = "0b5c1f0e-7a3d-4c51-9e2b-1d2f3a4b5c6d"


def fake_command(tool, args):
    """Only implement observed external commands; an unknown command fails."""
    state_file = Path(os.environ["FAKE_CLUSTER_STATE"])
    state = json.loads(state_file.read_text())
    with Path(os.environ["FAKE_CLUSTER_LOG"]).open("a") as log:
        log.write(json.dumps({"tool": tool, "args": args}) + "\n")

    def save():
        state_file.write_text(json.dumps(state))

    def unexpected():
        state.setdefault("unexpected", []).append([tool, args])
        save()
        print(f"unexpected fake command: {tool} {args}", file=sys.stderr)
        return 91

    if tool == "sleep":
        return unexpected()
    if tool == "ssh":
        host, command = args[-2:]
        node = host.removesuffix("-lan")
        if node not in state["nodes"]:
            return unexpected()
        if command == "[ -f /var/run/reboot-required ]":
            return 0 if state["nodes"][node]["reboot_required"] else 1
        if command.startswith("if [ -f /var/run/reboot-required ];"):
            status = (
                "required" if state["nodes"][node]["reboot_required"] else "current"
            )
            print(status + "\tfixture-kernel")
            return 0
        if command == "cat /proc/sys/kernel/random/boot_id":
            print(state["nodes"][node]["boot"])
            return 0
        if command == "uname -r":
            print("fixture-kernel")
            return 0
        return unexpected()

    args = [arg for arg in args if not arg.startswith("--request-timeout=")]
    namespace = "default"
    if "-n" in args:
        i = args.index("-n")
        namespace = args[i + 1]
        args = args[:i] + args[i + 2 :]
    verb, resource = args[:2]
    if verb == "get":
        if resource == "--raw=/readyz":
            print("ok")
        elif resource == "nodes":
            if "json" in args:
                print(
                    json.dumps(
                        {
                            "items": [
                                {
                                    "metadata": {"name": name},
                                    "spec": {"unschedulable": node["cordoned"]},
                                    "status": {"allocatable": {"memory": "64Gi"}},
                                }
                                for name, node in state["nodes"].items()
                            ]
                        }
                    )
                )
            else:
                for name, node in state["nodes"].items():
                    print(
                        name,
                        "Ready,SchedulingDisabled" if node["cordoned"] else "Ready",
                    )
        elif resource == "node":
            query = args[-1]
            if state.get("fail_node_read") in ("all", query):
                return 1
            node = state["nodes"][args[2]]
            if "unschedulable" in query:
                print(str(node["cordoned"]).lower())
            elif "Ready" in query:
                print(str(node["ready"]))
            else:
                return unexpected()
        elif resource == "pdb":
            print('{"items":[]}')
        elif resource == "pods":
            print(json.dumps({"items": state.get("pods", [])}))
        elif resource == "jobs" and namespace == "compendium":
            print("active-sync" if state.get("active_job") else "", end="")
        elif resource.startswith("volumes.longhorn.io"):
            parked = state["replicas"]["statefulset/garage/garage"] == 0
            volume = {
                "kind": "Volume",
                "metadata": {"name": "garage-volume"},
                "spec": {"numberOfReplicas": 1},
                "status": {
                    "state": "detached" if parked else "attached",
                    "robustness": "healthy",
                    "currentNodeID": "manu",
                    "kubernetesStatus": {
                        "namespace": "garage",
                        "pvcName": "data-garage-0",
                    },
                },
            }
            items = [volume]
            if ",replicas.longhorn.io" in resource:
                items.append(
                    {
                        "kind": "Replica",
                        "metadata": {"name": "replica"},
                        "spec": {"volumeName": "garage-volume", "nodeID": "timmy"},
                    }
                )
            print(json.dumps({"items": items}))
        elif resource == "cronjob":
            print(str(state["cronjobs"][namespace + "/" + args[2]]).lower())
        elif resource in ("deployment", "statefulset"):
            key = f"{resource}/{namespace}/{args[2]}"
            if key not in state["replicas"]:
                return unexpected()
            if args[-1] == "json":
                print(
                    json.dumps(
                        {"spec": {"selector": {"matchLabels": {"app": args[2]}}}}
                    )
                )
            else:
                print(state["replicas"][key])
        else:
            return unexpected()
    elif verb == "scale":
        key = f"{resource}/{namespace}/{args[2]}"
        replicas = int(
            next(arg.split("=", 1)[1] for arg in args if arg.startswith("--replicas="))
        )
        if key not in state["replicas"]:
            return unexpected()
        if state.get("fail_scale") == f"{key}={replicas}":
            return 1
        state["replicas"][key] = replicas
        save()
    elif verb in ("cordon", "uncordon"):
        # A cordon can fail before the API applies it, or after (a lost reply).
        fault = state.get("fail_cordon") if verb == "cordon" else None
        if fault == "before":
            return 1
        state["nodes"][resource]["cordoned"] = verb == "cordon"
        save()
        if fault == "after":
            return 1
    elif verb == "drain":
        return 1 if state.get("fail_drain") else 0
    elif verb == "patch" and resource == "cronjob":
        state["cronjobs"][namespace + "/" + args[2]] = json.loads(args[-1])["spec"][
            "suspend"
        ]
        save()
    elif verb == "exec" and "amtool" in args:
        if "add" in args:
            state["silence"] = SILENCE
            print(SILENCE)
        elif "expire" in args:
            state["silence"] = None
        else:
            return unexpected()
        save()
    elif verb == "exec" and namespace == "garage" and resource == "garage-0":
        print("fixture Garage healthy")
    elif (
        verb == "create"
        and resource == "job"
        and "--from=cronjob/k3s-datastore-backup" in args
    ):
        state["backups"] += 1
        save()
    else:
        return unexpected()
    return 0


class MaintenanceCycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_root = self.root / "state"
        self.state_root.mkdir()
        self.cluster_file = self.root / "cluster.json"
        self.log_file = self.root / "calls.jsonl"
        self.log_file.touch()
        self.initial = {
            "nodes": {
                name: {
                    "cordoned": False,
                    "ready": True,
                    "boot": OLD_BOOT,
                    "reboot_required": False,
                }
                for name in ("wemby", "manu", "timmy")
            },
            "replicas": {
                "statefulset/garage/garage": 3,
                "deployment/viking/embedder-qwen-cuda": 2,
                "deployment/omnipendium/omnipendium-db": 1,
                "deployment/llama/cloud-llm-counter": 4,
                "deployment/viking/openviking": 5,
                "deployment/viking/ov-vectordb": 2,
            },
            "cronjobs": {
                "kube-system/k3s-datastore-backup": False,
                "compendium/compendium-sync": False,
                "viking/ovlock-janitor": True,
            },
            "silence": None,
            "backups": 0,
        }
        self.write(self.initial)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        for name in ("kubectl", "ssh", "sleep"):
            (self.bin_dir / name).symlink_to(Path(__file__).resolve())
        self.env = dict(
            os.environ,
            PATH=f"{self.bin_dir}:{os.environ['PATH']}",
            XDG_STATE_HOME=str(self.state_root),
            NODE_MAINTENANCE=str(NODE),
            KUBECTL=str(self.bin_dir / "kubectl"),
            FAKE_CLUSTER_STATE=str(self.cluster_file),
            FAKE_CLUSTER_LOG=str(self.log_file),
            NODE_SSH_SUFFIX="-lan",
            PYTHONDONTWRITEBYTECODE="1",
        )
        self.cycle = self.state_root / "homelab-node-maintenance"
        self.outage = self.state_root / "homelab-timmy-offline"

    def write(self, state):
        self.cluster_file.write_text(json.dumps(state))

    def read(self):
        return json.loads(self.cluster_file.read_text())

    def calls(self):
        return [json.loads(line) for line in self.log_file.read_text().splitlines()]

    def run_command(self, script, *args, ok=True):
        result = subprocess.run(
            ["bash", str(script), *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(
            self.read().get("unexpected", []), [], result.stdout + result.stderr
        )
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def reboot(self, node="timmy", boot=NEW_BOOT):
        state = self.read()
        state["nodes"][node]["boot"] = boot
        state["nodes"][node]["reboot_required"] = False
        self.write(state)

    def assert_restored(self):
        state = self.read()
        self.assertEqual(state["replicas"], self.initial["replicas"])
        self.assertEqual(state["cronjobs"], self.initial["cronjobs"])
        self.assertIsNone(state["silence"])
        self.assertFalse(state["nodes"]["timmy"]["cordoned"])
        self.assertFalse((self.cycle / "timmy.boot-id").exists())
        self.assertFalse((self.cycle / "spin-down-replicas").exists())
        self.assertFalse((self.cycle / "window").exists())
        for name in (
            "parked-scales",
            "suspended-cronjobs",
            "silence-id",
            "silence-end",
            "window",
        ):
            self.assertFalse((self.outage / name).exists(), name)

    def test_down_repeat_and_up_restore_original_state(self):
        self.run_command(OUTAGE, "down")
        first_scales = (self.outage / "parked-scales").read_text()
        first_spindown = (self.cycle / "spin-down-replicas").read_text()
        self.reboot()
        repeat = self.run_command(OUTAGE, "down")
        self.assertIn("rebooted since the saved cycle", repeat.stdout)
        self.assertEqual((self.outage / "parked-scales").read_text(), first_scales)
        self.assertEqual(
            (self.cycle / "spin-down-replicas").read_text(), first_spindown
        )
        self.assertEqual((self.cycle / "timmy.boot-id").read_text().strip(), NEW_BOOT)
        cluster_calls = [
            call["args"] for call in self.calls() if call["tool"] == "kubectl"
        ]
        self.assertEqual(
            sum(args[:2] == ["drain", "timmy"] for args in cluster_calls), 2
        )
        self.assertEqual(
            sum("amtool" in args and "add" in args for args in cluster_calls), 1
        )
        ssh = [call["args"] for call in self.calls() if call["tool"] == "ssh"]
        self.assertTrue(all(args[-2].endswith("-lan") for args in ssh))
        self.reboot(boot=THIRD_BOOT)
        self.run_command(OUTAGE, "up")
        self.assert_restored()
        self.assertEqual(self.read()["backups"], 1)

    def test_earlier_reboot_refusal_precedes_every_mutation(self):
        state = self.read()
        state["nodes"]["wemby"]["reboot_required"] = True
        self.write(state)
        self.run_command(OUTAGE, "down", ok=False)
        self.assertEqual(self.read(), state)
        self.assertEqual(list(self.state_root.iterdir()), [])

    def test_partial_preparation_recovers_without_a_reboot(self):
        state = self.read()
        state["active_job"] = True
        self.write(state)
        self.run_command(OUTAGE, "down", ok=False)
        self.assertTrue((self.cycle / "spin-down-replicas").exists())
        self.assertFalse((self.cycle / "timmy.boot-id").exists())
        self.assertEqual(
            self.run_command(NODE, "cycle-status", "timmy").stdout, "none\n"
        )
        self.run_command(OUTAGE, "up")
        self.assert_restored()

    def test_other_cycle_cannot_lose_its_spin_down(self):
        self.run_command(NODE, "drain", "wemby", "--spin-down")
        before = self.read()
        self.run_command(OUTAGE, "down", ok=False)
        self.run_command(NODE, "recover", "timmy", ok=False)
        self.assertEqual(self.read(), before)
        self.assertTrue((self.cycle / "wemby.boot-id").exists())
        self.assertTrue((self.cycle / "spin-down-replicas").exists())

    def test_restore_failure_keeps_cordon_and_retries(self):
        self.run_command(OUTAGE, "down")
        self.reboot()
        state = self.read()
        state["fail_scale"] = "deployment/viking/ov-vectordb=2"
        self.write(state)
        self.run_command(OUTAGE, "up", ok=False)
        self.assertTrue(self.read()["nodes"]["timmy"]["cordoned"])
        self.assertEqual(self.read()["replicas"]["statefulset/garage/garage"], 0)
        self.assertTrue((self.cycle / "timmy.boot-id").exists())
        self.assertTrue((self.cycle / "spin-down-replicas").exists())
        state = self.read()
        del state["fail_scale"]
        self.write(state)
        self.run_command(OUTAGE, "up")
        self.assert_restored()

    def test_recover_rejects_unreadable_cordon_and_ready(self):
        for query in (
            "jsonpath={.spec.unschedulable}",
            'jsonpath={.status.conditions[?(@.type=="Ready")].status}',
        ):
            with self.subTest(query=query):
                state = self.read()
                state["fail_node_read"] = query
                self.write(state)
                self.run_command(NODE, "recover", "timmy", ok=False)
                self.assertEqual(self.read(), state)

    def test_preflight_rejects_failed_cordon_read_before_outage(self):
        state = self.read()
        state["fail_node_read"] = "all"
        self.write(state)
        self.run_command(OUTAGE, "down", ok=False)
        self.assertEqual(self.read(), state)
        self.assertEqual(list(self.state_root.iterdir()), [])

    def record_cycle(self, node="timmy", boot=OLD_BOOT):
        self.cycle.mkdir(exist_ok=True)
        (self.cycle / f"{node}.boot-id").write_text(boot + "\n")

    def test_repeat_of_recorded_cycle_keeps_multi_cordon_policy(self):
        state = self.read()
        for node in ("timmy", "wemby"):
            state["nodes"][node]["cordoned"] = True
        self.write(state)
        self.record_cycle()
        self.run_command(NODE, "preflight", "timmy", ok=False)
        self.run_command(NODE, "preflight", "timmy", "--allow-repeat")
        result = self.run_command(NODE, "drain", "timmy", "--spin-down")
        self.assertNotIn("rebooted since the saved cycle", result.stdout)
        self.assertFalse((self.cycle / "spin-down-replicas").exists())
        self.assertEqual(self.read(), state)
        self.assertEqual((self.cycle / "timmy.boot-id").read_text().strip(), OLD_BOOT)

    def test_hand_cordon_gets_no_repeat_drain(self):
        state = self.read()
        state["nodes"]["manu"]["cordoned"] = True
        self.write(state)
        self.run_command(NODE, "preflight", "manu", "--allow-repeat", ok=False)
        self.run_command(NODE, "drain", "manu", ok=False)
        self.assertFalse(
            any(
                c["args"][:1] == ["drain"]
                for c in self.calls()
                if c["tool"] == "kubectl"
            )
        )
        self.assertFalse((self.cycle / "manu.boot-id").exists())
        self.assertEqual(self.read(), state)

    def test_down_refuses_hand_cordoned_timmy_before_mutating(self):
        state = self.read()
        state["nodes"]["timmy"]["cordoned"] = True
        self.write(state)
        self.run_command(OUTAGE, "down", ok=False)
        self.assertEqual(self.read(), state)
        self.assertEqual(list(self.state_root.iterdir()), [])

    def interrupted_cordon_retries(self, fault):
        state = self.read()
        state["fail_cordon"] = fault
        self.write(state)
        self.run_command(OUTAGE, "down", ok=False)
        self.assertEqual((self.cycle / "timmy.boot-id").read_text().strip(), OLD_BOOT)
        state = self.read()
        del state["fail_cordon"]
        self.write(state)
        self.run_command(OUTAGE, "down")
        self.assertTrue(self.read()["nodes"]["timmy"]["cordoned"])
        self.reboot()
        self.run_command(OUTAGE, "up")
        self.assert_restored()

    def test_down_retries_after_cordon_applied_but_failed(self):
        self.interrupted_cordon_retries("after")

    def test_down_retries_after_cordon_never_applied(self):
        self.interrupted_cordon_retries("before")

    def test_unreadable_state_is_not_absent_state(self):
        self.record_cycle()
        for locked in (self.cycle, self.state_root):
            with self.subTest(locked=locked.name):
                locked.chmod(0)
                self.addCleanup(locked.chmod, 0o700)
                try:
                    result = self.run_command(NODE, "cycle-status", "timmy", ok=False)
                    self.assertNotIn("none", result.stdout)
                finally:
                    locked.chmod(0o700)

    def test_repeat_checks_leftovers_after_successful_drain(self):
        self.record_cycle()
        state = self.read()
        state["nodes"]["timmy"]["cordoned"] = True
        state["pods"] = [
            {
                "metadata": {"namespace": "default", "name": "leftover"},
                "spec": {"nodeName": "timmy", "containers": []},
                "status": {"phase": "Pending"},
            }
        ]
        self.write(state)
        self.run_command(NODE, "drain", "timmy", ok=False)
        self.assertEqual((self.cycle / "timmy.boot-id").read_text().strip(), OLD_BOOT)

    def test_first_successful_drain_keeps_its_existing_leftover_policy(self):
        state = self.read()
        state["pods"] = [
            {
                "metadata": {"namespace": "default", "name": "leftover"},
                "spec": {"nodeName": "timmy", "containers": []},
                "status": {"phase": "Pending"},
            }
        ]
        self.write(state)
        self.run_command(NODE, "drain", "timmy")
        self.assertTrue((self.cycle / "timmy.boot-id").exists())

    def test_strict_recovery_still_refuses_after_manual_uncordon(self):
        self.run_command(NODE, "reboot", "timmy", "--no-reboot")
        self.reboot()
        state = self.read()
        state["nodes"]["timmy"]["cordoned"] = False
        self.write(state)
        self.run_command(NODE, "recover", "timmy", ok=False)
        self.assertTrue((self.cycle / "timmy.boot-id").exists())

    def test_cycle_query_is_read_only_and_windows_are_not_cycles(self):
        self.cycle.mkdir()
        (self.cycle / "window").write_text("1999999999 planned maintenance\n")
        self.assertEqual(
            self.run_command(NODE, "cycle-status", "timmy").stdout, "none\n"
        )
        self.assertEqual(self.calls(), [])
        (self.cycle / "timmy.boot-id").mkdir()
        self.run_command(NODE, "cycle-status", "timmy", ok=False)
        self.assertEqual(self.calls(), [])

    def test_new_mutating_commands_obey_existing_lock(self):
        lock = self.cycle / "lock"
        lock.mkdir(parents=True)
        (lock / "pid").write_text(str(os.getpid()) + "\n")
        for command in ("drain", "recover"):
            with self.subTest(command=command):
                self.run_command(NODE, command, "timmy", ok=False)
        self.assertEqual(self.calls(), [])
        self.assertEqual((lock / "pid").read_text(), str(os.getpid()) + "\n")


if __name__ == "__main__":
    command_name = Path(sys.argv[0]).name
    if command_name in ("kubectl", "ssh", "sleep"):
        sys.exit(fake_command(command_name, sys.argv[1:]))
    unittest.main()
