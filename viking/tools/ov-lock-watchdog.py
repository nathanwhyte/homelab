#!/usr/bin/env python3
"""Long-hold watchdog for OpenViking lock leases (BUG-1220, BUG-1282).

On 2026-10-09..12 one OpenViking lease (an MCP session's memory extraction on
default/user/noot/sessions/<session>/.path.ovlock) refreshed every ~10 s for
~70 h while the rest of the queue kept completing work. Nothing restarted
OpenViking: the in-Job heal (compendium-sync, BUG-1181) restarts only for a
wedge its own run caused under resources/compendium, and the IMPR-1095 janitor
deletes dead leases only. This job restarts deploy/openviking when ONE lock has
been held by the SAME owner for longer than WATCHDOG_HOLD_SECONDS (3 h).

Identity, all of it required (any doubt = no restart, with the reason logged):

- ov-exporter's openviking_s3_ovlock_live_age_seconds{key,uri,owner,lock_type}
  (BUG-1220, homelab#205) says the lock has been continuously present for at
  least the hold threshold with a stable holder (the series restarts its age
  when owner or lock type changes) and is live-refreshing: a lease whose
  refreshes stopped leaves the series after the exporter's live window, which
  is the janitor's dead-lease case.
- The lock's owner is readable.
- Two reads of the exporter's GET /locks, WATCHDOG_RECHECK_SECONDS apart, show
  a later poll, the same key, owner and lock type, an owner read on each of
  those polls, not a cached one, and a newer mtime on the second. The exporter
  compares a lock's owner only from the poll after it is labelled, so a lock
  labelled by this run's first read has not been compared yet, and a frozen
  lock stays in the series for the exporter's live window.
- The exporter has known this owner for at least WATCHDOG_HOLD_SECONDS
  (owner_held_seconds). The series' age counts from when the key appeared, so
  it can span holders the exporter never read: before its 900 s label threshold,
  while reads fail, or past OVLOCK_LABEL_TOP_N. A lock therefore restarts about
  15 min later than its age alone suggests, and past the force cap on the
  shorter of the two.
- The exporter is healthy and fresh, the Deployment is settled and was not
  restarted within WATCHDOG_COOLDOWN_SECONDS.

The check is on the owner, not the lease (BUG-1282). v0.5.1.1 logs the ragfs
lease lines at debug, and lock tokens and `ov status` carry no lease_ref, so
nothing at the INFO level names a lease. A handoff keeps its owner and takes a
new lease_ref, so handoffs inside one owner read as one hold.

Disruption guard. BUG-1220's original wording was "restart when the queue is
not progressing", but in that incident the queue kept progressing around the one
wedged task, so that guard would never have fired. Instead: prefer a quiet
moment. Defer while the OV queue holds work (pending + in_progress of
queue="total", more than WATCHDOG_QUIET_MAX_WORK), and force the restart once
the hold exceeds WATCHDOG_FORCE_SECONDS (6 h) so a busy system cannot defer
forever. An unreadable queue counts as busy. The wedged task itself is not
subtracted: Prometheus showed queue="total" at 0 for hours (2026-10-10
12:05-15:20Z) while that lock was held, so a wedged extraction does not occupy
a queue slot, and subtracting one would hide a real task.

Safety: at most one restart per run no matter how many locks qualify; never
touches a lock file; never deletes anything; the PATCH carries the Deployment's
resourceVersion so a concurrent restart (the compendium heal, an operator, a
rollout) turns it into a no-op conflict instead of a second restart.
--dry-run logs the decision and stops short of the second read and the PATCH.

Env (all optional): WATCHDOG_HOLD_SECONDS=10800, WATCHDOG_FORCE_SECONDS=21600,
WATCHDOG_COOLDOWN_SECONDS=3600, WATCHDOG_QUIET_MAX_WORK=0,
WATCHDOG_RECHECK_SECONDS=30, WATCHDOG_EXPORTER_MAX_STALE_SECONDS=120,
WATCHDOG_EXPORTER_URL (GET /locks is read from the same host),
WATCHDOG_NAMESPACE, WATCHDOG_DEPLOYMENT.

Decisions (the run's `decision=` line, IMPR-1416):

- restart, defer: a lock held >= hold by one readable owner; the queue rule
  chose.
- held: a lock held >= hold was seen, but no restart: the cooldown, an
  unsettled rollout or unreadable restartedAt stopped it, or no long lock had a
  readable owner.
- none: usable lock series were read and no lock was held >= hold.
- blind: no lock series were read (the Deployment was unreadable, or the
  exporter was unusable, reported OpenViking down or its lock poll failing, or
  was stale).

A count of decision=none counts the runs that read usable lock series and saw
no lock held >= hold. The pre-restart recheck logs no second decision line; a
recheck that fails any gate logs `ABORTED before restart`.

Exit codes: 0 = a decision was made (restart, defer, held, none or blind, including
every "could not tell" case); 1 = a restart was attempted and failed, or an
unexpected error; 2 = blind (the ServiceAccount is refused by the Kubernetes API
- RBAC missing) or bad configuration. Non-zero fails the Job, which reaches
Slack through KubeJobFailed; a quiet "none" never does.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from lock_observations import observe_exporter

UTC = datetime.timezone.utc
TAG = "ov-lock-watchdog:"
RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"
LAST_RESTART_NOTE = "ov-lock-watchdog.nathanwhyte.dev/last-restart"
SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(UTC)


# --- configuration ----------------------------------------------------------


@dataclass(frozen=True)
class Config:
    hold: float = 10800.0
    force: float = 21600.0
    cooldown: float = 3600.0
    quiet_max_work: int = 0
    # Twice the exporter's 15 s poll, so the re-read lands on a later poll.
    recheck: float = 30.0
    exporter_max_stale: float = 120.0
    exporter_url: str = "http://openviking.viking.svc.cluster.local:9210/metrics"
    namespace: str = "viking"
    deployment: str = "openviking"

    @property
    def locks_url(self) -> str:
        """GET /locks on the exporter that serves exporter_url."""
        return self.exporter_url.rsplit("/", 1)[0] + "/locks"

    @classmethod
    def from_env(cls, env=os.environ) -> Config:
        d = cls()

        def num(name: str, default: float) -> float:
            raw = env.get(name)
            return default if raw in (None, "") else float(raw)

        cfg = cls(
            hold=num("WATCHDOG_HOLD_SECONDS", d.hold),
            force=num("WATCHDOG_FORCE_SECONDS", d.force),
            cooldown=num("WATCHDOG_COOLDOWN_SECONDS", d.cooldown),
            quiet_max_work=int(num("WATCHDOG_QUIET_MAX_WORK", d.quiet_max_work)),
            recheck=num("WATCHDOG_RECHECK_SECONDS", d.recheck),
            exporter_max_stale=num(
                "WATCHDOG_EXPORTER_MAX_STALE_SECONDS", d.exporter_max_stale
            ),
            exporter_url=env.get("WATCHDOG_EXPORTER_URL") or d.exporter_url,
            namespace=env.get("WATCHDOG_NAMESPACE") or d.namespace,
            deployment=env.get("WATCHDOG_DEPLOYMENT") or d.deployment,
        )
        if cfg.hold <= 0 or cfg.force < cfg.hold:
            raise ValueError(
                f"need 0 < WATCHDOG_HOLD_SECONDS ({cfg.hold}) <= "
                f"WATCHDOG_FORCE_SECONDS ({cfg.force})"
            )
        if cfg.recheck < 0:
            raise ValueError(f"need WATCHDOG_RECHECK_SECONDS >= 0, got {cfg.recheck}")
        return cfg


# --- exporter metrics -------------------------------------------------------


class ParseError(Exception):
    """The exporter text is not what this job needs; the reason is logged."""


@dataclass(frozen=True)
class LiveLock:
    key: str
    uri: str
    owner: str
    lock_type: str
    age: float


@dataclass(frozen=True)
class Exporter:
    up: bool
    lock_poll_ok: bool
    last_poll: float
    locks: tuple[LiveLock, ...]
    # pending + in_progress of the queue="total" row; None = not exported.
    queue_work: int | None


def parse_exporter(
    text: str, *, source: str = "exporter", observed_at: float | None = None
) -> Exporter:
    """Adapt shared exporter facts to this watchdog's existing required evidence."""
    observation = observe_exporter(text, source=source, observed_at=observed_at)
    try:
        locks = observation.require_locks()
    except ValueError as e:
        raise ParseError(str(e)) from e
    # The oldest-age aggregate is report-only; this job has never required it.
    errors = [str(d) for d in observation.diagnostics if d.field != "oldest_age"]
    if errors:
        raise ParseError("; ".join(errors))
    if any(
        v is None
        for v in (observation.up, observation.lock_poll_ok, observation.last_poll)
    ):
        raise ParseError(
            "exporter does not publish health, lock-poll status or poll time"
        )
    pending = {k.lower(): v for k, v in observation.pending.items()}
    in_progress = {k.lower(): v for k, v in observation.in_progress.items()}
    queue_work = (
        int(pending["total"] + in_progress["total"])
        if "total" in pending and "total" in in_progress
        else None
    )
    return Exporter(
        up=observation.up,
        lock_poll_ok=observation.lock_poll_ok,
        last_poll=observation.last_poll,
        locks=tuple(
            LiveLock(lock.key, lock.uri, lock.owner, lock.lock_type, lock.age)
            for lock in locks
        ),
        queue_work=queue_work,
    )


