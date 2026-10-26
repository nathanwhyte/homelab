#!/usr/bin/env python3
"""Own Ollama posture preparation, adoption evidence, and recovery artifacts.

Recipes are interpreted by the pinned Ollama CLI, not by a second Modelfile
parser. A candidate manifest is read back before its serving tag is changed.
Witness tags are published only after the selected runtime has been verified.
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
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

from ollama_recovery import (
    Http,
    PreparationError,
    TransportUnavailable,
    integer_parameter,
    recover,
    verify_loaded,
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


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        try:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
            os.chmod(temporary, 0o644)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


class Posture:
    def __init__(self, path, recipes=None, resident=None):
        self.path = Path(path).resolve()
        self.recipes = Path(recipes or self.path.parent / "modelfiles").resolve()
        self.data = json.loads(self.path.read_text())
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
            material["recipes"][tag] = recipe.read_text()
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

    def witness(self, tag):
        return f"homelab-prepared/{tag.replace(':', '-')}:{self.revision}"

    def recovery(self):
        return {
            "version": self.data["version"],
            "revision": self.revision,
            "residents": {tag: self.witness(tag) for tag in self.residents},
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


def blob_ids(show):
    # `show.modelfile` is Ollama's generated display. Only its leading FROM
    # block is inspected; a TEMPLATE body containing "FROM" is not a layer.
    text = show.get("modelfile")
    if not isinstance(text, str):
        raise PreparationError("show lacks canonical Modelfile display")
    blobs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = shlex.split(line)
        if not fields or fields[0] != "FROM":
            break
        if len(fields) != 2:
            raise PreparationError("malformed canonical FROM display")
        match = re.fullmatch(r".*/sha256-([0-9a-f]{64})", fields[1])
        if not match:
            raise PreparationError(
                "model does not expose a local content-addressed layer"
            )
        blobs.append(match[1])
    if not blobs:
        raise PreparationError("no model weights observed")
    return blobs


class Preparation:
    def __init__(self, posture, api, state, creator=None):
        self.posture = posture
        self.api = api
        self.state = Path(state)
        self.creator = creator or self.create

    @contextmanager
    def locked(self):
        self.state.mkdir(parents=True, exist_ok=True)
        with (self.state / "preparation.lock").open("a+") as lock:
            # The installer is root; the warm unit's directory belongs to
            # ollama. Keep a root-created lock accessible to its directory owner.
            if os.geteuid() == 0:
                owner = self.state.stat()
                os.fchown(lock.fileno(), owner.st_uid, owner.st_gid)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def version(self, timeout=None):
        options = {} if timeout is None else {"timeout": timeout}
        if (
            self.api.call("/api/version", **options).get("version")
            != self.posture.data["version"]
        ):
            raise PreparationError(
                "running Ollama version differs from the posture; use explicit bootstrap/recovery after installing the pinned release"
            )

    def wait_ready(self, runtime, timeout=30.0):
        """Wait only for startup transport, never for a wrong/protocol-invalid daemon."""
        deadline = time.monotonic() + timeout
        last_error = None
        while True:
            runtime._assert_current()
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
                runtime._assert_current()
                if time.monotonic() >= deadline:
                    raise PreparationError(
                        f"startup readiness timed out after {timeout:g}s"
                    )
                return
            runtime._assert_current()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PreparationError(
                    f"startup readiness timed out after {timeout:g}s: {last_error}"
                )
            time.sleep(min(1.0, remaining))

    def create(self, tag, recipe):
        env = dict(os.environ, OLLAMA_HOST=self.api.url)
        try:
            result = subprocess.run(
                ["ollama", "create", tag, "-f", str(recipe)],
                env=env,
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PreparationError(f"candidate creation failed: {exc}") from exc
        if result.returncode:
            raise PreparationError(
                f"candidate creation failed for {recipe.name}: {result.stderr[-3000:]}"
            )

    @contextmanager
    def candidate(self, tag, posture=None):
        posture = posture or self.posture
        name = f"homelab-preparing/{tag.replace(':', '-')}:{uuid.uuid4().hex}"
        if name in self.api.tags():
            raise PreparationError("candidate name collision")
        try:
            self.creator(name, posture.recipe(tag))
            inventory = self.api.tags()
            if name not in inventory:
                raise PreparationError(
                    "create acknowledged without a readable candidate"
                )
            shown = self.api.show(name)
            blobs = blob_ids(shown)
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
        finally:
            # This UUID was absent before this operation's create. Never prune
            # the store or remove a serving/base tag, including on partial create.
            if name in self.api.tags():
                self.api.call("/api/delete", {"model": name}, method="DELETE")
                if name in self.api.tags():
                    raise PreparationError(f"candidate cleanup did not remove {name}")

    def invalidate(self):
        self.state.joinpath("verified.json").unlink(missing_ok=True)
        # The module owns only this namespace and exact catalog model stems.
        stems = {
            f"homelab-prepared/{tag.replace(':', '-')}"
            for tag in self.posture.data["models"]
        }
        for tag in self.api.tags():
            stem, _, revision = tag.rpartition(":")
            if stem in stems and re.fullmatch(r"[0-9a-f]{64}", revision):
                self.api.call("/api/delete", {"model": tag}, method="DELETE")
                if tag in self.api.tags():
                    raise PreparationError(
                        "could not invalidate an old verification witness"
                    )

    def prepare(self, tags=None):
        self.version()
        result = {}
        for tag in tags or self.posture.required:
            with self.candidate(tag) as (candidate, wanted):
                if self.api.tags().get(tag) != wanted["digest"]:
                    self.invalidate()
                    self.api.call(
                        "/api/copy", {"source": candidate, "destination": tag}
                    )
                if self.api.tags().get(tag) != wanted["digest"]:
                    raise PreparationError(
                        f"{tag}: copy did not adopt the canonical candidate"
                    )
                shown = self.api.show(tag)
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
        for tag, model in models.items():
            witness = self.posture.witness(tag)
            if self.api.tags().get(tag) != model["digest"]:
                raise PreparationError("model changed before verification publication")
            if self.api.tags().get(witness) != model["digest"]:
                self.api.call("/api/copy", {"source": tag, "destination": witness})
            inventory = self.api.tags()
            if (
                inventory.get(witness) != model["digest"]
                or inventory.get(tag) != model["digest"]
            ):
                raise PreparationError("verification witness failed readback")
        receipt = {
            "schema": 1,
            "revision": self.posture.revision,
            "resident": self.posture.resident,
            "models": models,
            "runtime": evidence,
        }
        atomic_write(self.state / "verified.json", json.dumps(receipt, indent=2) + "\n")
        return receipt

    def warm(self, runtime):
        try:
            # Type=simple can release the warm unit before HTTP starts accepting.
            # Invalidate the local claim immediately, then clear remote witnesses
            # as soon as this captured generation's API is reachable. If it never
            # becomes reachable, abort reports that cleanup gap and confirms stop.
            self.state.joinpath("verified.json").unlink(missing_ok=True)
            self.wait_ready(runtime)
            self.invalidate()
            models = self.prepare()
            expected = {tag: models[tag] for tag in self.posture.residents}
            for tag in self.posture.residents:
                self.api.load(tag)
            verify_loaded(self.api.loaded(), expected)
            evidence = runtime.verify(
                self.posture.expected_runtime(), list(expected.values())
            )
            # Check HTTP observations again after the process/runner proof.
            verify_loaded(self.api.loaded(), expected)
            return self.certify(models, evidence)
        except Exception as exc:
            self.abort(runtime, exc)

    def abort(self, runtime, cause):
        self.state.joinpath("verified.json").unlink(missing_ok=True)
        try:
            self.invalidate()
        except Exception as cleanup:
            print(f"verification invalidation failed: {cleanup}", file=sys.stderr)
        try:
            stopped = runtime.stop_failed()
        except Exception as exc:
            raise PreparationError(
                f"adoption failed: {cause}; STOP FAILED: {exc}. Inspect the current daemon before recovery"
            ) from exc
        outcome = (
            "captured daemon stopped"
            if stopped and stopped.get("stopped")
            else "captured daemon already exited"
        )
        raise PreparationError(
            f"adoption failed; {outcome}. Correct the mismatch, then run the trusted host installer with --bootstrap-recovery. Cause: {cause}"
        ) from cause

    def verify(self, runtime, stop_on_failure=False):
        try:
            self.version()
            receipt = json.loads((self.state / "verified.json").read_text())
            if (
                receipt.get("schema") != 1
                or receipt.get("revision") != self.posture.revision
                or receipt.get("resident") != self.posture.resident
            ):
                raise PreparationError("missing or stale posture verification receipt")
            models = receipt["models"]
            if set(models) != set(self.posture.required):
                raise PreparationError("receipt does not cover all required models")
            inventory = self.api.tags()
            for tag, model in models.items():
                if (
                    model["tag"] != tag
                    or inventory.get(tag) != model["digest"]
                    or inventory.get(self.posture.witness(tag)) != model["digest"]
                ):
                    raise PreparationError(
                        "receipt model/witness identity is no longer current"
                    )
            expected = {tag: models[tag] for tag in self.posture.residents}
            verify_loaded(self.api.loaded(), expected)
            evidence = runtime.verify(
                self.posture.expected_runtime(), list(expected.values())
            )
            for field in ("boot_id", "invocation_id", "pid", "start_ticks"):
                if evidence[field] != receipt["runtime"][field]:
                    raise PreparationError(
                        "receipt belongs to a different daemon generation"
                    )
            verify_loaded(self.api.loaded(), expected)
            after = self.api.tags()
            if any(
                after.get(tag) != models[tag]["digest"]
                or after.get(self.posture.witness(tag)) != models[tag]["digest"]
                for tag in models
            ):
                raise PreparationError(
                    "model identity changed during receipt verification"
                )
            return {
                "status": "verified",
                "revision": self.posture.revision,
                "runtime": evidence,
            }
        except Exception as exc:
            if stop_on_failure:
                self.abort(runtime, exc)
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
                "run full adoption to publish matching recovery witnesses"
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
            {"name": "OLLAMA_URL", "value": "http://ollama.llama.svc:11434"},
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
        if not path.exists() or path.read_text() != content:
            changed.append(str(path))
            if not check:
                atomic_write(path, content)
    if check and changed:
        raise PreparationError(
            "generated adoption artifacts are stale: " + ", ".join(changed)
        )
    return {"revision": posture.revision, "changed": changed}


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
    parser.add_argument("--resident", default=os.environ.get("RESIDENT_TAG"))
    parser.add_argument(
        "--url", default=os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
    )
    parser.add_argument(
        "--state",
        default=os.environ.get(
            "OLLAMA_PREPARATION_STATE", "/var/lib/ollama-preparation"
        ),
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
    posture = Posture(args.config, args.recipes, args.resident)
    if args.command == "version":
        print(posture.data["version"])
        return
    if args.command == "render":
        result = render(posture, args.root, args.check)
    else:
        prep = Preparation(posture, Http(args.url), args.state)
        with prep.locked():
            if args.command in ("warm", "verify"):
                from ollama_runtime import Runtime

                if args.url not in ("http://127.0.0.1:11434", "http://localhost:11434"):
                    raise PreparationError(
                        "host verification requires the local daemon endpoint"
                    )
                with Runtime() as runtime:
                    result = (
                        prep.warm(runtime)
                        if args.command == "warm"
                        else prep.verify(runtime, args.stop_on_failure)
                    )
            elif args.command == "prepare":
                result = prep.prepare()
            elif args.command == "pair":
                result = prep.prepare(["qwen2.5-coder:fim", "gemma4:vlm"])
            elif args.command == "recover":
                result = recover(posture.recovery(), prep.api)
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
