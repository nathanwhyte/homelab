#!/usr/bin/env python3
"""Deny-first PVC capacity resolver for Alertmanager storage alerts.

The service accepts only KubePersistentVolumeFillingUp alerts, rechecks live
filesystem usage in Prometheus, validates an exact allowlist entry and a series
of Kubernetes/Longhorn safety gates, then either reports a dry-run decision or
patches the PVC. Mutation requires both policy mode=active and the independent
CAPACITY_RESOLVER_MUTATION_ENABLED=true environment switch.
"""

from __future__ import annotations

import hmac
import json
import logging
import math
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol, cast

LOG = logging.getLogger("capacity-auto-resolver")
SUPPORTED_ALERT = "KubePersistentVolumeFillingUp"
ANNOTATION_PREFIX = "capacity-resolver.homelab"
QUANTITY_RE = re.compile(r"^([1-9][0-9]*)(Ki|Mi|Gi|Ti)$")
UNIT_BYTES = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
}


class ResolverError(RuntimeError):
    """Base error for operational failures that Alertmanager should retry."""


class ExternalServiceError(ResolverError):
    """A required Kubernetes, Prometheus, or HTTP operation failed."""


class NotificationError(ResolverError):
    """A required Slack audit notification failed."""


@dataclass(frozen=True)
class HeadroomPolicy:
    minimum_free_bytes: int
    minimum_free_percent: int
    maximum_scheduled_percent: int


@dataclass(frozen=True)
class VolumePolicy:
    namespace: str
    pvc: str
    storage_class: str
    increment_bytes: int
    maximum_bytes: int


@dataclass(frozen=True)
class Policy:
    mode: str
    minimum_usage_percent: float
    cooldown: timedelta
    headroom: HeadroomPolicy
    allowlist: dict[tuple[str, str], VolumePolicy]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Policy:
        if not isinstance(value, dict):
            raise TypeError("policy must be an object")
        mode = value.get("mode", "dry-run")
        if mode not in {"dry-run", "active"}:
            raise ValueError("mode must be dry-run or active")

        minimum_usage = float(value.get("minimumUsagePercent", 85))
        if not 0 < minimum_usage <= 100:
            raise ValueError("minimumUsagePercent must be in (0, 100]")

        cooldown_hours = float(value.get("cooldownHours", 24))
        if cooldown_hours <= 0:
            raise ValueError("cooldownHours must be positive")

        raw_headroom = value.get("headroom")
        if not isinstance(raw_headroom, dict):
            raise TypeError("headroom must be an object")
        minimum_free_bytes = int(raw_headroom.get("minimumFreeBytes", 0))
        minimum_free_percent = int(raw_headroom.get("minimumFreePercent", 25))
        maximum_scheduled_percent = int(raw_headroom.get("maximumScheduledPercent", 80))
        if minimum_free_bytes < 0:
            raise ValueError("minimumFreeBytes cannot be negative")
        if not 0 <= minimum_free_percent < 100:
            raise ValueError("minimumFreePercent must be in [0, 100)")
        if not 0 < maximum_scheduled_percent <= 100:
            raise ValueError("maximumScheduledPercent must be in (0, 100]")

        raw_allowlist = value.get("allowlist")
        if not isinstance(raw_allowlist, list):
            raise TypeError("allowlist must be an array")
        allowlist: dict[tuple[str, str], VolumePolicy] = {}
        for item in raw_allowlist:
            if not isinstance(item, dict):
                raise TypeError("each allowlist entry must be an object")
            required = {
                "namespace",
                "persistentVolumeClaim",
                "storageClass",
                "increment",
                "maximumSize",
            }
            missing = sorted(required - item.keys())
            if missing:
                raise ValueError(f"allowlist entry missing: {', '.join(missing)}")
            volume_policy = VolumePolicy(
                namespace=str(item["namespace"]),
                pvc=str(item["persistentVolumeClaim"]),
                storage_class=str(item["storageClass"]),
                increment_bytes=parse_quantity(str(item["increment"])),
                maximum_bytes=parse_quantity(str(item["maximumSize"])),
            )
            if not all(
                (
                    volume_policy.namespace,
                    volume_policy.pvc,
                    volume_policy.storage_class,
                )
            ):
                raise ValueError("allowlist identity fields cannot be empty")
            if volume_policy.increment_bytes >= volume_policy.maximum_bytes:
                raise ValueError("allowlist increment must be smaller than maximumSize")
            key = (volume_policy.namespace, volume_policy.pvc)
            if key in allowlist:
                raise ValueError(f"duplicate allowlist entry: {key[0]}/{key[1]}")
            allowlist[key] = volume_policy

        return cls(
            mode=mode,
            minimum_usage_percent=minimum_usage,
            cooldown=timedelta(hours=cooldown_hours),
            headroom=HeadroomPolicy(
                minimum_free_bytes=minimum_free_bytes,
                minimum_free_percent=minimum_free_percent,
                maximum_scheduled_percent=maximum_scheduled_percent,
            ),
            allowlist=allowlist,
        )

    @classmethod
    def from_file(cls, path: str | Path) -> Policy:
        with Path(path).open(encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))