@dataclass(frozen=True)
class LockSample:
    """One lock in one GET /locks snapshot: all of it from a single poll."""

    generation: int
    owner: str
    lock_type: str
    owner_read_ok: bool
    last_modified: float
    owner_held: float


def _finite(value) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"not a finite number: {value!r}")
    return number


def parse_lock_sample(text: str, key: str) -> LockSample:
    """`key`'s sample from a GET /locks body. ParseError if there is none."""
    try:
        doc = json.loads(text)
        if doc["ok"] is not True:
            raise ParseError("the exporter's lock poll is failing")
        for lock in doc["locks"]:
            if lock["key"] == key:
                return LockSample(
                    generation=int(doc["generation"]),
                    owner=str(lock.get("owner", "")),
                    lock_type=str(lock.get("lock_type", "")),
                    owner_read_ok=lock.get("owner_read_ok") is True,
                    last_modified=_finite(lock["last_modified"]),
                    owner_held=_finite(lock.get("owner_held_seconds", 0.0)),
                )
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise ParseError(f"unreadable GET /locks reply: {e!r}") from e
    raise ParseError("lock gone from GET /locks")


def proof_failure(
    lock: LiveLock, first: LockSample, second: LockSample, hold: float
) -> str:
    """Why two /locks samples do not show `lock` held and refreshing, else ""."""
    if second.generation <= first.generation:
        return "the exporter has not polled again since the first read"
    if not (first.owner_read_ok and second.owner_read_ok):
        return "an owner read failed, so the owner is a cached value"
    holder = (lock.owner, lock.lock_type)
    if holder != (first.owner, first.lock_type) or holder != (
        second.owner,
        second.lock_type,
    ):
        return "lock gone or changed"
    if second.last_modified <= first.last_modified:
        return "the lock was not refreshed between the two reads"
    if second.owner_held < hold:
        return (
            f"the exporter has known this owner for {int(second.owner_held)}s, "
            f"under the {int(hold)}s hold"
        )
    return ""


