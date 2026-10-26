#!/usr/bin/env python3
"""Own Ollama posture preparation, adoption evidence, and recovery artifacts.

Recipes are interpreted by the pinned Ollama CLI, not by a second Modelfile
parser. A candidate manifest is read back before its serving tag is changed.
The root-owned receipt is published only after the selected runtime is verified.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import stat
import sys
import time
import uuid

from ollama_recovery import (
    Http,
    PreparationError,
    TransportUnavailable,
    integer_parameter,
    request_recovery,
    verify_loaded,
)
from ollama_signals import (
    MANAGED_SIGNALS,
    deferred,
    deferred_context,
    deferred_depth,
    interruptible,
    retain_signals_blocked,
)

HERE = Path(__file__).resolve().parent
SEMANTIC_FIELDS = (
    "parameters",
    "template",
    "template_source",
    "system",
    "renderer",
    "parser",
    "capabilities",
    "model_info",
    "projector_info",
)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


STAGING_DIRECTORY = ".ollama-write-staging"
PUBLICATION_LOCK_TIMEOUT = 1.0
PREPARATION_LOCK_TIMEOUT = 30.0
INVALIDATION_BUDGET = 30.0


def ensure_directory(path, mode=0o755):
    """Create every missing component with a safe initial write policy.

    Path.mkdir(parents=True) applies the requested mode only to the leaf;
    intermediate directories otherwise start as 0777 masked by the caller.
    Existing directories retain their permissions and each caller's trust checks.
    """
    path = Path(path)
    try:
        path.mkdir(mode=mode)
    except FileNotFoundError:
        ensure_directory(path.parent)
        path.mkdir(mode=mode, exist_ok=True)
    except FileExistsError:
        if not path.is_dir():
            raise


@deferred
def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def bounded_flock(fd, label, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PreparationError(
                    f"timed out acquiring {label} after {timeout:g}s"
                ) from exc
        # Leave the caught-error suite before interruptible waiting. The managed
        # handler preserves active exceptions so resource cleanup can unwind.
        # Publication scopes still defer delivery until ownership is released.
        interruptible(time.sleep, min(0.05, remaining))


@deferred_context
def publication_directory(directory):
    """Serialize writers/reclaimers without leaving an additional lock file."""
    directory = Path(directory)
    ensure_directory(directory)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        bounded_flock(fd, "publication directory lock", PUBLICATION_LOCK_TIMEOUT)
        yield directory
    finally:
        os.close(fd)


def reclaim_staging(directory, checkpoint=lambda: None):
    """Caller holds the directory lock; staging bytes are never authority."""
    staging = directory / STAGING_DIRECTORY
    try:
        info = staging.lstat()
    except FileNotFoundError:
        return
    Preparation.trusted_file(info, "atomic write staging directory", directory=True)
    for temporary in staging.iterdir():
        checkpoint()
        if not re.fullmatch(r"[0-9a-f]{32}\.tmp", temporary.name):
            raise PreparationError("unexpected atomic write staging entry")
        Preparation.trusted_file(temporary.lstat(), "atomic write staging file")
        temporary.unlink()
    staging.rmdir()
    sync_directory(directory)


def recover_atomic_writes(directory):
    with publication_directory(directory) as directory:
        reclaim_staging(directory)


def atomic_write(path, content):
    path = Path(path)
    with publication_directory(path.parent) as directory:
        reclaim_staging(directory)
        staging = directory / STAGING_DIRECTORY
        staging.mkdir(mode=0o700)
        temporary = staging / (uuid.uuid4().hex + ".tmp")
        try:
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            try:
                out = os.fdopen(fd, "w")
            except BaseException:
                os.close(fd)
                raise
            with out:
                out.write(content)
                out.flush()
                os.fchmod(out.fileno(), 0o644)
                os.fsync(out.fileno())
            temporary.replace(path)
            # Rename is the acceptance commit point; sync its parent before
            # acknowledging durability. A killed pre-rename writer leaves only
            # opaque staging, reclaimed on the next locked write/preparation.
            sync_directory(directory)
        finally:
            reclaim_staging(directory)


@deferred
def write_snapshot(snapshot, content):
    """Own the file from open through fdopen entry/exit and final directory sync."""
    fd = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        out = os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise
    with out:
        out.write(content)
        out.flush()
        os.fsync(out.fileno())
    sync_directory(snapshot.parent)


class Posture:
    def __init__(self, path, recipes=None, resident=None):
        self.path = Path(path).resolve()
        self.recipes = Path(recipes or self.path.parent / "modelfiles").resolve()
        self.resident_override = resident
        self.config_bytes = self.path.read_bytes()
        self.data = json.loads(self.config_bytes)
        self.recipe_bytes = {}
        if self.data.get("schema") != 1:
            raise PreparationError("unsupported posture schema")
        self.resident = resident or self.data["resident"]
        if self.resident not in self.data["profiles"]:
            raise PreparationError(f"no posture for resident {self.resident}")
        self.profile = self.data["profiles"][self.resident]
        self.residents = self.profile["residents"]
        self.required = self.residents + self.profile["standby"]
        if (
            not self.residents
            or self.residents[0] != self.resident
            or len(set(self.required)) != len(self.required)
        ):
            raise PreparationError("invalid resident/standby relationship")
        if not isinstance(self.data["parallel"], int) or self.data["parallel"] < 1:
            raise PreparationError("invalid parallel slot count")
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", self.data["version"]):
            raise PreparationError("version must be an exact release")
        material = {"posture": self.data, "resident": self.resident, "recipes": {}}
        for tag, spec in self.data["models"].items():
            if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]*:[a-z0-9][a-z0-9.\-]*", tag):
                raise PreparationError(f"unsupported local tag {tag!r}")
            recipe = self.recipe(tag)
            self.recipe_bytes[tag] = recipe.read_bytes()
            material["recipes"][tag] = self.recipe_bytes[tag].decode("utf-8")
            for pin in spec.get("blobs", []):
                if not re.fullmatch(r"[0-9a-f]{64}", pin):
                    raise PreparationError(f"invalid blob pin for {tag}")
        if any(tag not in self.data["models"] for tag in self.required):
            raise PreparationError("profile names a model without a recipe")
        self.revision = hashlib.sha256(encoded(material).encode()).hexdigest()

    def recipe(self, tag):
        name = self.data["models"][tag]["recipe"]
        path = (self.recipes / name).resolve()
        if path.parent != self.recipes or not name.endswith(".Modelfile"):
            raise PreparationError("recipes must be direct .Modelfile children")
        return path

    def recovery(self):
        return {
            "version": self.data["version"],
            "revision": self.revision,
            "residents": list(self.residents),
        }

    def expected_runtime(self):
        environment = dict(self.data["environment"])
        environment["OLLAMA_NUM_PARALLEL"] = str(self.data["parallel"])
        environment["OLLAMA_MAX_LOADED_MODELS"] = str(len(self.residents))
        return {
            "environment": environment,
            "forbidden_environment": self.data["forbidden_environment"],
            "parallel": self.data["parallel"],
            "max_loaded": len(self.residents),
            "service_properties": self.data["service_properties"],
            "gpu_name": self.data["gpu_name"],
            "backend": "Vulkan",
        }


MANIFEST_LIMIT = 4 * 1024 * 1024


def manifest_blob_ids(content, digest):
    """Read identities from the exact consumer-produced candidate manifest.

    A fresh, unique create produces one runnable manifest, even when FROM
    selects a child of a base manifest list. Never treat the parent list as
    that runnable child or infer a runner choice ourselves.
    """
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PreparationError("invalid candidate manifest digest")
    if len(content) > MANIFEST_LIMIT:
        raise PreparationError("candidate manifest exceeds size limit")
    if hashlib.sha256(content).hexdigest() != digest:
        raise PreparationError("candidate manifest bytes do not match API digest")
    try:
        manifest = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise PreparationError("candidate manifest is not valid JSON") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schemaVersion") != 2
        or manifest.get("mediaType")
        != "application/vnd.docker.distribution.manifest.v2+json"
        or "manifests" in manifest
        or not isinstance(manifest.get("layers"), list)
    ):
        raise PreparationError("candidate must expose a single runnable manifest")
    identities = {"model": [], "projector": []}
    for layer in manifest["layers"]:
        if not isinstance(layer, dict):
            raise PreparationError("invalid candidate manifest layer")
        for kind in identities:
            if layer.get("mediaType") == f"application/vnd.ollama.image.{kind}":
                value = layer.get("digest")
                if not isinstance(value, str) or not re.fullmatch(
                    r"sha256:[0-9a-f]{64}", value
                ):
                    raise PreparationError(f"invalid {kind} layer digest")
                identities[kind].append(value.removeprefix("sha256:"))
    if len(identities["model"]) != 1:
        raise PreparationError("candidate must expose exactly one weights layer")
    return identities["model"] + identities["projector"]


class AdoptionCancelled(BaseException):
    """Cancellation must escape OSError/Exception retry machinery unchanged."""

    def __init__(self, signum):
        super().__init__(f"adoption interrupted by signal {signum}")


class Preparation:
    def __init__(
        self,
        posture,
        api,
        state,
        creator=None,
        *,
        source=None,
        lock_fd=None,
        manifest_reader=None,
    ):
        self.posture = posture
        self.source = source or (
            posture.path,
            posture.recipes,
            posture.resident_override,
        )
        self.api = api
        self.state = Path(state)
        self.creator = creator or self.create
        self.manifest_reader = manifest_reader or self.read_manifest
        self.inherited_lock_fd = lock_fd
        self._lock_fd = None
        self._adoption_depth = 0
        self._aborting = False
        self.cancellation = None

    @deferred
    def read_manifest(self, digest):
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PreparationError("invalid candidate manifest digest")
        store = Path(self.posture.data["environment"]["OLLAMA_MODELS"])
        path = store / "blobs" / ("sha256-" + digest)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > MANIFEST_LIMIT:
                    raise PreparationError(
                        "candidate manifest is not a bounded regular file"
                    )
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    return stream.read(MANIFEST_LIMIT + 1)
            finally:
                os.close(fd)
        except OSError as exc:
            raise PreparationError(
                f"cannot read candidate manifest in declared model store {store}; "
                "run preparation on the daemon host with access to its model store"
            ) from exc

    def checkpoint(self):
        # Abort must finish invalidation and attempt the captured-generation
        # stop even when cancellation is sticky or more signals arrive.
        if self._aborting:
            deadline = getattr(self, "_invalidation_deadline", None)
            if deadline is not None and time.monotonic() >= deadline:
                raise PreparationError(
                    "verification invalidation exceeded its time budget"
                )
        elif self.cancellation is not None:
            self.cancellation.check()

    def operation(self, function, *args, **kwargs):
        """Do no further external work after a swallowed/recorded cancellation.

        Check both sides: a library may handle an unrelated exception while the
        signal arrives, so the handler can only record it until that operation
        returns. Direct helpers without a CLI controller retain their behavior.
        """
        self.checkpoint()
        # Resource owners keep signals deferred between operations. Only this
        # explicit work boundary enables delivery; short critical scopes still
        # forbid it (publication and file ownership must finish first).
        result = (
            function(*args, **kwargs)
            if self._aborting
            else interruptible(function, *args, **kwargs)
        )
        self.checkpoint()
        return result

    @staticmethod
    def trusted_file(info, label, directory=False):
        kind = stat.S_ISDIR if directory else stat.S_ISREG
        if (
            not kind(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
        ):
            raise PreparationError(
                f"{label} must be owned by the preparation user and not group/world-writable"
            )

    @deferred_context(critical=False)
    def locked(self):
        if self._lock_fd is not None:
            raise PreparationError("preparation lock is already held by this operation")
        ensure_directory(self.state)
        self.trusted_file(
            self.state.lstat(), "preparation state directory", directory=True
        )
        path = self.state / "preparation.lock"
        if self.inherited_lock_fd is None:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        else:
            fd = os.dup(self.inherited_lock_fd)
        try:
            info = os.fstat(fd)
            self.trusted_file(info, "preparation lock")
            actual = path.lstat()
            if not stat.S_ISREG(actual.st_mode) or (actual.st_dev, actual.st_ino) != (
                info.st_dev,
                info.st_ino,
            ):
                raise PreparationError(
                    "inherited preparation lock does not match its state path"
                )
            bounded_flock(fd, "preparation transaction lock", PREPARATION_LOCK_TIMEOUT)
            self._lock_fd = fd
            os.fchmod(fd, 0o644)
            os.chmod(self.state, 0o755)
            # A caller may have waited while installation replaced these files.
            # Capture both configuration and recipe bytes only after the lock.
            self.posture = Posture(*self.source)
            recover_atomic_writes(self.state)
            yield
        finally:
            self._lock_fd = None
            # An inherited descriptor shares the installer's open description:
            # never LOCK_UN here, which would release its transaction lock too.
            os.close(fd)

    def version(self, timeout=None):
        options = {} if timeout is None else {"timeout": timeout}
        if (
            self.operation(self.api.call, "/api/version", **options).get("version")
            != self.posture.data["version"]
        ):
            raise PreparationError(
                "running Ollama version differs from the posture; rerun deployment with --bootstrap-recovery to install the pinned release and verify adoption"
            )

    def wait_ready(self, runtime, timeout=30.0):
        """Wait only for startup transport, never for a wrong/protocol-invalid daemon."""
        deadline = time.monotonic() + timeout
        last_error = None
        while True:
            self.operation(runtime._assert_current)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PreparationError(
                    f"startup readiness timed out after {timeout:g}s: {last_error}"
                )
            try:
                # A slow connect/read cannot consume the default 15-second
                # transport timeout after the readiness deadline is nearly up.
                self.version(timeout=min(1.0, remaining))
            except TransportUnavailable as exc:
                last_error = exc
            else:
                self.operation(runtime._assert_current)
                if time.monotonic() >= deadline:
                    raise PreparationError(
                        f"startup readiness timed out after {timeout:g}s"
                    )
                return
            self.operation(runtime._assert_current)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PreparationError(
                    f"startup readiness timed out after {timeout:g}s: {last_error}"
                )
            self.operation(time.sleep, min(1.0, remaining))

    def create(self, tag, recipe):
        if self._lock_fd is None:
            raise PreparationError("candidate creation requires the preparation lock")
        env = dict(os.environ, OLLAMA_HOST=self.api.url)
        try:
            result = self.operation(
                subprocess.run,
                ["ollama", "create", tag, "-f", str(recipe)],
                env=env,
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
                pass_fds=(self._lock_fd,),
            )
        except subprocess.TimeoutExpired as exc:
            retry = (
                "Adoption abort must invalidate verification and attempt to stop its captured daemon, "
                "including a bootstrap daemon. After confirmed stop, retry the trusted host installer "
                "with --bootstrap-recovery to restart and resume preparation with the same bounded limit"
                if self._adoption_depth
                else "This preparation step does not stop the daemon. If the installer invoked it, "
                "rerun the same deployment command and mode; otherwise retry the same prepare/pair "
                "command. The same bounded limit applies"
            )
            raise PreparationError(
                "candidate creation exceeded the 900-second limit, including any base-model download. "
                "Keep the model store: cached blobs and resumable partial downloads may be reused on retry. "
                + retry
            ) from exc
        except OSError as exc:
            raise PreparationError(f"candidate creation failed: {exc}") from exc
        if result.returncode:
            raise PreparationError(
                f"candidate creation failed for {recipe.name}: {result.stderr[-3000:]}"
            )

    def intent_directory(self):
        self.checkpoint()
        path = self.state / "candidate-intents"
        ensure_directory(path, mode=0o700)
        self.trusted_file(path.lstat(), "candidate intent directory", directory=True)
        # Persist the directory links too: fsyncing only the eventual intent
        # file would not protect a newly created state/intent directory on crash.
        self.sync_directory(path.parent)
        self.sync_directory(path.parent.parent)
        return path

    sync_directory = staticmethod(sync_directory)

    def cleanup_intent(self, path):
        self.trusted_file(path.lstat(), "candidate intent")
        intent = json.loads(path.read_text())
        token = path.stem
        tag = intent.get("tag", "")
        snapshot = Path(intent.get("snapshot", ""))
        if (
            intent.get("schema") != 1
            or not re.fullmatch(r"[0-9a-f]{32}", token)
            or not re.fullmatch(r"homelab-preparing/[a-z0-9][a-z0-9.\-]*:" + token, tag)
            or not snapshot.is_absolute()
            or snapshot.name != f".ollama-preparing-{token}.Modelfile"
        ):
            raise PreparationError(
                "invalid candidate intent; refusing unrelated cleanup"
            )
        if tag in self.operation(self.api.tags):
            self.operation(
                self.api.call, "/api/delete", {"model": tag}, method="DELETE"
            )
            if tag in self.operation(self.api.tags):
                raise PreparationError(f"candidate cleanup did not remove {tag}")
        if snapshot.exists() or snapshot.is_symlink():
            self.trusted_file(snapshot.lstat(), "candidate recipe snapshot")
            self.operation(snapshot.unlink)
            self.sync_directory(snapshot.parent)
        self.operation(path.unlink)
        self.sync_directory(path.parent)

    def replay_candidates(self):
        if self._lock_fd is None:
            with self.locked():
                return self.replay_candidates()
        # Only durable operation records authorize deletion. An unrecorded
        # homelab-preparing tag is someone else's, regardless of its spelling.
        directory = self.intent_directory()
        self.operation(recover_atomic_writes, directory)
        for path in sorted(directory.iterdir()):
            if not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
                raise PreparationError("unexpected candidate intent entry")
            self.cleanup_intent(path)

    @contextmanager
    def candidate(self, tag, posture=None):
        self.checkpoint()
        posture = posture or self.posture
        self.replay_candidates()
        token = uuid.uuid4().hex
        name = f"homelab-preparing/{tag.replace(':', '-')}:{token}"
        if name in self.operation(self.api.tags):
            raise PreparationError("candidate name collision")
        # Keep the captured file beside its source so relative FROM/ADAPTER
        # paths retain Ollama's own path semantics. The bytes are never reread.
        snapshot = posture.recipes / f".ollama-preparing-{token}.Modelfile"
        if snapshot.exists() or snapshot.is_symlink():
            raise PreparationError("candidate snapshot collision")
        intent = self.intent_directory() / f"{token}.json"
        self.operation(
            atomic_write,
            intent,
            json.dumps({"schema": 1, "tag": name, "snapshot": str(snapshot)}),
        )
        self.sync_directory(intent.parent)
        cleanup_now = False
        try:
            self.checkpoint()
            write_snapshot(snapshot, posture.recipe_bytes[tag])
            self.operation(self.creator, name, snapshot)
            cleanup_now = True
            inventory = self.operation(self.api.tags)
            if name not in inventory:
                raise PreparationError(
                    "create acknowledged without a readable candidate"
                )
            shown = self.operation(self.api.show, name)
            digest = inventory[name]
            content = self.operation(self.manifest_reader, digest)
            blobs = manifest_blob_ids(content, digest)
            if self.operation(self.api.tags).get(name) != digest:
                raise PreparationError(
                    "candidate identity changed during manifest readback"
                )
            pins = posture.data["models"][tag].get("blobs")
            if pins and blobs != pins:
                raise PreparationError(
                    f"{tag}: weights/projector do not match approved pins"
                )
            value = {
                "tag": tag,
                "digest": inventory[name],
                "context": integer_parameter(shown, "num_ctx"),
                "batch": integer_parameter(
                    shown, "num_batch", posture.data["default_batch"]
                ),
                "weights_sha256": blobs[0],
                "show": shown,
            }
            yield name, value
        except (AdoptionCancelled, InterruptedError, KeyboardInterrupt, SystemExit):
            # The create request may have outlived its interrupted caller.
            # Leave its durable intent for the next lock-owning preparation.
            cleanup_now = False
            raise
        finally:
            if cleanup_now:
                self.cleanup_intent(intent)

    def invalidate_receipt(self):
        self.checkpoint()
        with publication_directory(self.state) as directory:
            reclaim_staging(directory, self.checkpoint)
            self.checkpoint()
            directory.joinpath("verified.json").unlink(missing_ok=True)
            self.sync_directory(directory)

    def invalidate(self):
        self.invalidate_receipt()
        # Retire the entire legacy certificate namespace, including models no
        # longer in this catalog. Public aliases are never acceptance evidence.
        for tag in self.operation(self.api.tags):
            self.checkpoint()
            if re.fullmatch(r"homelab-prepared/[a-z0-9][a-z0-9.\-]*:[0-9a-f]{64}", tag):
                self.operation(
                    self.api.call, "/api/delete", {"model": tag}, method="DELETE"
                )
                if tag in self.operation(self.api.tags):
                    raise PreparationError(
                        "could not invalidate an old verification witness"
                    )

    def prepare(self, tags=None):
        self.version()
        self.replay_candidates()
        result = {}
        for tag in tags or self.posture.required:
            with self.candidate(tag) as (candidate, wanted):
                if self.operation(self.api.tags).get(tag) != wanted["digest"]:
                    self.invalidate()
                    self.operation(
                        self.api.call,
                        "/api/copy",
                        {"source": candidate, "destination": tag},
                    )
                if self.operation(self.api.tags).get(tag) != wanted["digest"]:
                    raise PreparationError(
                        f"{tag}: copy did not adopt the canonical candidate"
                    )
                shown = self.operation(self.api.show, tag)
                if any(
                    shown.get(field) != wanted["show"].get(field)
                    for field in SEMANTIC_FIELDS
                ):
                    raise PreparationError(
                        f"{tag}: serving-name metadata differs from the candidate"
                    )
                result[tag] = {
                    key: value for key, value in wanted.items() if key != "show"
                }
        return result

    def certify(self, models, evidence):
        inventory = self.operation(self.api.tags)
        for tag, model in models.items():
            if inventory.get(tag) != model["digest"]:
                raise PreparationError("model changed before verification publication")
        # This root-owned atomic receipt is the only publication/commit point.
        # HTTP clients can copy aliases, but cannot create this acceptance record.
        receipt = {
            "schema": 2,
            "revision": self.posture.revision,
            "version": self.posture.data["version"],
            "resident": self.posture.resident,
            "residents": list(self.posture.residents),
            "models": models,
            "runtime": evidence,
        }
        self.operation(
            atomic_write,
            self.state / "verified.json",
            json.dumps(receipt, indent=2) + "\n",
        )
        return receipt

    @contextmanager
    def adopting(self, runtime):
        """Keep one cleanup boundary around the entire managed operation.

        CLI callers put signal activation and the flushed response inside this
        guard, and the runtime/transaction lock outside it. Nested warm/verify
        guards preserve direct-call cleanup without repeating a failed stop.
        """
        if self._adoption_depth == 0:
            self._aborting = False
        self._adoption_depth += 1
        try:
            yield
            self.checkpoint()
        except BaseException as exc:
            if not self._aborting:
                self.abort(runtime, exc)
            raise
        finally:
            self._adoption_depth -= 1

    def warm(self, runtime):
        with self.adopting(runtime):
            # Type=simple can release the warm unit before HTTP starts accepting.
            # Invalidate the local claim immediately, then clear remote witnesses
            # as soon as this captured generation's API is reachable. If it never
            # becomes reachable, abort reports that cleanup gap and confirms stop.
            self.invalidate_receipt()
            self.wait_ready(runtime)
            self.invalidate()
            models = self.operation(self.prepare)
            expected = {tag: models[tag] for tag in self.posture.residents}
            for tag in self.posture.residents:
                self.operation(self.api.load, tag)
            verify_loaded(self.operation(self.api.loaded), expected)
            evidence = self.operation(
                runtime.verify, self.posture.expected_runtime(), list(expected.values())
            )
            # Check HTTP observations again after the process/runner proof.
            verify_loaded(self.operation(self.api.loaded), expected)
            self.operation(runtime._assert_current)
            return self.certify(models, evidence)

    def abort(self, runtime, cause):
        # The managed handlers stop raising as soon as cleanup begins. This
        # covers teardown too; a second cancellation cannot bypass target stop.
        self._aborting = True
        # No new cleanup request starts after this budget. Existing HTTP calls
        # retain their finite transport timeout; target stop is outside this gate.
        self._invalidation_deadline = time.monotonic() + INVALIDATION_BUDGET
        previous = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT}
        )
        try:
            self._abort(runtime, cause)
        finally:
            # Deliver any pending cancellation only after invalidation and the
            # captured-generation stop attempt. A managed handler must not
            # replace the cleanup outcome with another cancellation exception.
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous)
            except (AdoptionCancelled, InterruptedError, KeyboardInterrupt):
                pass

    @deferred
    def _abort(self, runtime, cause):
        # Shared critical ownership also keeps Runtime's interruptible process
        # observations masked until the stop attempt has finished.
        cleanup_failure = ""
        try:
            self.invalidate()
        except BaseException as cleanup:
            cleanup_failure = f" INVALIDATION FAILED: {cleanup}."
            try:
                print(f"verification invalidation failed: {cleanup}", file=sys.stderr)
            except BaseException:
                pass  # A broken diagnostic stream must not skip target stop.
        try:
            stopped = runtime.stop_failed()
        except BaseException as exc:
            raise PreparationError(
                f"adoption failed: {cause}; STOP FAILED: {exc}.{cleanup_failure} Inspect the current daemon before recovery"
            ) from exc
        outcome = (
            "captured daemon stopped"
            if stopped and stopped.get("stopped")
            else "captured daemon already exited"
        )
        raise PreparationError(
            f"adoption failed; {outcome}.{cleanup_failure} Correct the mismatch, then run the trusted host installer with --bootstrap-recovery. Cause: {cause}"
        ) from cause

    def verify(self, runtime, stop_on_failure=False):
        if stop_on_failure:
            with self.adopting(runtime):
                return self.verify(runtime)
        try:
            self.version()
            receipt = json.loads(
                self.operation((self.state / "verified.json").read_text)
            )
            if (
                receipt.get("schema") != 2
                or receipt.get("revision") != self.posture.revision
                or receipt.get("version") != self.posture.data["version"]
                or receipt.get("resident") != self.posture.resident
                or receipt.get("residents") != self.posture.residents
            ):
                raise PreparationError("missing or stale posture verification receipt")
            models = receipt["models"]
            if set(models) != set(self.posture.required):
                raise PreparationError("receipt does not cover all required models")
            inventory = self.operation(self.api.tags)
            for tag, model in models.items():
                if model["tag"] != tag or inventory.get(tag) != model["digest"]:
                    raise PreparationError(
                        "receipt model identity is no longer current"
                    )
            expected = {tag: models[tag] for tag in self.posture.residents}
            verify_loaded(self.operation(self.api.loaded), expected)
            evidence = self.operation(
                runtime.verify, self.posture.expected_runtime(), list(expected.values())
            )
            for field in ("boot_id", "invocation_id", "pid", "start_ticks"):
                if evidence[field] != receipt["runtime"][field]:
                    raise PreparationError(
                        "receipt belongs to a different daemon generation"
                    )
            verify_loaded(self.operation(self.api.loaded), expected)
            after = self.operation(self.api.tags)
            if any(after.get(tag) != models[tag]["digest"] for tag in models):
                raise PreparationError(
                    "model identity changed during receipt verification"
                )
            return {
                "status": "verified",
                "revision": self.posture.revision,
                "runtime": evidence,
            }
        except BaseException as exc:
            raise PreparationError(f"verification failed: {exc}") from exc

    def files_check(self, installed):
        if (
            self.posture.data != installed.data
            or self.posture.resident != installed.resident
        ):
            raise PreparationError(
                "--files-only refuses a posture/settings/pin/selection change; run full adoption"
            )
        if self.posture.revision != installed.revision:
            raise PreparationError(
                "--files-only refuses a recipe revision change, including comments; "
                "run full adoption to publish a matching recovery receipt"
            )
        # This mode accepts only byte-identical recipe inputs. Compiling them
        # again cannot admit another change, but would pull unused rollback bases.
        # Full prepare/warm retain canonical consumer identity verification.
        self.version()


def dropin(posture):
    env = posture.expected_runtime()["environment"]
    props = posture.data["service_properties"]
    quota = props["CPUQuotaPerSecUSec"]
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?s", quota):
        raise PreparationError("CPUQuotaPerSecUSec must be expressed in seconds")
    lines = [
        "# Generated by ollama_prepare.py render; edit model-posture.json.",
        "[Unit]",
        "After=network-online.target",
        "Wants=network-online.target ollama-warm.service",
        "",
        "[Service]",
        f"MemoryMax={props['MemoryMax']}",
        f"CPUQuota={float(quota[:-1]) * 100:g}%",
        "RestartPreventExitStatus=",
        "RestartPreventExitStatus=SIGHUP",
        "RestartForceExitStatus=",
        f"KillMode={props['KillMode']}",
        f"SendSIGKILL={props['SendSIGKILL']}",
        f"TimeoutStopSec={props['TimeoutStopUSec']}",
    ]
    for key, value in env.items():
        if (
            not re.fullmatch(r"[A-Z_]+", key)
            or not isinstance(value, str)
            or any(c in value for c in '\n\r"\\%')
        ):
            raise PreparationError("unsupported systemd environment value")
        lines.append(f'Environment="{key}={value}"')
    return "\n".join(lines) + "\n"


def recovery_manifest(posture):
    program = (HERE / "ollama_recovery.py").read_text()
    container = {
        "name": "warm",
        "image": "python:3.12-alpine",
        "env": [
            {
                "name": "OLLAMA_RECOVERY_URL",
                "value": "http://ollama.llama.svc:9111/preparation/recover",
            },
            {"name": "OLLAMA_RECOVERY", "value": encoded(posture.recovery())},
        ],
        "command": ["python3", "-c", program],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "resources": {
            "requests": {"cpu": "10m", "memory": "32Mi"},
            "limits": {"cpu": "100m", "memory": "96Mi"},
        },
    }
    job = {
        "backoffLimit": 1,
        "activeDeadlineSeconds": 900,
        "ttlSecondsAfterFinished": 172800,
        "template": {
            "metadata": {"labels": {"app": "ollama", "job": "ollama-warm"}},
            "spec": {
                "restartPolicy": "Never",
                "securityContext": {"runAsNonRoot": True, "runAsUser": 65534},
                "containers": [container],
            },
        },
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {
            "name": "ollama-warm",
            "namespace": "llama",
            "labels": {
                "app": "ollama",
                "homelab.nathanwhyte.dev/backend": "host-systemd",
            },
        },
        "spec": {
            "schedule": "*/15 * * * *",
            "suspend": False,
            "concurrencyPolicy": "Forbid",
            "successfulJobsHistoryLimit": 1,
            "failedJobsHistoryLimit": 3,
            "jobTemplate": {"spec": job},
        },
    }


def render(posture, root, check):
    root = Path(root)
    warm = json.dumps(recovery_manifest(posture), indent=2) + "\n"
    pull = (HERE / "ollama-pull.template.yaml").read_text()
    outputs = {
        root / "llama/host/ollama.service.d/homelab.conf": dropin(posture),
        root
        / "llama/ollama-jobs.yaml": "# Generated by llama/host/ollama_prepare.py render.\n"
        + warm
        + "---\n"
        + pull,
    }
    changed = []
    for path, content in outputs.items():
        if not check:
            recover_atomic_writes(path.parent)
        if not path.exists() or path.read_text() != content:
            changed.append(str(path))
            if not check:
                atomic_write(path, content)
    if check and changed:
        raise PreparationError(
            "generated adoption artifacts are stale: " + ", ".join(changed)
        )
    return {"revision": posture.revision, "changed": changed}


class CommandCancellation:
    """Own adopting CLI signals until process exit, with one completion point.

    This is deliberately not a restoring context manager. Setup blocks both
    signals before changing either disposition. After durable publication and
    a flushed response, the empty pending-cancellation observation in complete()
    linearizes success. Signals then remain blocked through runtime/lock release
    and CLI exit; a later request cannot cancel the completed operation.

    Before that boundary, handled or queued cancellation must reach the outer
    adoption guard while the runtime and preparation lock are still held. Direct
    preparation helpers do not install this process-lifetime CLI policy.
    """

    SIGNALS = MANAGED_SIGNALS

    def __init__(self, cleaning_up=lambda: False):
        self.cleaning_up = cleaning_up
        self.interrupted = None
        self.completed = False

    def cancelled(self, signum, _frame):
        if not self.completed:
            if self.interrupted is None:
                self.interrupted = signum
            # An asynchronous raise while another exception is unwinding can
            # replace that exception before its cleanup guard runs. Record the
            # cancellation there and let the existing unwind reach abort. If an
            # inner handler swallows that exception, the next operation boundary
            # rejects this sticky cancellation. Every ordinary-work signal can
            # raise again; an earlier signal never disables later interruption.
            if not self.cleaning_up() and sys.exc_info()[0] is None:
                raise AdoptionCancelled(signum)

    def check(self):
        pending = signal.sigpending() & self.SIGNALS
        if not self.completed and (self.interrupted is not None or pending):
            raise AdoptionCancelled(
                self.interrupted if self.interrupted is not None else min(pending)
            )

    def quiesce(self, terminal=False):
        signal.pthread_sigmask(signal.SIG_BLOCK, self.SIGNALS)
        if terminal:
            retain_signals_blocked()

    def activate(self):
        self.quiesce()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, self.cancelled)
        # A signal queued during setup is delivered only after both handlers
        # exist, inside the caller's adoption cleanup boundary.
        if not deferred_depth():
            signal.pthread_sigmask(signal.SIG_UNBLOCK, self.SIGNALS)
        self.check()

    def complete(self):
        self.quiesce()
        pending = signal.sigpending() & self.SIGNALS
        if self.interrupted is not None or pending:
            signum = self.interrupted if self.interrupted is not None else min(pending)
            raise AdoptionCancelled(signum)
        # The empty pending observation above is the linearization point. A
        # signal queued after it stays blocked and cannot reopen cancellation.
        self.completed = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(HERE / "model-posture.json"))
    parser.add_argument(
        "--recipes",
        default=os.environ.get("MODELFILE_DIR")
        or str(
            HERE / "modelfiles"
            if (HERE / "modelfiles").is_dir()
            else HERE.parent / "ollama"
        ),
    )
    parser.add_argument(
        "--resident",
        help=(
            "temporary explicit profile selection; RESIDENT_TAG is ignored. "
            "For a persistent production change, edit model-posture.json and rerender"
        ),
    )
    parser.add_argument(
        "--url", default=os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
    )
    parser.add_argument(
        "--state",
        default=os.environ.get(
            "OLLAMA_PREPARATION_STATE", "/var/lib/ollama-preparation"
        ),
    )
    parser.add_argument(
        "--lock-fd", type=int, help="inherited preparation lock descriptor"
    )
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "warm", "pair", "recover", "version"):
        subs.add_parser(name)
    verification = subs.add_parser("verify")
    verification.add_argument("--stop-on-failure", action="store_true")
    files = subs.add_parser("files-check")
    files.add_argument("--installed", required=True)
    render_args = subs.add_parser("render")
    render_args.add_argument("--root", default=str(HERE.parents[1]))
    render_args.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.command in ("version", "render", "recover"):
        posture = Posture(args.config, args.recipes, args.resident)
        if args.command == "version":
            print(posture.data["version"])
            return
        if args.command == "recover":
            print(
                json.dumps(
                    request_recovery(
                        posture.recovery(), "http://127.0.0.1:9111/preparation/recover"
                    )
                )
            )
            return
        result = render(posture, args.root, args.check)
    else:
        prep = Preparation(
            None,
            Http(args.url),
            args.state,
            source=(args.config, args.recipes, args.resident),
            lock_fd=args.lock_fd,
        )
        with prep.locked():
            posture = prep.posture
            if args.command in ("warm", "verify"):
                from ollama_runtime import Runtime

                if args.url not in ("http://127.0.0.1:11434", "http://localhost:11434"):
                    raise PreparationError(
                        "host verification requires the local daemon endpoint"
                    )
                with Runtime() as runtime:
                    if args.command == "warm" or args.stop_on_failure:
                        # Handler activation follows lock/generation capture.
                        # Signals deferred across setup count as adoption
                        # cancellation when activate() checks the pending set.
                        cancellation = CommandCancellation(lambda: prep._aborting)
                        prep.cancellation = cancellation
                        try:
                            with prep.adopting(runtime):
                                cancellation.activate()
                                result = (
                                    prep.warm(runtime)
                                    if args.command == "warm"
                                    else prep.verify(runtime, True)
                                )
                                cancellation.check()
                                print(json.dumps(result, sort_keys=True))
                                cancellation.check()
                                sys.stdout.flush()
                                cancellation.complete()
                        finally:
                            # Never restore SIG_DFL or unmask in this command.
                            # Failure cleanup has run, or success linearized;
                            # retain signal ownership through all resource exit.
                            cancellation.quiesce(terminal=True)
                    else:
                        # Observational verify never acquires cancellation
                        # authority to invalidate a receipt or stop its daemon.
                        result = prep.verify(runtime)
                        print(json.dumps(result, sort_keys=True))
                        sys.stdout.flush()
                return
            elif args.command == "prepare":
                result = prep.prepare()
            elif args.command == "pair":
                result = prep.prepare(["qwen2.5-coder:fim", "gemma4:vlm"])
            else:
                installed = Path(args.installed)
                prep.files_check(
                    Posture(
                        installed / "model-posture.json",
                        installed / "modelfiles",
                        args.resident,
                    )
                )
                result = {"status": "unchanged effective posture"}
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
