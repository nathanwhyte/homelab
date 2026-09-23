#!/usr/bin/env python3
"""Safety and webhook tests for the capacity auto-resolver."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name("resolver.py")
SPEC = importlib.util.spec_from_file_location("capacity_auto_resolver", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"could not load {MODULE_PATH}")
resolver_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = resolver_module
SPEC.loader.exec_module(resolver_module)

Gi = 1024**3
NOW = datetime(2026, 9, 13, 20, 0, tzinfo=UTC)


def base_policy(mode: str = "dry-run") -> dict:
    return {
        "mode": mode,
        "minimumUsagePercent": 85,
        "cooldownHours": 24,
        "headroom": {
            "minimumFreeBytes": 20 * Gi,
            "minimumFreePercent": 25,
            "maximumScheduledPercent": 80,
        },
        "denylist": [
            {"namespace": "grafana", "persistentVolumeClaim": "*"},
            {"namespace": "*", "persistentVolumeClaim": "*postgres*"},
        ],
        "allowlist": [
            {
                "namespace": "viking",
                "persistentVolumeClaim": "llama-cuda-model-cache",
                "storageClass": "longhorn-ssd",
                "increment": "4Gi",
                "maximumSize": "20Gi",
            }
        ],
    }


def firing_alert(
    *,
    alertname: str = "KubePersistentVolumeFillingUp",
    status: str = "firing",
    namespace: str = "viking",
    pvc: str = "llama-cuda-model-cache",
) -> dict:
    return {
        "status": status,
        "labels": {
            "alertname": alertname,
            "alertgroup": "storage",
            "namespace": namespace,
            "persistentvolumeclaim": pvc,
            "severity": "warning",
        },
        "annotations": {
            "summary": "PVC is filling up.",
            "description": "The PersistentVolume is only 10.0% free.",
        },
        "fingerprint": "alert-fingerprint-1",
        "startsAt": "2026-09-13T19:00:00Z",
    }


class FakeKubernetes:
    def __init__(self) -> None:
        self.pvc = {
            "metadata": {
                "name": "llama-cuda-model-cache",
                "namespace": "viking",
                "resourceVersion": "12345",
                "annotations": {},
            },
            "spec": {
                "storageClassName": "longhorn-ssd",
                "volumeName": "pvc-volume-uid",
                "resources": {"requests": {"storage": "12Gi"}},
            },
            "status": {
                "phase": "Bound",
                "capacity": {"storage": "12Gi"},
            },
        }
        self.storage_class = {
            "allowVolumeExpansion": True,
            "provisioner": "driver.longhorn.io",
        }
        self.volume = {
            "spec": {"numberOfReplicas": 1, "size": str(12 * Gi)},
            "status": {"state": "attached", "robustness": "healthy"},
        }
        self.replicas = [
            {
                "metadata": {"name": "replica-1"},
                "spec": {"nodeID": "timmy", "diskID": "disk-uuid-1"},
            }
        ]
        self.nodes = {
            "timmy": {
                "status": {
                    "diskStatus": {
                        "default-disk": {
                            "diskUUID": "disk-uuid-1",
                            "storageAvailable": 200 * Gi,
                            "storageMaximum": 500 * Gi,
                            "storageScheduled": 100 * Gi,
                            "conditions": [
                                {"type": "Ready", "status": "True"},
                                {"type": "Schedulable", "status": "True"},
                            ],
                        }
                    }
                }
            }
        }
        self.patches: list[tuple[str, str, list[dict]]] = []

    def get_pvc(self, namespace: str, name: str) -> dict:
        if (namespace, name) != ("viking", "llama-cuda-model-cache"):
            raise AssertionError("unexpected PVC lookup")
        return copy.deepcopy(self.pvc)

    def get_storage_class(self, name: str) -> dict:
        if name != "longhorn-ssd":
            raise AssertionError("unexpected StorageClass lookup")
        return copy.deepcopy(self.storage_class)

    def get_longhorn_volume(self, name: str) -> dict:
        if name != "pvc-volume-uid":
            raise AssertionError("unexpected Longhorn volume lookup")
        return copy.deepcopy(self.volume)

    def list_longhorn_replicas(self, volume: str) -> list[dict]:
        if volume != "pvc-volume-uid":
            raise AssertionError("unexpected replica lookup")
        return copy.deepcopy(self.replicas)

    def get_longhorn_node(self, name: str) -> dict:
        return copy.deepcopy(self.nodes[name])

    def patch_pvc(self, namespace: str, name: str, patch: list[dict]) -> dict:
        self.patches.append((namespace, name, copy.deepcopy(patch)))
        annotations = self.pvc["metadata"].get("annotations")
        for operation in patch:
            path = operation["path"]
            if operation["op"] == "test":
                if path == "/metadata/resourceVersion":
                    actual = self.pvc["metadata"]["resourceVersion"]
                elif path == "/spec/resources/requests/storage":
                    actual = self.pvc["spec"]["resources"]["requests"]["storage"]
                elif path.endswith("notification-state"):
                    assert isinstance(annotations, dict)
                    actual = annotations["capacity-resolver.homelab/notification-state"]
                else:
                    raise AssertionError(f"unexpected test path: {path}")
                if actual != operation["value"]:
                    raise resolver_module.ExternalServiceError("JSON Patch test failed")
            elif path == "/metadata/annotations":
                self.pvc["metadata"]["annotations"] = {}
                annotations = self.pvc["metadata"]["annotations"]
            elif path.startswith("/metadata/annotations/"):
                assert isinstance(annotations, dict)
                encoded_key = path.removeprefix("/metadata/annotations/")
                key = encoded_key.replace("~1", "/").replace("~0", "~")
                annotations[key] = operation["value"]
            elif path == "/spec/resources/requests/storage":
                self.pvc["spec"]["resources"]["requests"]["storage"] = operation[
                    "value"
                ]
            else:
                raise AssertionError(f"unexpected patch path: {path}")
        self.pvc["metadata"]["resourceVersion"] = str(
            int(self.pvc["metadata"]["resourceVersion"]) + 1
        )
        return copy.deepcopy(self.pvc)


class FakeNotifier:
    def __init__(self, fail_on_call: int | None = None) -> None:
        self.messages: list[dict] = []
        self.fail_on_call = fail_on_call

    def notify(self, decision, phase: str) -> None:
        self.messages.append({"decision": decision, "phase": phase})
        if self.fail_on_call == len(self.messages):
            raise resolver_module.NotificationError("simulated Slack failure")


class ResolverSafetyTests(unittest.TestCase):
    def make_resolver(
        self,
        *,
        mode: str = "dry-run",
        usage: float = 90.0,
        mutation_enabled: bool = False,
        kube: FakeKubernetes | None = None,
        notifier: FakeNotifier | None = None,
        clock: datetime = NOW,
    ):
        kube = kube or FakeKubernetes()
        notifier = notifier or FakeNotifier()
        usage_provider = mock.Mock(return_value=usage)
        instance = resolver_module.Resolver(
            policy=resolver_module.Policy.from_dict(base_policy(mode)),
            kubernetes=kube,
            usage_provider=usage_provider,
            notifier=notifier,
            mutation_enabled=mutation_enabled,
            clock=lambda: clock,
        )
        return instance, kube, notifier, usage_provider

    def test_quantity_parser_uses_binary_kubernetes_units(self) -> None:
        self.assertEqual(resolver_module.parse_quantity("4Gi"), 4 * Gi)
        self.assertEqual(resolver_module.parse_quantity("512Mi"), 512 * 1024**2)
        self.assertEqual(resolver_module.parse_quantity("1Ti"), 1024 * Gi)
        self.assertEqual(resolver_module.format_quantity(16 * Gi), "16Gi")
        with self.assertRaises(ValueError):
            resolver_module.parse_quantity("4G")

    def test_prometheus_usage_query_pairs_each_series_before_deduplication(
        self,
    ) -> None:
        response = io.BytesIO(
            json.dumps(
                {
                    "status": "success",
                    "data": {"result": [{"value": [0, "90.5"]}]},
                }
            ).encode()
        )
        with mock.patch.object(
            urllib.request, "urlopen", return_value=response
        ) as urlopen:
            usage = resolver_module.PrometheusUsageProvider("http://prometheus")(
                "viking", "llama-cuda-model-cache"
            )

        self.assertEqual(usage, 90.5)
        requested_url = urlopen.call_args.args[0]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(requested_url).query)[
            "query"
        ][0]
        self.assertIn(
            "max(kubelet_volume_stats_used_bytes{",
            query,
        )
        self.assertIn(
            "/ kubelet_volume_stats_capacity_bytes{",
            query,
        )
        self.assertNotIn("/ max(kubelet_volume_stats_capacity_bytes", query)

    def test_rejects_non_object_alert_entry(self) -> None:
        instance, _, _, _ = self.make_resolver()
        with self.assertRaisesRegex(TypeError, "alert must be an object"):
            instance.process_payload({"alerts": [None]})

    def test_refuses_same_named_alert_without_trusted_route_labels(self) -> None:
        instance, kube, notifier, usage = self.make_resolver()
        kube.get_pvc = mock.Mock(side_effect=AssertionError("must not query PVC"))
        alert = firing_alert()
        alert["labels"]["alertgroup"] = "not-storage"

        decision = instance.process_alert(alert)

        self.assertEqual(decision.outcome, "refuse")
        self.assertEqual(decision.reason, "untrusted-alert-labels")
        kube.get_pvc.assert_not_called()
        usage.assert_not_called()
        self.assertEqual(notifier.messages[0]["phase"], "refused")

    def test_refuses_alert_without_episode_start_time(self) -> None:
        instance, kube, _, usage = self.make_resolver()
        kube.get_pvc = mock.Mock(side_effect=AssertionError("must not query PVC"))
        alert = firing_alert()
        del alert["startsAt"]

        decision = instance.process_alert(alert)

        self.assertEqual(decision.outcome, "refuse")
        self.assertEqual(decision.reason, "missing-alert-start-time")
        kube.get_pvc.assert_not_called()
        usage.assert_not_called()

    def test_ignores_longhorn_allocation_alert_without_kubernetes_calls(self) -> None:
        instance, kube, notifier, usage = self.make_resolver()
        kube.get_pvc = mock.Mock(side_effect=AssertionError("must not query PVC"))

        decision = instance.process_alert(
            firing_alert(alertname="LonghornVolumeSpaceHigh")
        )

        self.assertEqual(decision.outcome, "ignore")
        self.assertEqual(decision.reason, "unsupported-alert")
        kube.get_pvc.assert_not_called()
        usage.assert_not_called()
        self.assertEqual(notifier.messages, [])

    def test_ignores_resolved_alert(self) -> None:
        instance, kube, notifier, usage = self.make_resolver()
        kube.get_pvc = mock.Mock(side_effect=AssertionError("must not query PVC"))

        decision = instance.process_alert(firing_alert(status="resolved"))

        self.assertEqual(decision.outcome, "ignore")
        self.assertEqual(decision.reason, "not-firing")
        kube.get_pvc.assert_not_called()
        usage.assert_not_called()
        self.assertEqual(notifier.messages, [])

    def test_refuses_unallowlisted_pvc_before_cluster_lookup(self) -> None:
        instance, kube, notifier, usage = self.make_resolver()
        kube.get_pvc = mock.Mock(side_effect=AssertionError("must not query PVC"))

        # Neither deny-listed nor allowlisted, so this exercises the allowlist
        # miss itself (a grafana/* PVC would now stop at the deny-list first).
        decision = instance.process_alert(
            firing_alert(namespace="llama", pvc="llama-model-cache")
        )

        self.assertEqual(decision.outcome, "refuse")
        self.assertEqual(decision.reason, "not-allowlisted")
        kube.get_pvc.assert_not_called()
        usage.assert_not_called()
        self.assertEqual(notifier.messages[0]["phase"], "refused")

    def test_dry_run_reports_would_expand_without_patch(self) -> None:
        instance, kube, notifier, usage = self.make_resolver()

        decision = instance.process_alert(firing_alert())

        self.assertEqual(decision.outcome, "would-expand")
        self.assertEqual(decision.old_size, "12Gi")
        self.assertEqual(decision.new_size, "16Gi")
        self.assertEqual(decision.maximum_size, "20Gi")
        self.assertEqual(decision.usage_percent, 90.0)
        self.assertTrue(decision.headroom_allowed)
        self.assertEqual(kube.patches, [])
        usage.assert_called_once_with("viking", "llama-cuda-model-cache")
        self.assertEqual([m["phase"] for m in notifier.messages], ["dry-run"])

    def test_refuses_when_real_usage_is_below_policy_threshold(self) -> None:
        instance, kube, notifier, _ = self.make_resolver(usage=70.0)

        decision = instance.process_alert(firing_alert())

        self.assertEqual(decision.outcome, "refuse")
        self.assertEqual(decision.reason, "usage-below-threshold")
        self.assertEqual(kube.patches, [])
        self.assertEqual(notifier.messages[0]["phase"], "refused")

    def test_active_policy_still_requires_explicit_mutation_switch(self) -> None:
        instance, kube, notifier, _ = self.make_resolver(mode="active")

        decision = instance.process_alert(firing_alert())

        self.assertEqual(decision.outcome, "refuse")
        self.assertEqual(decision.reason, "mutation-switch-disabled")
        self.assertEqual(kube.patches, [])
        self.assertEqual(notifier.messages[0]["phase"], "refused")

    def test_active_mode_notifies_before_and_after_optimistic_patch(self) -> None:
        instance, kube, notifier, _ = self.make_resolver(
            mode="active", mutation_enabled=True
        )

        decision = instance.process_alert(firing_alert())

        self.assertEqual(decision.outcome, "expand")
        self.assertEqual(
            [m["phase"] for m in notifier.messages], ["proposed", "completed"]
        )
        self.assertEqual(len(kube.patches), 2)
        namespace, name, operations = kube.patches[0]
        self.assertEqual((namespace, name), ("viking", "llama-cuda-model-cache"))
        self.assertEqual(
            operations[:2],
            [
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": "12345",
                },
                {
                    "op": "test",
                    "path": "/spec/resources/requests/storage",
                    "value": "12Gi",
                },
            ],
        )
        self.assertIn(
            {
                "op": "replace",
                "path": "/spec/resources/requests/storage",
                "value": "16Gi",
            },
            operations,
        )
        annotation_values = {
            operation["path"]: operation["value"]
            for operation in operations
            if operation["path"].startswith("/metadata/annotations/")
        }
        self.assertEqual(
            annotation_values[
                "/metadata/annotations/capacity-resolver.homelab~1last-target"
            ],
            "16Gi",
        )
        self.assertEqual(
            annotation_values[
                "/metadata/annotations/"
                "capacity-resolver.homelab~1last-alert-fingerprint"
            ],
            "alert-fingerprint-1",
        )
        self.assertEqual(
            annotation_values[
                "/metadata/annotations/capacity-resolver.homelab~1last-expanded-at"
            ],
            "2026-09-13T20:00:00Z",
        )
        self.assertEqual(
            annotation_values[
                "/metadata/annotations/capacity-resolver.homelab~1last-alert-starts-at"
            ],
            "2026-09-13T19:00:00Z",
        )
        self.assertEqual(
            annotation_values[
                "/metadata/annotations/capacity-resolver.homelab~1notification-state"
            ],
            "pending",
        )
        _, _, completion_operations = kube.patches[1]
        self.assertEqual(completion_operations[-1]["value"], "completed")

    def test_notification_failure_blocks_irreversible_patch(self) -> None:
        notifier = FakeNotifier(fail_on_call=1)
        instance, kube, _, _ = self.make_resolver(
            mode="active", mutation_enabled=True, notifier=notifier
        )

        with self.assertRaises(resolver_module.NotificationError):
            instance.process_alert(firing_alert())

        self.assertEqual(kube.patches, [])

    def test_retry_completes_pending_audit_without_second_expansion(self) -> None:
        failing_notifier = FakeNotifier(fail_on_call=2)
        instance, kube, _, _ = self.make_resolver(
            mode="active", mutation_enabled=True, notifier=failing_notifier
        )
        with self.assertRaises(resolver_module.NotificationError):
            instance.process_alert(firing_alert())
        self.assertEqual(len(kube.patches), 1)
        self.assertEqual(
            kube.pvc["metadata"]["annotations"][
                "capacity-resolver.homelab/notification-state"
            ],
            "pending",
        )

        retry_notifier = FakeNotifier()
        retry, _, _, retry_usage = self.make_resolver(
            mode="active",
            mutation_enabled=True,
            kube=kube,
            notifier=retry_notifier,
        )
        retry_usage.side_effect = AssertionError(
            "outbox recovery must not depend on fresh usage"
        )
        decision = retry.process_alert(firing_alert())

        retry_usage.assert_not_called()
        self.assertEqual(decision.outcome, "expand")
        self.assertEqual(decision.reason, "patch-accepted")
        self.assertEqual([m["phase"] for m in retry_notifier.messages], ["completed"])
        self.assertEqual(len(kube.patches), 2)
        self.assertNotIn(
            "/spec/resources/requests/storage",
            [operation["path"] for operation in kube.patches[1][2]],
        )
        self.assertEqual(
            kube.pvc["metadata"]["annotations"][
                "capacity-resolver.homelab/notification-state"
            ],
            "completed",
        )

    def test_completed_alert_fingerprint_is_ignored_as_duplicate(self) -> None:
        instance, kube, _, _ = self.make_resolver(mode="active", mutation_enabled=True)
        instance.process_alert(firing_alert())
        repeat_notifier = FakeNotifier()
        repeat, _, _, _ = self.make_resolver(
            mode="active",
            mutation_enabled=True,
            kube=kube,
            notifier=repeat_notifier,
        )

        decision = repeat.process_alert(firing_alert())

        self.assertEqual(decision.outcome, "ignore")
        self.assertEqual(decision.reason, "duplicate-alert")
        self.assertEqual(repeat_notifier.messages, [])
        self.assertEqual(len(kube.patches), 2)

    def test_new_alert_episode_can_expand_after_cooldown(self) -> None:
        instance, kube, _, _ = self.make_resolver(mode="active", mutation_enabled=True)
        instance.process_alert(firing_alert())
        kube.pvc["status"]["capacity"]["storage"] = "16Gi"
        kube.volume["spec"]["size"] = str(16 * Gi)
        later_alert = firing_alert()
        later_alert["startsAt"] = "2026-09-14T20:30:00Z"
        later, _, later_notifier, _ = self.make_resolver(
            mode="active",
            mutation_enabled=True,
            kube=kube,
            clock=NOW + timedelta(hours=25),
        )

        decision = later.process_alert(later_alert)

        self.assertEqual(decision.outcome, "expand")
        self.assertEqual(decision.old_size, "16Gi")
        self.assertEqual(decision.new_size, "20Gi")
        self.assertEqual(
            [message["phase"] for message in later_notifier.messages],
            ["proposed", "completed"],
        )
        self.assertEqual(len(kube.patches), 4)

    def test_refuses_at_cap(self) -> None:
        kube = FakeKubernetes()
        kube.pvc["spec"]["resources"]["requests"]["storage"] = "20Gi"
        kube.pvc["status"]["capacity"]["storage"] = "20Gi"
        instance, _, notifier, _ = self.make_resolver(kube=kube)

        decision = instance.process_alert(firing_alert())

        self.assertEqual(decision.outcome, "refuse")
        self.assertEqual(decision.reason, "maximum-size-reached")
        self.assertEqual(notifier.messages[0]["phase"], "refused")

    def test_refuses_while_previous_resize_is_still_converging(self) -> None:
        kube = FakeKubernetes()
        kube.pvc["spec"]["resources"]["requests"]["storage"] = "16Gi"
        kube.pvc["status"]["capacity"]["storage"] = "12Gi"
        instance, _, _, _ = self.make_resolver(kube=kube)

        decision = instance.process_alert(firing_alert())

        self.assertEqual(decision.outcome, "refuse")
        self.assertEqual(decision.reason, "expansion-in-progress")

    def test_refuses_during_persistent_cooldown(self) -> None:
        kube = FakeKubernetes()
        kube.pvc["metadata"]["annotations"] = {
            "capacity-resolver.homelab/last-expanded-at": "2026-09-13T08:00:00Z"
        }
        instance, _, _, _ = self.make_resolver(kube=kube)

        decision = instance.process_alert(firing_alert())

        self.assertEqual(decision.outcome, "refuse")
        self.assertEqual(decision.reason, "cooldown-active")

    def test_refuses_storage_class_without_longhorn_expansion(self) -> None:
        kube = FakeKubernetes()
        kube.storage_class["allowVolumeExpansion"] = False
        instance, _, _, _ = self.make_resolver(kube=kube)

        decision = instance.process_alert(firing_alert())

        self.assertEqual(decision.outcome, "refuse")
        self.assertEqual(decision.reason, "storage-class-not-expandable")

    def test_refuses_unhealthy_longhorn_volume(self) -> None:
        kube = FakeKubernetes()
        kube.volume["status"]["robustness"] = "degraded"
        instance, _, _, _ = self.make_resolver(kube=kube)

        decision = instance.process_alert(firing_alert())

        self.assertEqual(decision.outcome, "refuse")
        self.assertEqual(decision.reason, "longhorn-volume-not-healthy")

    def test_refuses_replica_count_mismatch(self) -> None:
        kube = FakeKubernetes()
        kube.volume["spec"]["numberOfReplicas"] = 2
        instance, _, _, _ = self.make_resolver(kube=kube)

        decision = instance.process_alert(firing_alert())

        self.assertEqual(decision.outcome, "refuse")
        self.assertEqual(decision.reason, "replica-count-mismatch")

    def test_refuses_unschedulable_replica_disk(self) -> None:
        kube = FakeKubernetes()
        disk = kube.nodes["timmy"]["status"]["diskStatus"]["default-disk"]
        disk["conditions"][1]["status"] = "False"
        instance, _, _, _ = self.make_resolver(kube=kube)

        decision = instance.process_alert(firing_alert())

        self.assertEqual(decision.outcome, "refuse")
        self.assertEqual(decision.reason, "disk-not-schedulable")
        self.assertFalse(decision.headroom_allowed)

    def test_refuses_when_projected_physical_free_space_crosses_floor(self) -> None:
        kube = FakeKubernetes()
        disk = kube.nodes["timmy"]["status"]["diskStatus"]["default-disk"]
        disk["storageAvailable"] = 126 * Gi  # 25% floor is 125Gi; -4Gi fails.
        instance, _, _, _ = self.make_resolver(kube=kube)

        decision = instance.process_alert(firing_alert())

        self.assertEqual(decision.outcome, "refuse")
        self.assertEqual(decision.reason, "insufficient-physical-headroom")

    def test_refuses_when_projected_scheduled_ratio_exceeds_limit(self) -> None:
        kube = FakeKubernetes()
        disk = kube.nodes["timmy"]["status"]["diskStatus"]["default-disk"]
        disk["storageScheduled"] = 398 * Gi
        instance, _, _, _ = self.make_resolver(kube=kube)

        decision = instance.process_alert(firing_alert())

        self.assertEqual(decision.outcome, "refuse")
        self.assertEqual(decision.reason, "scheduled-capacity-limit")


class RepositorySafetyContractTests(unittest.TestCase):
    def test_committed_rollout_is_dry_run_and_exactly_scoped(self) -> None:
        directory = Path(__file__).parent
        policy = resolver_module.Policy.from_file(directory / "policy.json")
        manifests = (directory / "manifests.yaml").read_text(encoding="utf-8")

        self.assertEqual(policy.mode, "dry-run")
        self.assertEqual(
            set(policy.allowlist),
            {("viking", "llama-cuda-model-cache")},
        )
        allowed = policy.allowlist[("viking", "llama-cuda-model-cache")]
        self.assertEqual(allowed.increment_bytes, 4 * Gi)
        self.assertEqual(allowed.maximum_bytes, 20 * Gi)
        self.assertRegex(
            manifests,
            r'CAPACITY_RESOLVER_MUTATION_ENABLED\n\s+value: "false"',
        )
        self.assertIn('resourceNames: ["llama-cuda-model-cache"]', manifests)
        self.assertIn(
            "value: http://prom-prometheus.grafana.svc:9090",
            manifests,
        )
        self.assertNotIn("prom-kube-prometheus-stack-prometheus", manifests)
        self.assertNotIn('resources: ["persistentvolumeclaims"]\n    verbs:', manifests)

    def test_committed_denylist_covers_unbounded_growth_volumes(self) -> None:
        policy = resolver_module.Policy.from_file(Path(__file__).parent / "policy.json")
        for namespace, pvc in [
            ("grafana", "prometheus-prom-prometheus-db-prometheus-prom-prometheus-0"),
            ("grafana", "storage-loki-0"),
            ("yt-dlp", "media"),
            ("copyparty", "copyparty-files"),
            ("garage", "data-garage-0"),
            ("harbor", "database-data-harbor-database-0"),
            ("coach", "postgres-data"),
            ("omnipendium", "omnipendium-db-data"),
            ("viking", "openviking-data"),
            ("viking", "ov-vectordb-data"),
        ]:
            with self.subTest(pvc=f"{namespace}/{pvc}"):
                self.assertIsNotNone(policy.denied(namespace, pvc))
        self.assertIsNone(policy.denied("viking", "llama-cuda-model-cache"))

    def test_slack_transport_matches_the_live_bot_token_secret(self) -> None:
        """IMPR-1173 retired alertmanager-slack-webhook; referencing it would
        leave the pod in CreateContainerConfigError and make deploy.sh refuse."""
        directory = Path(__file__).parent
        manifests = (directory / "manifests.yaml").read_text(encoding="utf-8")
        deploy = (directory / "deploy.sh").read_text(encoding="utf-8")

        self.assertNotIn("alertmanager-slack-webhook", manifests + deploy)
        self.assertIn("name: alertmanager-slack-bot-token", manifests)
        self.assertIn("alertmanager-slack-bot-token", deploy)

    def test_deploy_script_never_expands_an_empty_array(self) -> None:
        """macOS bash 3.2 aborts on an empty "${a[@]}" under set -u."""
        deploy = (Path(__file__).parent / "deploy.sh").read_text(encoding="utf-8")

        self.assertNotRegex(deploy, r"(?m)^dry_run=\(\)")
        self.assertIn("dry_run=(--dry-run=none)", deploy)


class DenylistAndPolicyValidationTests(unittest.TestCase):
    """Gate 2 and the config guards: a bad policy must refuse to load."""

    def test_denied_pvc_is_refused_before_the_allowlist_lookup(self) -> None:
        kube = mock.Mock()
        notifier = FakeNotifier()
        instance = resolver_module.Resolver(
            policy=resolver_module.Policy.from_dict(base_policy()),
            kubernetes=kube,
            usage_provider=mock.Mock(return_value=99.0),
            notifier=notifier,
            mutation_enabled=True,
            clock=lambda: NOW,
        )

        decision = instance.process_alert(
            firing_alert(namespace="grafana", pvc="storage-loki-0")
        )

        self.assertEqual((decision.outcome, decision.reason), ("refuse", "deny-listed"))
        kube.get_pvc.assert_not_called()
        kube.patch_pvc.assert_not_called()

    def test_allowlisting_a_denied_pvc_fails_to_load(self) -> None:
        policy = base_policy()
        policy["allowlist"][0]["namespace"] = "grafana"
        with self.assertRaisesRegex(ValueError, "matches denylist"):
            resolver_module.Policy.from_dict(policy)

    def test_invalid_policies_fail_to_load(self) -> None:
        cases = {
            "missing denylist": lambda p: p.pop("denylist"),
            "empty denylist": lambda p: p.update(denylist=[]),
            "blank deny rule": lambda p: p["denylist"].append({"namespace": "x"}),
            "unknown mode": lambda p: p.update(mode="live"),
            "usage above 100": lambda p: p.update(minimumUsagePercent=101),
            "zero cooldown": lambda p: p.update(cooldownHours=0),
            "free percent 100": lambda p: p["headroom"].update(minimumFreePercent=100),
            "scheduled 0": lambda p: p["headroom"].update(maximumScheduledPercent=0),
            "increment at cap": lambda p: p["allowlist"][0].update(increment="20Gi"),
            "decimal unit": lambda p: p["allowlist"][0].update(maximumSize="20G"),
            "empty identity": lambda p: p["allowlist"][0].update(storageClass=""),
            "missing field": lambda p: p["allowlist"][0].pop("maximumSize"),
            "duplicate entry": lambda p: p["allowlist"].append(
                copy.deepcopy(p["allowlist"][0])
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                policy = base_policy()
                mutate(policy)
                with self.assertRaises((TypeError, ValueError)):
                    resolver_module.Policy.from_dict(policy)


class SlackNotifierTests(unittest.TestCase):
    def decision(self):
        return resolver_module.Decision(
            outcome="would-expand",
            reason="dry-run",
            namespace="viking",
            pvc="llama-cuda-model-cache",
        )

    def post(self, body: bytes, status: int = 200):
        response = mock.MagicMock()
        response.status = status
        response.read.return_value = body
        response.__enter__.return_value = response
        notifier = resolver_module.SlackNotifier(
            "https://slack.test/api/chat.postMessage", "xoxb-test", "#cron-homelab"
        )
        with mock.patch.object(
            resolver_module.urllib.request, "urlopen", return_value=response
        ) as urlopen:
            notifier.notify(self.decision(), "proposed")
        return urlopen.call_args.args[0]

    def test_posts_to_the_channel_with_the_bot_token(self) -> None:
        request = self.post(b'{"ok": true, "ts": "1.2"}')

        self.assertEqual(request.full_url, "https://slack.test/api/chat.postMessage")
        self.assertEqual(request.get_header("Authorization"), "Bearer xoxb-test")
        payload = json.loads(request.data)
        self.assertEqual(payload["channel"], "#cron-homelab")
        self.assertIn("decision=would-expand", payload["text"])

    def test_ok_false_on_http_200_is_a_failure(self) -> None:
        """chat.postMessage rejects with HTTP 200; only `ok` tells."""
        with self.assertRaisesRegex(
            resolver_module.NotificationError, "channel_not_found"
        ):
            self.post(b'{"ok": false, "error": "channel_not_found"}')

    def test_non_json_body_is_a_failure(self) -> None:
        with self.assertRaises(resolver_module.NotificationError):
            self.post(b"ok")

    def test_http_error_status_is_a_failure(self) -> None:
        with self.assertRaisesRegex(resolver_module.NotificationError, "HTTP 500"):
            self.post(b'{"ok": true}', status=500)

    def test_transport_error_is_a_failure(self) -> None:
        notifier = resolver_module.SlackNotifier("https://x", "t", "#c")
        with (
            mock.patch.object(
                resolver_module.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("refused"),
            ),
            self.assertRaises(resolver_module.NotificationError),
        ):
            notifier.notify(self.decision(), "proposed")


class WebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = mock.Mock()
        self.resolver.process_payload.return_value = [
            resolver_module.Decision(
                outcome="would-expand",
                reason="dry-run",
                namespace="viking",
                pvc="llama-cuda-model-cache",
            )
        ]
        self.server = resolver_module.make_server(
            ("127.0.0.1", 0), self.resolver, "test-token"
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path: str, *, token: str | None = None, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return urllib.request.urlopen(
            urllib.request.Request(
                self.base_url + path,
                data=data,
                headers=headers,
                method="POST" if data else "GET",
            ),
            timeout=2,
        )

    def test_health_endpoint(self) -> None:
        with self.request("/healthz") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response), {"status": "ok"})

    def test_rejects_unauthorized_webhook(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/alerts", payload={"alerts": []})
        self.assertEqual(raised.exception.code, 401)
        raised.exception.close()
        self.resolver.process_payload.assert_not_called()

    def test_accepts_authenticated_alertmanager_payload(self) -> None:
        payload = {"status": "firing", "alerts": [firing_alert()]}
        with self.request("/alerts", token="test-token", payload=payload) as response:
            self.assertEqual(response.status, 200)
            body = json.load(response)
        self.assertEqual(body["decisions"][0]["outcome"], "would-expand")
        self.resolver.process_payload.assert_called_once_with(payload)

    def test_rejects_malformed_json(self) -> None:
        request = urllib.request.Request(
            self.base_url + "/alerts",
            data=b"not-json",
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()


if __name__ == "__main__":
    unittest.main()