# --- Kubernetes -------------------------------------------------------------


class K8sError(Exception):
    """A Kubernetes API call failed. `blind` = the SA is refused (401/403)."""

    def __init__(self, message: str, blind: bool = False, conflict: bool = False):
        super().__init__(message)
        self.blind = blind
        self.conflict = conflict


@dataclass(frozen=True)
class DeployState:
    resource_version: str
    settled: bool
    unsettled_why: str
    restarted_at: datetime.datetime | None
    restarted_at_error: str


def parse_deployment(doc: dict) -> DeployState:
    """Deployment JSON -> DeployState (rollout settled? last restartedAt?)."""
    try:
        meta = doc["metadata"]
        spec_replicas = (doc.get("spec") or {}).get("replicas", 1)
        status = doc.get("status") or {}
        annotations = (
            ((doc.get("spec") or {}).get("template") or {}).get("metadata") or {}
        ).get("annotations") or {}
        generation = meta.get("generation")
        why = ""
        if spec_replicas == 0:
            why = "scaled to 0"
        elif status.get("observedGeneration") != generation:
            why = "a spec change has not been observed yet"
        elif not (
            status.get("replicas", 0)
            == status.get("updatedReplicas", 0)
            == status.get("availableReplicas", 0)
            == spec_replicas
        ):
            why = "rollout in progress or replicas unavailable"
        restarted_at = None
        err = ""
        raw = annotations.get(RESTARTED_AT)
        if raw:
            try:
                restarted_at = datetime.datetime.fromisoformat(raw)
                if restarted_at.tzinfo is None:
                    restarted_at, err = None, f"naive {RESTARTED_AT} {raw!r}"
            except ValueError:
                err = f"unparseable {RESTARTED_AT} {raw!r}"
        return DeployState(
            resource_version=str(meta["resourceVersion"]),
            settled=not why,
            unsettled_why=why,
            restarted_at=restarted_at,
            restarted_at_error=err,
        )
    except (KeyError, TypeError, AttributeError) as e:
        raise K8sError(f"unreadable Deployment document: {e!r}") from e


