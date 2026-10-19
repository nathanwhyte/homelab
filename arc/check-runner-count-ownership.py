#!/usr/bin/env python3
"""Run the Helm ownership regression in a fresh local Docker API server."""

from contextlib import contextmanager
import json
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


def run(*args, timeout=30, check=True, env=None):
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        # The integration launches Helm/kubectl grandchildren. Kill the entire
        # isolated group before its API server is removed.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed: {result.stderr.strip()}")
    return result


def docker(endpoint, *args, **kwargs):
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "DOCKER_CONTEXT",
            "DOCKER_HOST",
            "DOCKER_TLS",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        )
    }
    return run("docker", "--host", endpoint, *args, env=env, **kwargs)


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


def cleanup(endpoint, name, token):
    result = docker(
        endpoint,
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
            return False
        raise RuntimeError(f"cannot verify cleanup for {name}: {result.stderr.strip()}")
    if result.stdout.strip() != token:
        raise RuntimeError(
            f"refusing cleanup of container with a different owner: {name}"
        )
    docker(endpoint, "rm", "--force", name)
    return True


@contextmanager
def disposable_api(endpoint):
    token = uuid.uuid4().hex
    name = "arc-ssa-test-" + token
    # Pulling does not create a container, so do it before opening a launch
    # receipt. A killed Docker create client can leave daemon work pending.
    docker(endpoint, "pull", IMAGE, timeout=180)
    receipt_dir = Path(tempfile.mkdtemp(prefix="arc-ssa-recovery-"))
    receipt = receipt_dir / "container.json"
    receipt.write_text(
        json.dumps({"endpoint": endpoint, "name": name, "label": LABEL, "token": token})
        + "\n"
    )
    receipt.chmod(0o600)
    created = False
    with tempfile.TemporaryDirectory(prefix="arc-ssa-api-") as directory:
        output = Path(directory)
        kubeconfig = output / "kubeconfig"
        try:
            # No Kubernetes context is consulted. The credential belongs only
            # to this new API server, which has no agents or host resources.
            docker(
                endpoint,
                "create",
                "--rm",
                "--privileged",
                "--name",
                name,
                "--label",
                f"{LABEL}={token}",
                "-p",
                "127.0.0.1::6443",
                IMAGE,
                "server",
                "--disable-agent",
                "--disable=traefik,servicelb,metrics-server,local-storage",
                "--tls-san",
                "127.0.0.1",
                timeout=180,
            )
            created = True
            docker(endpoint, "start", name, timeout=30)
            address = docker(endpoint, "port", name, "6443/tcp").stdout.strip()
            if not re.fullmatch(r"127\.0\.0\.1:[0-9]+", address):
                raise RuntimeError(f"unexpected disposable API binding: {address!r}")
            server = "https://" + address
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if not kubeconfig.exists():
                    credential = docker(
                        endpoint,
                        "exec",
                        name,
                        "cat",
                        "/etc/rancher/k3s/k3s.yaml",
                        check=False,
                    )
                    if credential.returncode == 0 and credential.stdout.strip():
                        # Docker's root-owned file stays inside the container.
                        # The caller creates its own private credential copy.
                        with kubeconfig.open("x") as handle:
                            os.fchmod(handle.fileno(), 0o600)
                            handle.write(credential.stdout)
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
            try:
                removed = cleanup(endpoint, name, token)
                if not created and not removed:
                    # Bounded reconciliation can catch delayed registration,
                    # but absence cannot prove a failed create has finished.
                    for _ in range(10):
                        time.sleep(1)
                        if cleanup(endpoint, name, token):
                            removed = True
                            break
                    if not removed:
                        raise RuntimeError(
                            "container creation outcome remains indeterminate"
                        )
            except BaseException as exc:
                raise RuntimeError(
                    f"cleanup unconfirmed; retain recovery receipt {receipt}: {exc}"
                ) from exc
            else:
                receipt.unlink()
                receipt_dir.rmdir()


def main():
    for tool in ("docker", "kubectl", "helm"):
        if not shutil.which(tool):
            raise RuntimeError(
                f"required tool missing: {tool}; integration validation did not run"
            )
    endpoint = local_docker_endpoint()
    if docker(endpoint, "info", "--format", "{{.OSType}}").stdout.strip() != "linux":
        raise RuntimeError("a running local Linux Docker engine is required")
    if not run("helm", "version", "--short").stdout.strip().startswith("v4."):
        raise RuntimeError("Helm 4 is required for the SSA regression")
    with disposable_api(endpoint) as (kubeconfig, server):
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
