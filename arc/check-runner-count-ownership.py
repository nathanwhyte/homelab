#!/usr/bin/env python3
"""Run the Helm ownership regression in a fresh local Docker API server."""

from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

IMAGE = "rancher/k3s:v1.36.2-k3s1@sha256:6a47cea22c4b834d4ba72c89d291696b79ebe406251f90b446e4dff03513dd87"
LABEL = "compendium.arc-ssa-test"
ROOT = Path(__file__).resolve().parent


def run(*args, timeout=30, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed: {result.stderr.strip()}")
    return result


def local_docker_endpoint():
    # DOCKER_CONTEXT overrides DOCKER_HOST in the Docker CLI.
    context = os.environ.get("DOCKER_CONTEXT")
    if not context and os.environ.get("DOCKER_HOST"):
        endpoint = os.environ["DOCKER_HOST"]
    else:
        context = context or run("docker", "context", "show").stdout.strip()
        endpoint = run(
            "docker",
            "context",
            "inspect",
            context,
            "--format",
            "{{.Endpoints.docker.Host}}",
        ).stdout.strip()
    if not endpoint.startswith("unix://"):
        raise RuntimeError(
            "a local Unix-socket Docker engine is required; refusing remote Docker"
        )
    return endpoint


def cleanup(name, token):
    result = run(
        "docker",
        "inspect",
        name,
        "--format",
        '{{index .Config.Labels "' + LABEL + '"}}',
        check=False,
    )
    if result.returncode:
        # A successful --rm exit already removed it. Daemon errors are not
        # proof of cleanup; retain the exact identity in the diagnostic.
        if "No such object" in result.stderr or "No such container" in result.stderr:
            return
        raise RuntimeError(f"cannot verify cleanup for {name}: {result.stderr.strip()}")
    if result.stdout.strip() != token:
        raise RuntimeError(
            f"refusing cleanup of container with a different owner: {name}"
        )
    run("docker", "rm", "--force", name)


@contextmanager
def disposable_api():
    token = uuid.uuid4().hex
    name = "arc-ssa-test-" + token
    with tempfile.TemporaryDirectory(prefix="arc-ssa-api-") as directory:
        output = Path(directory)
        kubeconfig = output / "kubeconfig"
        try:
            # No Kubernetes context is consulted. The credential belongs only
            # to this new API server, which has no agents or host resources.
            run(
                "docker",
                "run",
                "--detach",
                "--rm",
                "--privileged",
                "--name",
                name,
                "--label",
                f"{LABEL}={token}",
                "-p",
                "127.0.0.1::6443",
                "-v",
                f"{output}:/output",
                "-e",
                "K3S_KUBECONFIG_OUTPUT=/output/kubeconfig",
                "-e",
                "K3S_KUBECONFIG_MODE=600",
                IMAGE,
                "server",
                "--disable-agent",
                "--disable=traefik,servicelb,metrics-server,local-storage",
                "--tls-san",
                "127.0.0.1",
                timeout=180,
            )
            address = run("docker", "port", name, "6443/tcp").stdout.strip()
            if not re.fullmatch(r"127\.0\.0\.1:[0-9]+", address):
                raise RuntimeError(f"unexpected disposable API binding: {address!r}")
            server = "https://" + address
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if kubeconfig.is_file():
                    ready = run(
                        "kubectl",
                        "--kubeconfig",
                        str(kubeconfig),
                        "--server",
                        server,
                        "--request-timeout=3s",
                        "get",
                        "--raw=/readyz",
                        timeout=5,
                        check=False,
                    )
                    if ready.returncode == 0 and ready.stdout.strip() == "ok":
                        break
                time.sleep(1)
            else:
                raise RuntimeError(
                    "disposable Kubernetes API did not become ready in 60 seconds"
                )
            yield kubeconfig, server
        finally:
            cleanup(name, token)


def main():
    for tool in ("docker", "kubectl", "helm"):
        if not shutil.which(tool):
            raise RuntimeError(
                f"required tool missing: {tool}; integration validation did not run"
            )
    local_docker_endpoint()
    if run("docker", "info", "--format", "{{.OSType}}").stdout.strip() != "linux":
        raise RuntimeError("a running local Linux Docker engine is required")
    if not run("helm", "version", "--short").stdout.strip().startswith("v4."):
        raise RuntimeError("Helm 4 is required for the SSA regression")
    with disposable_api() as (kubeconfig, server):
        result = run(
            sys.executable,
            str(ROOT / "test-runner-count-ownership-integration.py"),
            "--kubeconfig",
            str(kubeconfig),
            "--server",
            server,
            timeout=240,
            check=False,
        )
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        if result.returncode:
            raise RuntimeError(
                f"ownership integration regression failed (exit {result.returncode})"
            )
    print("Disposable API container removed.")


def interrupted(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    try:
        main()
    except KeyboardInterrupt:
        print("ownership integration check interrupted", file=sys.stderr)
        raise SystemExit(130)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"ownership integration check: {exc}", file=sys.stderr)
        raise SystemExit(1)