class Cluster:
    """The Kubernetes calls this job makes, through its ServiceAccount token."""

    def __init__(self, namespace: str, deployment: str) -> None:
        self.namespace = namespace
        self.deployment = deployment

    def _call(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = "",
    ) -> bytes:
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        token_file = SA_DIR / "token"
        if not host or not token_file.exists():
            raise K8sError("not in-cluster with a ServiceAccount token", blind=True)
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        req = urllib.request.Request(
            f"https://{host}:{port}{path}", data=body, method=method
        )
        req.add_header("Authorization", f"Bearer {token_file.read_text().strip()}")
        if content_type:
            req.add_header("Content-Type", content_type)
        ca = SA_DIR / "ca.crt"
        ctx = (
            ssl.create_default_context(cafile=str(ca))
            if ca.exists()
            else ssl.create_default_context()
        )
        endpoint = path.split("?", 1)[0]
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise K8sError(
                    f"HTTP {e.code} on {method} {endpoint}: the ov-lock-watchdog "
                    "ServiceAccount lacks this permission (viking/manifests/"
                    "ov-lock-watchdog-rbac.yaml)",
                    blind=True,
                ) from e
            raise K8sError(
                f"HTTP {e.code} on {method} {endpoint}", conflict=e.code == 409
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise K8sError(f"{method} {endpoint}: {str(e)[:200]}") from e

    def _deploy_path(self) -> str:
        return (
            f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{self.deployment}"
        )

    def get_deployment(self) -> DeployState:
        body = self._call("GET", self._deploy_path())
        try:
            return parse_deployment(json.loads(body))
        except ValueError as e:
            raise K8sError("unreadable Deployment JSON") from e

    def restart(self, resource_version: str, note: str, now: datetime.datetime) -> str:
        """Rolling-restart: what `kubectl rollout restart` does, as a PATCH.

        resourceVersion in the patch body is an optimistic-concurrency
        precondition: the API server answers 409 if the Deployment changed
        since it was read, so a concurrent restart cannot be doubled up.
        """
        patch = json.dumps(
            {
                "metadata": {
                    "resourceVersion": resource_version,
                    "annotations": {LAST_RESTART_NOTE: note},
                },
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                RESTARTED_AT: now.strftime("%Y-%m-%dT%H:%M:%SZ")
                            }
                        }
                    }
                },
            }
        ).encode()
        self._call(
            "PATCH",
            self._deploy_path(),
            body=patch,
            content_type="application/strategic-merge-patch+json",
        )
        return "patched"


# --- decision ---------------------------------------------------------------


@dataclass(frozen=True)
class LockReport:
    lock: LiveLock
    qualified: bool
    reason: str


@dataclass(frozen=True)
class Decision:
    action: str  # "restart" | "defer" | "held" | "none" | "blind"
    reason: str
    locks: tuple[LockReport, ...] = field(default_factory=tuple)

    @property
    def qualified(self) -> tuple[LockReport, ...]:
        return tuple(r for r in self.locks if r.qualified)


def _stop(reason: str) -> tuple[Decision, list[LiveLock]]:
    return Decision("none", reason), []


def _blind(reason: str) -> tuple[Decision, list[LiveLock]]:
    """A stop before the lock series could be read: the run observed nothing,
    so it must not count as a clean "none" (IMPR-1416 row 2)."""
    return Decision("blind", reason), []


def _held(reason: str) -> tuple[Decision, list[LiveLock]]:
    """A stop after a lock held >= hold was seen: not a clean "none" either."""
    return Decision("held", reason), []