@dataclass
class Decision:
    outcome: str
    reason: str
    namespace: str = ""
    pvc: str = ""
    usage_percent: float | None = None
    old_size: str = "unknown"
    new_size: str = "none"
    maximum_size: str = "unknown"
    volume: str = "unknown"
    headroom_allowed: bool | None = None
    headroom_details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class KubernetesAPI(Protocol):
    def get_pvc(self, namespace: str, name: str) -> dict[str, Any]: ...

    def get_storage_class(self, name: str) -> dict[str, Any]: ...

    def get_longhorn_volume(self, name: str) -> dict[str, Any]: ...

    def list_longhorn_replicas(self, volume: str) -> list[dict[str, Any]]: ...

    def get_longhorn_node(self, name: str) -> dict[str, Any]: ...

    def patch_pvc(
        self, namespace: str, name: str, patch: list[dict[str, Any]]
    ) -> dict[str, Any]: ...


class UsageProvider(Protocol):
    def __call__(self, namespace: str, pvc: str) -> float: ...


class Notifier(Protocol):
    def notify(self, decision: Decision, phase: str) -> None: ...


def parse_quantity(value: str) -> int:
    match = QUANTITY_RE.fullmatch(value)
    if not match:
        raise ValueError(f"unsupported Kubernetes storage quantity: {value!r}")
    amount, unit = match.groups()
    return int(amount) * UNIT_BYTES[unit]


def format_quantity(value: int) -> str:
    if value <= 0:
        raise ValueError("storage quantity must be positive")
    for unit in ("Ti", "Gi", "Mi", "Ki"):
        divisor = UNIT_BYTES[unit]
        if value % divisor == 0:
            return f"{value // divisor}{unit}"
    raise ValueError("storage quantity is not an exact binary Kubernetes unit")


