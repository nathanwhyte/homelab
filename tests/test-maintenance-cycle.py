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
OWNER_KEY = "homelab/cordon-owner"


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
                                    "metadata": {
                                        "name": name,
                                        "annotations": node.get("annotations", {}),
                                    },
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
            if "unschedulable" in query or query == "json":
                if query == "json":
                    print(
                        json.dumps(
                            {
                                "metadata": {
                                    "name": args[2],
                                    "resourceVersion": str(node.get("version", 1)),
                                    "annotations": node.get("annotations", {}),
                                },
                                "spec": {"unschedulable": node["cordoned"]},
                            }
                        )
                    )
                else:
                    print(str(node["cordoned"]).lower())
                # The other Mac's down finishes right after this read: its park
                # and spin-down zero every workload, then its drain cordons.
                # Another writer replaces the owner right after down's read.
                if state.get("race_owner") and query == "json" and args[2] == "timmy":
                    node.setdefault("annotations", {})[OWNER_KEY] = state.pop(
                        "race_owner"
                    )
                    node["version"] = node.get("version", 1) + 1
                    save()
                elif state.get("race_cordon") and args[2] == "timmy":
                    state["race_cordon"] = False
                    node["cordoned"] = True
                    node["annotations"] = {OWNER_KEY: "outage:mac-two"}
                    node["version"] = node.get("version", 1) + 1
                    state["replicas"] = dict.fromkeys(state["replicas"], 0)
                    save()
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
    elif verb == "patch" and resource == "node":
        node = state["nodes"][args[2]]
        patch = json.loads(args[args.index("-p") + 1])
        if "--type=merge" not in args or set(patch) - {"metadata", "spec"}:
            return unexpected()
        metadata = patch.get("metadata", {})
        version = metadata.get("resourceVersion")
        # A status update landing between the read and this write.
        if version is not None and state.get("node_churn", 0) > 0:
            state["node_churn"] -= 1
            node["version"] = node.get("version", 1) + 1
            save()
        if version is not None and version != str(node.get("version", 1)):
            print(
                f'Error from server (Conflict): Operation cannot be fulfilled on nodes "{args[2]}": the object has been modified',
                file=sys.stderr,
            )
            return 1
        cordon = patch.get("spec", {}).get("unschedulable")
        # A cordon can fail before the API applies it, or after (a lost reply).
        fault = state.get("fail_cordon") if cordon else None
        if fault == "before":
            return 1
        node["cordoned"] = bool(cordon)
        annotations = node.setdefault("annotations", {})
        for key, value in metadata.get("annotations", {}).items():
            if value is None:
                annotations.pop(key, None)
            else:
                annotations[key] = value
        node["version"] = node.get("version", 1) + 1
        save()
        if fault == "after":
            return 1
    elif verb == "drain":
        node = state["nodes"][resource]
        # Something clears the cordon and its owner just before the drain,
        # which then cordons again itself, as kubectl drain does.
        if state.pop("clear_before_drain", False):
            node["cordoned"] = False
            node["annotations"] = {}
        node["cordoned"] = True
        save()
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
            MAINTENANCE_HOST="mac-one",
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

    def drain_calls(self):
        return [
            c["args"]
            for c in self.calls()
            if c["tool"] == "kubectl" and c["args"][:1] == ["drain"]
        ]

    def cordon_by(self, node, owner=None):
        state = self.read()
        state["nodes"][node]["cordoned"] = True
        if owner:
            state["nodes"][node]["annotations"] = {OWNER_KEY: owner}
        self.write(state)
        return state

    def owner(self, node="timmy"):
        return self.read()["nodes"][node].get("annotations", {}).get(OWNER_KEY)

    def assert_down_refused_untouched(self, before):
        self.run_command(OUTAGE, "down", ok=False)
        self.assertEqual(self.read(), before)
        self.assertFalse(self.outage.exists())
        self.assertFalse((self.cycle / "spin-down-replicas").exists())

    def test_repeat_keeps_multi_cordon_policy(self):
        self.cordon_by("wemby")
        state = self.cordon_by("timmy", "drain:mac-one")
        self.record_cycle()
        self.run_command(NODE, "preflight", "timmy", ok=False)
        self.run_command(NODE, "preflight", "timmy", "--allow-repeat")
        self.run_command(NODE, "drain", "timmy", "--spin-down", ok=False)
        self.assertEqual(self.drain_calls(), [])
        result = self.run_command(NODE, "drain", "timmy", "--spin-down", "--repeat")
        self.assertNotIn("rebooted since the saved cycle", result.stdout)
        self.assertFalse((self.cycle / "spin-down-replicas").exists())
        self.assertEqual(self.read(), state)
        self.assertEqual((self.cycle / "timmy.boot-id").read_text().strip(), OLD_BOOT)

    def test_bare_drain_refuses_any_cordoned_node(self):
        state = self.cordon_by("manu")
        self.run_command(NODE, "drain", "manu", ok=False)
        self.assertEqual(self.drain_calls(), [])
        self.assertFalse((self.cycle / "manu.boot-id").exists())
        self.assertEqual(self.read(), state)
        # --repeat re-drains only this Mac's own cordon, never a hand cordon.
        self.run_command(NODE, "drain", "manu", "--repeat", ok=False)
        self.assertEqual(self.drain_calls(), [])
        self.assertFalse((self.cycle / "manu.boot-id").exists())
        self.cordon_by("manu", "drain:mac-one")
        self.run_command(NODE, "drain", "manu", "--repeat", "--outage", ok=False)
        self.assertEqual(self.drain_calls(), [])
        self.run_command(NODE, "drain", "manu", "--repeat")
        self.assertEqual(len(self.drain_calls()), 1)
        self.assertEqual((self.cycle / "manu.boot-id").read_text().strip(), OLD_BOOT)

    def test_down_refuses_a_cordon_it_does_not_own(self):
        for owner in (None, "outage:mac-two", "drain:mac-one", "reboot:mac-one"):
            with self.subTest(owner=owner):
                self.write(self.initial)
                before = self.cordon_by("timmy", owner)
                self.assert_down_refused_untouched(before)

    def test_down_refuses_a_reboot_cycles_cordon(self):
        self.run_command(NODE, "reboot", "timmy", "--no-reboot")
        self.assertEqual(self.owner(), "reboot:mac-one")
        before = self.read()
        self.assertTrue((self.cycle / "timmy.boot-id").exists())
        self.run_command(OUTAGE, "down", ok=False)
        self.assertEqual(self.read(), before)
        self.assertFalse(self.outage.exists())

    def test_down_from_the_other_mac_refuses_before_any_change(self):
        self.run_command(OUTAGE, "down")
        self.assertEqual(self.owner(), "outage:mac-one")
        before = self.read()
        first_mac = self.env["XDG_STATE_HOME"]
        other_mac = self.root / "other-mac"
        other_mac.mkdir()
        self.env.update(XDG_STATE_HOME=str(other_mac), MAINTENANCE_HOST="mac-two")
        result = self.run_command(OUTAGE, "down", ok=False)
        self.assertIn("outage:mac-one", result.stderr)
        self.assertEqual(self.read(), before)
        self.assertEqual(list(other_mac.iterdir()), [])
        # The first Mac's up restores the real counts and drops the owner.
        self.env.update(XDG_STATE_HOME=first_mac, MAINTENANCE_HOST="mac-one")
        self.reboot()
        self.run_command(OUTAGE, "up")
        self.assert_restored()
        self.assertIsNone(self.owner())

    def test_a_claimed_legacy_cordon_repeats(self):
        # A cordon from before owners were recorded refuses until claimed.
        before = self.cordon_by("timmy")
        self.assert_down_refused_untouched(before)
        self.cordon_by("timmy", "outage:mac-one")
        self.run_command(OUTAGE, "down")
        self.assertEqual(len(self.drain_calls()), 1)
        self.assertFalse((self.cycle / "spin-down-replicas").exists())

    def test_stale_owner_on_an_uncordoned_node_is_replaced(self):
        state = self.read()
        state["nodes"]["timmy"]["annotations"] = {OWNER_KEY: "outage:mac-two"}
        self.write(state)
        self.run_command(OUTAGE, "down")
        self.assertTrue(self.read()["nodes"]["timmy"]["cordoned"])
        self.assertEqual(self.owner(), "outage:mac-one")
        self.assertTrue((self.cycle / "spin-down-replicas").exists())

    def test_owner_replaced_after_down_reads_it_refuses_before_any_change(self):
        state = self.cordon_by("timmy", "outage:mac-one")
        state["race_owner"] = "outage:mac-two"
        self.write(state)
        self.run_command(OUTAGE, "down", ok=False)
        self.assertIsNone(self.read()["silence"])
        self.assertEqual(self.read()["replicas"], self.initial["replicas"])
        self.assertEqual(self.read()["cronjobs"], self.initial["cronjobs"])
        self.assertFalse(self.outage.exists())
        self.assertEqual(self.drain_calls(), [])

    def test_a_drain_that_recordons_a_cleared_cordon_fails(self):
        for repeat in (False, True):
            with self.subTest(repeat=repeat):
                self.write(self.initial)
                if repeat:
                    self.cordon_by("timmy", "drain:mac-one")
                state = self.read()
                state["clear_before_drain"] = True
                self.write(state)
                args = ("drain", "timmy", "--repeat") if repeat else ("drain", "timmy")
                result = self.run_command(NODE, *args, ok=False)
                self.assertIn("the cordon changed during the drain", result.stderr)
                self.assertTrue(self.read()["nodes"]["timmy"]["cordoned"])
                self.assertIsNone(self.owner())

    def test_owner_with_a_backslash_still_repeats(self):
        self.env["MAINTENANCE_HOST"] = "mac\\one"
        self.run_command(OUTAGE, "down")
        self.assertEqual(self.owner(), "outage:mac\\one")
        self.run_command(OUTAGE, "down")
        self.assertEqual(len(self.drain_calls()), 2)

    def test_cordon_retries_a_conflict(self):
        state = self.read()
        state["node_churn"] = 2
        self.write(state)
        result = self.run_command(OUTAGE, "down")
        self.assertEqual(result.stdout.count("changed while cordoning"), 2)
        self.assertEqual(self.owner(), "outage:mac-one")

    def test_cordon_gives_up_after_repeated_conflicts(self):
        state = self.read()
        state["node_churn"] = 3
        self.write(state)
        self.run_command(NODE, "drain", "timmy", ok=False)
        self.assertFalse(self.read()["nodes"]["timmy"]["cordoned"])
        self.assertIsNone(self.owner())
        self.assertFalse((self.cycle / "timmy.boot-id").exists())
        self.assertEqual(self.drain_calls(), [])

    def test_down_racing_the_other_macs_down_refuses_before_parking(self):
        state = self.read()
        state["race_cordon"] = True
        self.write(state)
        self.run_command(OUTAGE, "down", ok=False)
        self.assertFalse((self.outage / "parked-scales").exists())
        self.assertIsNone(self.read()["silence"])
        self.assertEqual(self.drain_calls(), [])
        self.assertFalse(
            any(
                c["args"][:1] == ["scale"]
                for c in self.calls()
                if c["tool"] == "kubectl"
            )
        )

    def test_record_path_refusal_precedes_every_mutation(self):
        self.cycle.mkdir()
        (self.cycle / "timmy.boot-id").symlink_to(self.root / "elsewhere")
        before = self.read()
        for script, args in (
            (NODE, ("drain", "timmy", "--spin-down")),
            (NODE, ("reboot", "timmy")),
            (NODE, ("preflight", "timmy")),
            (OUTAGE, ("down",)),
        ):
            with self.subTest(args=args):
                self.run_command(script, *args, ok=False)
                self.assertEqual(self.read(), before)
                self.assertFalse((self.cycle / "spin-down-replicas").exists())
                self.assertFalse(self.outage.exists())
        state = self.cordon_by("timmy", "outage:mac-one")
        for script, args in (
            (NODE, ("preflight", "timmy", "--allow-repeat")),
            (NODE, ("drain", "timmy", "--repeat", "--outage")),
            (OUTAGE, ("down",)),
        ):
            with self.subTest(args=args):
                self.run_command(script, *args, ok=False)
                self.assertEqual(self.read(), state)
                self.assertFalse(self.outage.exists())
        self.assertEqual(self.drain_calls(), [])

    def test_restore_services_refuses_a_dangling_record(self):
        # cycle-status counts a dangling link as a record; so must this scan.
        self.cycle.mkdir()
        (self.cycle / "wemby.boot-id").symlink_to(self.root / "elsewhere")
        self.assertEqual(
            self.run_command(NODE, "cycle-status", "wemby", ok=False).stdout, ""
        )
        result = self.run_command(NODE, "restore-services", ok=False)
        self.assertIn("wemby has saved reboot state", result.stderr)

    def test_finish_takes_an_explicit_id_for_an_empty_record(self):
        for content in ("", "\n"):
            with self.subTest(content=repr(content)):
                state = self.read()
                state["nodes"]["timmy"]["cordoned"] = True
                state["nodes"]["timmy"]["boot"] = OLD_BOOT
                self.write(state)
                self.record_cycle(boot="")
                (self.cycle / "timmy.boot-id").write_text(content)
                self.reboot()
                self.run_command(NODE, "finish", "timmy", OLD_BOOT)
                self.assertFalse(self.read()["nodes"]["timmy"]["cordoned"])
                self.assertFalse((self.cycle / "timmy.boot-id").exists())

    def test_finish_refuses_a_record_it_cannot_compare(self):
        wrong = "11111111-1111-1111-1111-111111111111"
        state = self.read()
        state["nodes"]["timmy"]["cordoned"] = True
        self.write(state)
        record = self.cycle / "timmy.boot-id"
        self.record_cycle()
        result = self.run_command(NODE, "finish", "timmy", wrong, ok=False)
        self.assertIn("does not match the saved cycle state", result.stderr)
        record.chmod(0)
        self.addCleanup(lambda: record.exists() and record.chmod(0o600))
        for args in (("finish", "timmy", wrong), ("finish", "timmy")):
            with self.subTest(record="unreadable", args=args):
                result = self.run_command(NODE, *args, ok=False)
                self.assertIn("cannot read saved reboot state", result.stderr)
        record.chmod(0o600)
        record.unlink()
        record.mkdir()
        with self.subTest(record="directory"):
            result = self.run_command(NODE, "finish", "timmy", wrong, ok=False)
            self.assertIn("cannot read saved reboot state", result.stderr)
        self.assertEqual(self.read(), state)

    def test_preflight_refuses_unusable_state_before_down_parks(self):
        self.cycle.mkdir()
        self.addCleanup(self.cycle.chmod, 0o700)
        for mode, message in (
            (0o500, "cannot write maintenance state"),
            (0o600, "cannot read maintenance state directory"),
        ):
            with self.subTest(mode=oct(mode)):
                self.cycle.chmod(mode)
                before = self.read()
                result = self.run_command(OUTAGE, "down", ok=False)
                self.cycle.chmod(0o700)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.read(), before)
                self.assertFalse(self.outage.exists())
        # The repeat path: this Mac's cordon, with a record it cannot read.
        state = self.cordon_by("timmy", "outage:mac-one")
        record = self.cycle / "timmy.boot-id"
        self.record_cycle()
        record.chmod(0)
        self.addCleanup(record.chmod, 0o600)
        result = self.run_command(OUTAGE, "down", ok=False)
        self.assertIn("cannot read saved reboot state", result.stderr)
        self.assertEqual(self.read(), state)
        self.assertFalse(self.outage.exists())
        self.assertEqual(self.drain_calls(), [])

    def test_memory_blocked_drain_advises_drain(self):
        def pod(node, memory):
            return {
                "spec": {
                    "nodeName": node,
                    "containers": [{"resources": {"requests": {"memory": memory}}}],
                }
            }

        state = self.read()
        state["pods"] = [
            pod("wemby", "20Gi"),
            pod("manu", "60Gi"),
            pod("timmy", "60Gi"),
        ]
        self.write(state)
        result = self.run_command(NODE, "drain", "wemby", ok=False)
        self.assertIn("memory preflight BLOCKED", result.stderr)
        self.assertRegex(result.stderr, r"Re-run with: \S+ drain wemby --spin-down")
        result = self.run_command(NODE, "reboot", "wemby", ok=False)
        self.assertRegex(result.stderr, r"Re-run with: \S+ reboot wemby --spin-down")
        self.assertEqual(self.drain_calls(), [])

    def test_boot_record_save_is_atomic(self):
        self.record_cycle()
        record = self.cycle / "timmy.boot-id"
        # A failed rename (the stand-in for a stop mid-save) keeps the old record.
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'source "{NODE}"; mv() {{ return 1; }}; save_cycle_boot_id timmy {NEW_BOOT}',
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(record.read_text().strip(), OLD_BOOT)
        # Its leftover temp file is not a cycle record.
        record.unlink()
        self.assertTrue(list(self.cycle.glob("timmy.boot-id.*")))
        self.assertEqual(
            self.run_command(NODE, "cycle-status", "timmy").stdout, "none\n"
        )

    def test_record_readers_agree_on_empty_records(self):
        for content in ("", "\n"):
            with self.subTest(content=repr(content)):
                self.record_cycle(boot="")
                (self.cycle / "timmy.boot-id").write_text(content)
                self.assertEqual(
                    self.run_command(NODE, "cycle-status", "timmy").stdout,
                    "recorded\n",
                )
                result = self.run_command(NODE, "recover", "timmy", ok=False)
                self.assertIn("pass its previous boot ID explicitly", result.stderr)
                state = self.cordon_by("timmy", "drain:mac-one")
                result = self.run_command(NODE, "drain", "timmy", "--repeat")
                self.assertIn("saved reboot state was empty", result.stdout)
                self.assertEqual(
                    (self.cycle / "timmy.boot-id").read_text().strip(), OLD_BOOT
                )
                state["nodes"]["timmy"]["cordoned"] = False
                self.write(state)

    def test_repeat_down_repairs_an_empty_boot_record(self):
        self.run_command(OUTAGE, "down")
        (self.cycle / "timmy.boot-id").write_text("")
        self.run_command(OUTAGE, "down")
        self.assertEqual((self.cycle / "timmy.boot-id").read_text().strip(), OLD_BOOT)
        self.reboot()
        self.run_command(OUTAGE, "up")
        self.assert_restored()

    def interrupted_cordon_retries(self, fault):
        state = self.read()
        state["fail_cordon"] = fault
        self.write(state)
        self.run_command(OUTAGE, "down", ok=False)
        # Cordon first, then save: a saved record always means a cordon.
        self.assertFalse((self.cycle / "timmy.boot-id").exists())
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
        state = self.cordon_by("timmy", "drain:mac-one")
        state["pods"] = [
            {
                "metadata": {"namespace": "default", "name": "leftover"},
                "spec": {"nodeName": "timmy", "containers": []},
                "status": {"phase": "Pending"},
            }
        ]
        self.write(state)
        self.run_command(NODE, "drain", "timmy", "--repeat", ok=False)
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