def gate(
    cfg: Config,
    now: datetime.datetime,
    deploy: DeployState | None,
    exporter: Exporter | None,
    errors: dict[str, str],
) -> tuple[Decision, list[LiveLock]]:
    """Everything but the per-lock owner and queue rules. (Decision, long locks):
    the locks are empty exactly when the Decision is already final."""
    if deploy is None:
        return _blind(f"deployment unreadable: {errors.get('deploy', 'unknown')}")
    if exporter is None:
        return _blind(f"exporter unusable: {errors.get('exporter', 'unknown')}")
    if not exporter.up or not exporter.lock_poll_ok:
        return _blind(
            "exporter reports OpenViking down or the lock poll failing "
            f"(up={int(exporter.up)}, lock_poll_ok={int(exporter.lock_poll_ok)}); "
            "its lock series are stale"
        )
    stale = now.timestamp() - exporter.last_poll
    if stale > cfg.exporter_max_stale or stale < -cfg.exporter_max_stale:
        return _blind(f"exporter last polled {int(stale)}s ago; its series are stale")
    longs = sorted(
        (lock for lock in exporter.locks if lock.age >= cfg.hold),
        key=lambda lock: -lock.age,
    )
    if not longs:
        longest = max((lock.age for lock in exporter.locks), default=0.0)
        return _stop(
            f"no lock held >= {int(cfg.hold)}s (longest labelled: {int(longest)}s)"
        )
    if deploy.restarted_at_error:
        return _held(f"cannot judge the cooldown: {deploy.restarted_at_error}")
    if not deploy.settled:
        return _held(f"deployment not settled: {deploy.unsettled_why}")
    if deploy.restarted_at is not None:
        since = (now - deploy.restarted_at).total_seconds()
        if since < cfg.cooldown:
            return _held(
                f"cooldown: deployment restarted {int(since)}s ago "
                f"(< {int(cfg.cooldown)}s)"
            )
    return Decision("none", ""), longs


def decide(
    cfg: Config,
    now: datetime.datetime,
    deploy: DeployState | None,
    exporter: Exporter | None,
    errors: dict[str, str] | None = None,
) -> Decision:
    """The first read's policy, free of I/O. See the module docstring."""
    errors = errors or {}
    stop, longs = gate(cfg, now, deploy, exporter, errors)
    if not longs:
        return stop
    reports = tuple(
        LockReport(lock, False, "owner unknown (lock token unreadable)")
        if lock.owner in ("", "unknown")
        else LockReport(lock, True, "one owner for the whole hold, per the exporter")
        for lock in longs
    )
    qualified = [r for r in reports if r.qualified]
    if not qualified:
        return Decision(
            "held",
            f"{len(reports)} lock(s) held >= {int(cfg.hold)}s, none with a readable "
            "owner",
            reports,
        )
    longest = max(r.lock.age for r in qualified)
    work = exporter.queue_work if exporter else None
    return queue_policy(cfg, longest, work, reports)


def queue_policy(
    cfg: Config, longest: float, work: int | None, reports: tuple[LockReport, ...]
) -> Decision:
    """Restart, or defer, for a one-owner lock held `longest` seconds (>= hold)
    with `work` queue items (None = unreadable). Applied again to fresh metrics
    just before a restart, so a queue that turned busy defers it."""
    queue = (
        "queue unreadable (counted as busy)"
        if work is None
        else f"{work} queue item(s) pending or in progress"
    )
    if longest >= cfg.force:
        return Decision(
            "restart",
            f"forced: held {int(longest)}s >= {int(cfg.force)}s cap; {queue}",
            reports,
        )
    if work is not None and work <= cfg.quiet_max_work:
        return Decision(
            "restart",
            f"quiet moment: held {int(longest)}s >= {int(cfg.hold)}s; {queue}",
            reports,
        )
    return Decision(
        "defer",
        f"busy: held {int(longest)}s >= {int(cfg.hold)}s but {queue}; forced "
        f"restart in {int(cfg.force - longest)}s at {int(cfg.force)}s",
        reports,
    )


# --- run --------------------------------------------------------------------


def _describe(report: LockReport) -> str:
    return (
        f"{TAG} lock key={report.lock.key} uri={report.lock.uri} "
        f"owner={report.lock.owner} lock_type={report.lock.lock_type} "
        f"hold={int(report.lock.age)}s "
        f"identity={'same-owner' if report.qualified else 'REJECTED'} "
        f"reason={report.reason!r}"
    )


def same_holder(lock: LiveLock, locks: list[LiveLock]) -> LiveLock | None:
    """`lock` as re-read in `locks`: same key, owner and lock type."""
    for other in locks:
        if (other.key, other.owner, other.lock_type) == (
            lock.key,
            lock.owner,
            lock.lock_type,
        ):
            return other
    return None