def parse_rfc3339(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp is missing timezone")
    return parsed.astimezone(UTC)


def utc_rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def condition_true(conditions: list[dict[str, Any]], condition_type: str) -> bool:
    return any(
        condition.get("type") == condition_type and condition.get("status") == "True"
        for condition in conditions
    )


class Resolver:
    def __init__(
        self,
        *,
        policy: Policy,
        kubernetes: KubernetesAPI,
        usage_provider: UsageProvider,
        notifier: Notifier,
        mutation_enabled: bool,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.policy = policy
        self.kubernetes = kubernetes
        self.usage_provider = usage_provider
        self.notifier = notifier
        self.mutation_enabled = mutation_enabled
        self.clock = clock or (lambda: datetime.now(UTC))

    def process_payload(self, payload: dict[str, Any]) -> list[Decision]:
        alerts = payload.get("alerts")
        if not isinstance(alerts, list):
            raise TypeError("Alertmanager payload must contain an alerts array")
        return [self.process_alert(alert) for alert in alerts]

    def process_alert(self, alert: dict[str, Any]) -> Decision:
        if not isinstance(alert, dict):
            raise TypeError("each Alertmanager alert must be an object")
        labels = alert.get("labels")
        labels = labels if isinstance(labels, dict) else {}
        alertname = str(labels.get("alertname", ""))
        namespace = str(labels.get("namespace", ""))
        pvc_name = str(labels.get("persistentvolumeclaim", ""))

        if alertname != SUPPORTED_ALERT:
            return Decision(
                outcome="ignore",
                reason="unsupported-alert",
                namespace=namespace,
                pvc=pvc_name,
            )
        if alert.get("status") != "firing":
            return Decision(
                outcome="ignore",
                reason="not-firing",
                namespace=namespace,
                pvc=pvc_name,
            )
        if not namespace or not pvc_name:
            return self._refuse(
                "missing-pvc-identity", namespace=namespace, pvc=pvc_name
            )
        if labels.get("alertgroup") != "storage" or labels.get("severity") not in {
            "warning",
            "critical",
        }:
            return self._refuse(
                "untrusted-alert-labels", namespace=namespace, pvc=pvc_name
            )
        fingerprint = alert.get("fingerprint")
        starts_at = alert.get("startsAt")
        if not isinstance(fingerprint, str) or not fingerprint:
            return self._refuse(
                "missing-alert-fingerprint", namespace=namespace, pvc=pvc_name
            )
        if not isinstance(starts_at, str) or not starts_at:
            return self._refuse(
                "missing-alert-start-time", namespace=namespace, pvc=pvc_name
            )
        try:
            parse_rfc3339(starts_at)
        except ValueError:
            return self._refuse(
                "invalid-alert-start-time", namespace=namespace, pvc=pvc_name
            )

        volume_policy = self.policy.allowlist.get((namespace, pvc_name))
        if volume_policy is None:
            return self._refuse("not-allowlisted", namespace=namespace, pvc=pvc_name)

        pvc = self.kubernetes.get_pvc(namespace, pvc_name)
        decision = Decision(
            outcome="refuse",
            reason="verification-incomplete",
            namespace=namespace,
            pvc=pvc_name,
            maximum_size=format_quantity(volume_policy.maximum_bytes),
        )

        metadata = pvc.get("metadata", {})
        spec = pvc.get("spec", {})
        status = pvc.get("status", {})
        if status.get("phase") != "Bound":
            return self._finish_refusal(decision, "pvc-not-bound")

        actual_storage_class = spec.get("storageClassName")
        if actual_storage_class != volume_policy.storage_class:
            return self._finish_refusal(decision, "storage-class-policy-mismatch")

        try:
            requested_bytes = parse_quantity(
                str(spec["resources"]["requests"]["storage"])
            )
            capacity_bytes = parse_quantity(str(status["capacity"]["storage"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ExternalServiceError("PVC has invalid storage quantities") from exc
        decision.old_size = format_quantity(requested_bytes)
        decision.volume = str(spec.get("volumeName", "")) or "unknown"

        resource_version = str(metadata.get("resourceVersion", ""))
        if not resource_version:
            raise ExternalServiceError("PVC resourceVersion is missing")
        raw_annotations = metadata.get("annotations")
        if raw_annotations is not None and not isinstance(raw_annotations, dict):
            raise ExternalServiceError("PVC annotations are invalid")
        annotations = raw_annotations or {}
        last_fingerprint = annotations.get(
            f"{ANNOTATION_PREFIX}/last-alert-fingerprint"
        )
        last_starts_at = annotations.get(f"{ANNOTATION_PREFIX}/last-alert-starts-at")
        last_target = annotations.get(f"{ANNOTATION_PREFIX}/last-target")
        notification_state = annotations.get(f"{ANNOTATION_PREFIX}/notification-state")

        # A patch can succeed just before the completion Slack request fails.
        # Persisting an outbox marker on the PVC lets Alertmanager's retry send
        # the missing completion without authorizing another expansion.
        if (
            last_fingerprint == fingerprint
            and last_starts_at == starts_at
            and last_target == decision.old_size
        ):
            if notification_state == "pending":
                decision.outcome = "expand"
                decision.reason = "patch-accepted"
                decision.old_size = str(
                    annotations.get(
                        f"{ANNOTATION_PREFIX}/last-previous-size", "unknown"
                    )
                )
                decision.new_size = str(last_target)
                try:
                    decision.usage_percent = float(
                        annotations[f"{ANNOTATION_PREFIX}/last-usage-percent"]
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise ExternalServiceError(
                        "pending audit is missing its measured usage"
                    ) from exc
                decision.headroom_allowed = True
                decision.headroom_details = [
                    "previous expansion approved; completing audit notification"
                ]
                self.notifier.notify(decision, "completed")
                self._mark_notification_completed(namespace, pvc_name, resource_version)
                self._log_decision(decision)
                return decision
            if notification_state == "completed":
                decision.outcome = "ignore"
                decision.reason = "duplicate-alert"
                decision.new_size = str(last_target)
                return decision

        usage_percent = float(self.usage_provider(namespace, pvc_name))
        if not math.isfinite(usage_percent) or not 0 <= usage_percent <= 100:
            raise ExternalServiceError(
                f"Prometheus returned invalid usage percent: {usage_percent!r}"
            )
        decision.usage_percent = usage_percent
        if usage_percent < self.policy.minimum_usage_percent:
            return self._finish_refusal(decision, "usage-below-threshold")

        if requested_bytes != capacity_bytes:
            return self._finish_refusal(decision, "expansion-in-progress")
        if requested_bytes >= volume_policy.maximum_bytes:
            return self._finish_refusal(decision, "maximum-size-reached")

        target_bytes = min(
            requested_bytes + volume_policy.increment_bytes,
            volume_policy.maximum_bytes,
        )
        decision.new_size = format_quantity(target_bytes)

        last_expanded = annotations.get(f"{ANNOTATION_PREFIX}/last-expanded-at")
        if last_expanded:
            try:
                in_cooldown = (
                    self.clock() - parse_rfc3339(str(last_expanded))
                    < self.policy.cooldown
                )
            except (TypeError, ValueError) as exc:
                raise ExternalServiceError(
                    "PVC has invalid capacity resolver cooldown annotation"
                ) from exc
            if in_cooldown:
                return self._finish_refusal(decision, "cooldown-active")

        storage_class = self.kubernetes.get_storage_class(actual_storage_class)
        if (
            storage_class.get("allowVolumeExpansion") is not True
            or storage_class.get("provisioner") != "driver.longhorn.io"
        ):
            return self._finish_refusal(decision, "storage-class-not-expandable")

        volume_name = str(spec.get("volumeName", ""))
        decision.volume = volume_name or "unknown"
        if not volume_name:
            return self._finish_refusal(decision, "pvc-volume-missing")

        volume = self.kubernetes.get_longhorn_volume(volume_name)
        volume_status = volume.get("status", {})
        if (
            volume_status.get("state") != "attached"
            or volume_status.get("robustness") != "healthy"
        ):
            return self._finish_refusal(decision, "longhorn-volume-not-healthy")
        try:
            longhorn_size = int(volume.get("spec", {}).get("size"))
        except (TypeError, ValueError) as exc:
            raise ExternalServiceError("Longhorn volume size is invalid") from exc
        if longhorn_size != requested_bytes:
            return self._finish_refusal(decision, "longhorn-size-mismatch")

        allowed, reason, details = self._check_headroom(
            volume_name=volume_name,
            volume=volume,
            delta_bytes=target_bytes - requested_bytes,
        )
        decision.headroom_allowed = allowed
        decision.headroom_details = details
        if not allowed:
            return self._finish_refusal(decision, reason)

        if self.policy.mode == "dry-run":
            decision.outcome = "would-expand"
            decision.reason = "dry-run"
            self.notifier.notify(decision, "dry-run")
            self._log_decision(decision)
            return decision

        if not self.mutation_enabled:
            return self._finish_refusal(decision, "mutation-switch-disabled")

        decision.outcome = "expand"
        decision.reason = "approved"
        # Fail closed: no irreversible patch occurs unless the proposed action
        # was first delivered to the audit channel.
        self.notifier.notify(decision, "proposed")

        operations: list[dict[str, Any]] = [
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": resource_version,
            },
            {
                "op": "test",
                "path": "/spec/resources/requests/storage",
                "value": decision.old_size,
            },
        ]
        if raw_annotations is None:
            operations.append(
                {"op": "add", "path": "/metadata/annotations", "value": {}}
            )
        action_annotations = {
            f"{ANNOTATION_PREFIX}/last-expanded-at": utc_rfc3339(self.clock()),
            f"{ANNOTATION_PREFIX}/last-previous-size": decision.old_size,
            f"{ANNOTATION_PREFIX}/last-target": decision.new_size,
            f"{ANNOTATION_PREFIX}/last-alert-fingerprint": fingerprint,
            f"{ANNOTATION_PREFIX}/last-alert-starts-at": starts_at,
            f"{ANNOTATION_PREFIX}/last-usage-percent": f"{usage_percent:.3f}",
            f"{ANNOTATION_PREFIX}/notification-state": "pending",
        }
        operations.extend(
            {
                "op": "add",
                "path": annotation_pointer(key),
                "value": value,
            }
            for key, value in action_annotations.items()
        )
        operations.append(
            {
                "op": "replace",
                "path": "/spec/resources/requests/storage",
                "value": decision.new_size,
            }
        )
        patched_pvc = self.kubernetes.patch_pvc(namespace, pvc_name, operations)
        patched_resource_version = str(
            patched_pvc.get("metadata", {}).get("resourceVersion", "")
        )
        if not patched_resource_version:
            raise ExternalServiceError(
                "patched PVC response is missing resourceVersion"
            )
        decision.reason = "patch-accepted"
        self.notifier.notify(decision, "completed")
        self._mark_notification_completed(namespace, pvc_name, patched_resource_version)
        self._log_decision(decision)
        return decision

    def _mark_notification_completed(
        self, namespace: str, pvc_name: str, resource_version: str
    ) -> None:
        state_path = annotation_pointer(f"{ANNOTATION_PREFIX}/notification-state")
        self.kubernetes.patch_pvc(
            namespace,
            pvc_name,
            [
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": resource_version,
                },
                {"op": "test", "path": state_path, "value": "pending"},
                {"op": "replace", "path": state_path, "value": "completed"},
            ],
        )

    def _check_headroom(
        self,
        *,
        volume_name: str,
        volume: dict[str, Any],
        delta_bytes: int,
    ) -> tuple[bool, str, list[str]]:
        try:
            expected_replicas = int(volume.get("spec", {}).get("numberOfReplicas"))
        except (TypeError, ValueError) as exc:
            raise ExternalServiceError("Longhorn replica count is invalid") from exc
        replicas = self.kubernetes.list_longhorn_replicas(volume_name)
        if expected_replicas <= 0 or len(replicas) != expected_replicas:
            return (
                False,
                "replica-count-mismatch",
                [f"replicas={len(replicas)}/{expected_replicas}"],
            )

        details: list[str] = []
        for replica in replicas:
            replica_spec = replica.get("spec", {})
            node_name = str(replica_spec.get("nodeID", ""))
            disk_uuid = str(replica_spec.get("diskID", ""))
            if not node_name or not disk_uuid:
                return False, "replica-placement-missing", details
            node = self.kubernetes.get_longhorn_node(node_name)
            disk_name, disk = self._find_disk(node, disk_uuid)
            disk_label = f"{node_name}/{disk_name}"
            conditions = disk.get("conditions") or []
            if not condition_true(conditions, "Ready"):
                details.append(f"{disk_label}: Ready=False")
                return False, "disk-not-ready", details
            if not condition_true(conditions, "Schedulable"):
                details.append(f"{disk_label}: Schedulable=False")
                return False, "disk-not-schedulable", details

            try:
                available = int(disk["storageAvailable"])
                maximum = int(disk["storageMaximum"])
                scheduled = int(disk["storageScheduled"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ExternalServiceError(
                    f"Longhorn disk accounting is invalid for {disk_label}"
                ) from exc
            if maximum <= 0:
                raise ExternalServiceError(
                    f"Longhorn disk maximum is invalid for {disk_label}"
                )

            projected_available = available - delta_bytes
            percent_floor = math.ceil(
                maximum * self.policy.headroom.minimum_free_percent / 100
            )
            free_floor = max(self.policy.headroom.minimum_free_bytes, percent_floor)
            if projected_available <= free_floor:
                details.append(
                    f"{disk_label}: projected-free={format_bytes(projected_available)} "
                    f"floor={format_bytes(free_floor)}"
                )
                return False, "insufficient-physical-headroom", details

            projected_scheduled = scheduled + delta_bytes
            if (
                projected_scheduled * 100
                > maximum * self.policy.headroom.maximum_scheduled_percent
            ):
                details.append(
                    f"{disk_label}: projected-scheduled="
                    f"{projected_scheduled * 100 / maximum:.1f}% "
                    f"limit={self.policy.headroom.maximum_scheduled_percent}%"
                )
                return False, "scheduled-capacity-limit", details

            details.append(
                f"{disk_label}: projected-free={format_bytes(projected_available)} "
                f"floor={format_bytes(free_floor)} projected-scheduled="
                f"{projected_scheduled * 100 / maximum:.1f}%"
            )

        return True, "headroom-approved", details

    @staticmethod
    def _find_disk(node: dict[str, Any], disk_uuid: str) -> tuple[str, dict[str, Any]]:
        disk_status = node.get("status", {}).get("diskStatus", {})
        for disk_name, disk in disk_status.items():
            if disk.get("diskUUID") == disk_uuid:
                return str(disk_name), disk
        raise ExternalServiceError(f"Longhorn disk UUID not found: {disk_uuid}")

    def _refuse(self, reason: str, **values: Any) -> Decision:
        return self._finish_refusal(Decision("refuse", reason, **values), reason)

    def _finish_refusal(self, decision: Decision, reason: str) -> Decision:
        decision.outcome = "refuse"
        decision.reason = reason
        self.notifier.notify(decision, "refused")
        self._log_decision(decision)
        return decision

    @staticmethod
    def _log_decision(decision: Decision) -> None:
        LOG.info(
            json.dumps({"event": "decision", **decision.to_dict()}, sort_keys=True)
        )


def annotation_pointer(key: str) -> str:
    escaped = key.replace("~", "~0").replace("/", "~1")
    return f"/metadata/annotations/{escaped}"


def format_bytes(value: int) -> str:
    if value < 0:
        return f"-{format_bytes(-value)}"
    for unit, divisor in (("TiB", 1024**4), ("GiB", 1024**3), ("MiB", 1024**2)):
        if value >= divisor:
            return f"{value / divisor:.1f}{unit}"
    return f"{value}B"


class InClusterKubernetesClient:
    def __init__(self) -> None:
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        if not host:
            raise RuntimeError("KUBERNETES_SERVICE_HOST is not set")
        self.base_url = f"https://{host}:{port}"
        token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
        ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
        self.token = token_path.read_text(encoding="utf-8").strip()
        self.ssl_context = ssl.create_default_context(cafile=str(ca_path))

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode()
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        if body is not None:
            headers["Content-Type"] = (
                "application/json-patch+json"
                if isinstance(body, list)
                else "application/merge-patch+json"
            )
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request, context=self.ssl_context, timeout=10
            ) as response:
                return json.load(response)
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ExternalServiceError(
                f"Kubernetes API {method} {path} failed: {exc}"
            ) from exc

    def get_pvc(self, namespace: str, name: str) -> dict[str, Any]:
        return self._request(
            f"/api/v1/namespaces/{quote(namespace)}/persistentvolumeclaims/{quote(name)}"
        )

    def get_storage_class(self, name: str) -> dict[str, Any]:
        return self._request(f"/apis/storage.k8s.io/v1/storageclasses/{quote(name)}")

    def get_longhorn_volume(self, name: str) -> dict[str, Any]:
        return self._request(
            "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/"
            + quote(name)
        )

    def list_longhorn_replicas(self, volume: str) -> list[dict[str, Any]]:
        selector = urllib.parse.urlencode({"labelSelector": f"longhornvolume={volume}"})
        response = self._request(
            "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/replicas?" + selector
        )
        items = response.get("items")
        if not isinstance(items, list):
            raise ExternalServiceError("Kubernetes replica list has no items array")
        return items

    def get_longhorn_node(self, name: str) -> dict[str, Any]:
        return self._request(
            "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes/" + quote(name)
        )

    def patch_pvc(
        self, namespace: str, name: str, patch: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return self._request(
            f"/api/v1/namespaces/{quote(namespace)}/persistentvolumeclaims/{quote(name)}",
            method="PATCH",
            body=patch,
        )


def quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


class PrometheusUsageProvider:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def __call__(self, namespace: str, pvc: str) -> float:
        namespace_matcher = json.dumps(namespace)
        pvc_matcher = json.dumps(pvc)
        selector = (
            'job="kubelet",metrics_path="/metrics",'
            f"namespace={namespace_matcher},persistentvolumeclaim={pvc_matcher}"
        )
        query = (
            f"100 * max(kubelet_volume_stats_used_bytes{{{selector}}} "
            f"/ kubelet_volume_stats_capacity_bytes{{{selector}}})"
        )
        url = (
            self.base_url + "/api/v1/query?" + urllib.parse.urlencode({"query": query})
        )
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                payload = json.load(response)
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ExternalServiceError(f"Prometheus usage query failed: {exc}") from exc
        try:
            results = payload["data"]["result"]
            if payload.get("status") != "success" or len(results) != 1:
                raise ValueError("query did not return exactly one result")
            return float(results[0]["value"][1])
        except (KeyError, TypeError, ValueError) as exc:
            raise ExternalServiceError(
                "Prometheus usage response was missing one numeric result"
            ) from exc


class SlackNotifier:
    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    def notify(self, decision: Decision, phase: str) -> None:
        usage = (
            "unknown"
            if decision.usage_percent is None
            else f"{decision.usage_percent:.1f}%"
        )
        headroom = (
            "unknown"
            if decision.headroom_allowed is None
            else "pass"
            if decision.headroom_allowed
            else "fail"
        )
        details = "; ".join(decision.headroom_details) or "not evaluated"
        text = (
            f"PVC capacity resolver — {phase}\n"
            f"decision={decision.outcome} reason={decision.reason}\n"
            f"pvc={decision.namespace}/{decision.pvc} volume={decision.volume}\n"
            f"usage={usage} old={decision.old_size} new={decision.new_size} "
            f"cap={decision.maximum_size}\n"
            f"headroom={headroom}: {details}"
        )
        request = urllib.request.Request(
            self.webhook_url,
            data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response_body = response.read().decode(errors="replace").strip()
                if response.status // 100 != 2:
                    raise NotificationError(f"Slack returned HTTP {response.status}")
                if response_body not in {"", "ok"}:
                    raise NotificationError("Slack rejected the notification")
        except NotificationError:
            raise
        except urllib.error.URLError as exc:
            raise NotificationError(f"Slack notification failed: {exc}") from exc


class ResolverHTTPServer(ThreadingHTTPServer):
    resolver: Any
    webhook_token: str


class ResolverHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json_response(200, {"status": "ok"})
            return
        self._json_response(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/alerts":
            self._json_response(404, {"error": "not found"})
            return
        server = cast(ResolverHTTPServer, self.server)
        authorization = self.headers.get("Authorization", "")
        expected = f"Bearer {server.webhook_token}"
        if not hmac.compare_digest(authorization, expected):
            self._json_response(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1024 * 1024:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise TypeError("payload must be an object")
            decisions = server.resolver.process_payload(payload)
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            self._json_response(400, {"error": str(exc)})
            return
        except ResolverError as exc:
            LOG.error("resolver dependency failed: %s", exc)
            self._json_response(503, {"error": "required dependency failed"})
            return
        except Exception:
            LOG.exception("unexpected resolver failure")
            self._json_response(500, {"error": "internal error"})
            return
        self._json_response(
            200, {"decisions": [decision.to_dict() for decision in decisions]}
        )

    def log_message(self, format: str, *args: Any) -> None:
        LOG.info("http client=%s message=%s", self.client_address[0], format % args)

    def _json_response(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def make_server(
    address: tuple[str, int], resolver: Resolver, webhook_token: str
) -> ResolverHTTPServer:
    server = ResolverHTTPServer(address, ResolverHandler)
    server.resolver = resolver
    server.webhook_token = webhook_token
    return server


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    if value.lower() in {"1", "true", "yes"}:
        return True
    if value.lower() in {"0", "false", "no"}:
        return False
    raise ValueError(f"{name} must be true or false")


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        policy = Policy.from_file(
            os.environ.get("CAPACITY_RESOLVER_POLICY", "/etc/resolver/policy.json")
        )
        webhook_token = os.environ["ALERTMANAGER_WEBHOOK_TOKEN"]
        slack_webhook = os.environ["SLACK_WEBHOOK_URL"]
        if len(webhook_token) < 32:
            raise ValueError(
                "ALERTMANAGER_WEBHOOK_TOKEN must be at least 32 characters"
            )
        mutation_enabled = env_bool("CAPACITY_RESOLVER_MUTATION_ENABLED", False)
        resolver = Resolver(
            policy=policy,
            kubernetes=InClusterKubernetesClient(),
            usage_provider=PrometheusUsageProvider(
                os.environ.get(
                    "PROMETHEUS_URL",
                    "http://prom-prometheus.grafana.svc:9090",
                )
            ),
            notifier=SlackNotifier(slack_webhook),
            mutation_enabled=mutation_enabled,
        )
        server = make_server(("0.0.0.0", 8080), resolver, webhook_token)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        LOG.error("startup failed: %s", exc)
        return 1

    LOG.info(
        "starting mode=%s mutation_enabled=%s allowlist_entries=%d",
        policy.mode,
        mutation_enabled,
        len(policy.allowlist),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