def fetch_exporter_text(url: str, timeout: float = 10.0) -> str:
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def run(
    cfg: Config,
    cluster,
    fetch,
    *,
    dry_run: bool,
    now_fn=utcnow,
    say=None,
    sleep=time.sleep,
) -> int:
    say = say or (lambda line: print(line, flush=True))
    mode = "dry-run" if dry_run else "apply"
    now = now_fn()
    errors: dict[str, str] = {}
    blind = False

    deploy = None
    try:
        deploy = cluster.get_deployment()
    except K8sError as e:
        errors["deploy"] = str(e)
        blind = blind or e.blind

    exporter = None
    try:
        exporter = parse_exporter(
            fetch(cfg.exporter_url),
            source=cfg.exporter_url,
            observed_at=now.timestamp(),
        )
    except Exception as e:  # noqa: BLE001 - any failure is "exporter unusable"
        # Keep every parser diagnostic, including failures after the first row.
        errors["exporter"] = f"{type(e).__name__}: {e}"

    decision = decide(cfg, now, deploy, exporter, errors)

    for report in decision.locks:
        say(_describe(report))
    say(f"{TAG} decision={decision.action} mode={mode} reason={decision.reason!r}")
    if decision.action != "restart":
        if blind:
            say(f"{TAG} BLIND: cannot read the cluster as this ServiceAccount")
        return 2 if blind else 0

    first = decision.qualified[0]
    note = (
        f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')} key={first.lock.key} "
        f"owner={first.lock.owner} hold={int(first.lock.age)}s "
        f"reason={decision.reason}"
    )
    if dry_run:
        say(f"{TAG} DRY-RUN would restart deploy/{cfg.deployment}: {note}")
        return 0
    # Re-read everything the decision rests on, on a later exporter poll: a
    # restart is the one step that cannot be undone. A failed re-read is an
    # abort (retry in 15 min), not a failed restart.
    try:
        sample = parse_lock_sample(fetch(cfg.locks_url), first.lock.key)
        sleep(cfg.recheck)
        fresh = cluster.get_deployment()
        exporter_again = parse_exporter(
            fetch(cfg.exporter_url),
            source=cfg.exporter_url,
            observed_at=now_fn().timestamp(),
        )
        again, still_long = gate(cfg, now_fn(), fresh, exporter_again, errors)
        if not still_long:
            say(f"{TAG} ABORTED before restart: {again.reason}")
            return 0
        same = same_holder(first.lock, still_long)
        if same is None:
            say(f"{TAG} ABORTED before restart: lock gone or changed")
            return 0
        last = parse_lock_sample(fetch(cfg.locks_url), first.lock.key)
        why = proof_failure(first.lock, sample, last, cfg.hold)
        if why:
            say(f"{TAG} ABORTED before restart: {why}")
            return 0
        # The quiet-queue / force decision too, on the fresh queue count.
        policy = queue_policy(
            cfg,
            min(same.age, last.owner_held),
            exporter_again.queue_work,
            decision.locks,
        )
        if policy.action != "restart":
            say(f"{TAG} ABORTED before restart: {policy.reason}")
            return 0
    except Exception as e:  # noqa: BLE001 - nothing was changed; try next slot
        say(f"{TAG} ABORTED before restart: {type(e).__name__}: {e}")
        return 2 if getattr(e, "blind", False) else 0
    try:
        cluster.restart(fresh.resource_version, note, now_fn())
    except K8sError as e:
        outcome = "lost a race (409), nothing restarted" if e.conflict else "FAILED"
        say(f"{TAG} restart {outcome}: {e}")
        return 0 if e.conflict else (2 if e.blind else 1)
    say(f"{TAG} RESTARTED deploy/{cfg.deployment} in {cfg.namespace}: {note}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log the decision (and the restart it would make) without PATCHing",
    )
    args = parser.parse_args(argv)
    try:
        cfg = Config.from_env()
    except ValueError as e:
        print(f"{TAG} bad configuration: {e}", file=sys.stderr)
        return 2
    print(
        f"{TAG} start mode={'dry-run' if args.dry_run else 'apply'} "
        f"hold={int(cfg.hold)}s force={int(cfg.force)}s "
        f"cooldown={int(cfg.cooldown)}s exporter={cfg.exporter_url}",
        flush=True,
    )
    return run(
        cfg,
        Cluster(cfg.namespace, cfg.deployment),
        fetch_exporter_text,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
