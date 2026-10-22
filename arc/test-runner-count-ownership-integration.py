#!/usr/bin/env python3
"""Exercise real Helm 4 SSA on a disposable, explicitly selected local API server.

Requires a fresh Kubernetes API with neither arc-runners nor ARC CRDs present.
Creates only fixture resources there; never uses the implicit kubectl context.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
CRD = "autoscalingrunnersets.actions.github.com"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--server", required=True)
    args = parser.parse_args()
    if urlparse(args.server).hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("only an explicit loopback disposable API server is supported")
    kube = [
        shutil.which("kubectl"),
        "--kubeconfig",
        args.kubeconfig,
        "--server",
        args.server,
    ]
    helm = [
        shutil.which("helm"),
        "--kubeconfig",
        args.kubeconfig,
        "--kube-apiserver",
        args.server,
    ]

    def run(argv, *, failure=False, env=None):
        result = subprocess.run(
            argv, text=True, capture_output=True, timeout=45, env=env
        )
        if failure:
            assert result.returncode != 0, "expected failure: " + repr(argv)
        elif result.returncode:
            raise RuntimeError(repr(argv) + "\n" + result.stdout + result.stderr)
        return result

    def read():
        return json.loads(
            run(
                kube
                + [
                    "get",
                    "autoscalingrunnerset",
                    "homelab-arc-compendium",
                    "-n",
                    "arc-runners",
                    "-o",
                    "json",
                    "--show-managed-fields",
                ]
            ).stdout
        )

    def current_revision():
        return str(
            json.loads(
                run(
                    helm + ["status", "compendium", "-n", "arc-runners", "-o", "json"]
                ).stdout
            )["version"]
        )

    version = run(helm + ["version", "--short"]).stdout.strip()
    assert version.startswith("v4."), "the regression requires Helm 4 SSA"
    for resource, name in (("namespace", "arc-runners"), ("crd", CRD)):
        assert not run(
            kube + ["get", resource, name, "--ignore-not-found", "-o", "json"]
        ).stdout.strip(), "fixture identity already exists; refusing reuse"
    module_spec = importlib.util.spec_from_file_location(
        "ownership", ROOT / "runner-count-ownership.py"
    )
    ownership = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(ownership)
    made_namespace = made_crd = False
    with tempfile.TemporaryDirectory(prefix="arc-ssa-") as directory:
        tmp = Path(directory)
        chart = tmp / "chart"
        (chart / "templates").mkdir(parents=True)
        (chart / "Chart.yaml").write_text(
            "apiVersion: v2\nname: arc-ownership-fixture\nversion: 0.1.0\n"
        )
        (chart / "values.yaml").write_text(
            "minRunners: 4\nmaxRunners: 8\ncpu: 1500m\nfailAfterApply: false\n"
        )
        (
            chart / "templates" / "runner.yaml"
        ).write_text("""apiVersion: actions.github.com/v1alpha1
kind: AutoscalingRunnerSet
metadata:
  name: homelab-arc-compendium
spec:
  minRunners: {{ .Values.minRunners }}
  maxRunners: {{ .Values.maxRunners }}
  cpu: {{ .Values.cpu | quote }}
{{- if .Values.failAfterApply }}
---
apiVersion: batch/v1
kind: Job
metadata:
  name: deliberate-post-upgrade-failure
  annotations:
    helm.sh/hook: post-upgrade
    helm.sh/hook-delete-policy: before-hook-creation,hook-failed
spec:
  template:
    spec:
      nodeSelector:
        arc-fixture-never-schedule: "true"
      restartPolicy: Never
      containers:
        - name: unused
          image: busybox:1.37
          command: ["false"]
{{- end }}
""")
        crd = tmp / "crd.json"
        crd.write_text(
            json.dumps(
                dict(
                    apiVersion="apiextensions.k8s.io/v1",
                    kind="CustomResourceDefinition",
                    metadata=dict(name=CRD),
                    spec=dict(
                        group="actions.github.com",
                        scope="Namespaced",
                        names=dict(
                            plural="autoscalingrunnersets",
                            singular="autoscalingrunnerset",
                            kind="AutoscalingRunnerSet",
                        ),
                        versions=[
                            dict(
                                name="v1alpha1",
                                served=True,
                                storage=True,
                                schema=dict(
                                    openAPIV3Schema=dict(
                                        type="object",
                                        properties=dict(
                                            spec=dict(
                                                type="object",
                                                properties=dict(
                                                    minRunners=dict(type="integer"),
                                                    maxRunners=dict(type="integer"),
                                                    cpu=dict(type="string"),
                                                ),
                                            )
                                        ),
                                    )
                                ),
                            )
                        ],
                    ),
                )
            )
        )
        wrapper = tmp / "kubectl"
        wrapper.write_text(
            "#!"
            + sys.executable
            + "\nimport os,sys\nos.execv("
            + repr(kube[0])
            + ", "
            + repr(kube)
            + " + sys.argv[1:])\n"
        )
        wrapper.chmod(0o755)
        environment = {**os.environ, "PATH": str(tmp) + os.pathsep + os.environ["PATH"]}
        target = ["autoscalingrunnerset", "homelab-arc-compendium", "-n", "arc-runners"]

        def upgrade(values, *, failure=False):
            return run(
                helm
                + [
                    "upgrade",
                    "compendium",
                    str(chart),
                    "-n",
                    "arc-runners",
                    "--reuse-values",
                    "--set",
                    values,
                    "--timeout",
                    "2s",
                ],
                failure=failure,
            )

        try:
            run(kube + ["create", "namespace", "arc-runners"])
            made_namespace = True
            run(kube + ["create", "-f", str(crd)])
            made_crd = True
            run(
                kube
                + ["wait", "--for=condition=Established", "--timeout=20s", "crd/" + CRD]
            )
            run(
                helm
                + [
                    "install",
                    "compendium",
                    str(chart),
                    "-n",
                    "arc-runners",
                    "--server-side=true",
                ]
            )
            for counts in ((3, 7), (4, 8)):
                run(
                    kube
                    + [
                        "patch",
                        *target,
                        "--type=merge",
                        "--patch",
                        json.dumps(
                            {"spec": dict(zip(("minRunners", "maxRunners"), counts))}
                        ),
                    ]
                )
            upgrade(
                "minRunners=4,maxRunners=8"
            )  # same-value apply creates co-ownership
            good = current_revision()
            conflict = upgrade("minRunners=0,maxRunners=0", failure=True)
            assert "kubectl-patch" in conflict.stderr and "conflict" in conflict.stderr
            run(
                helm
                + [
                    "rollback",
                    "compendium",
                    good,
                    "-n",
                    "arc-runners",
                    "--timeout",
                    "5s",
                ]
            )
            before = read()
            stale_patch = ownership.handoff_patch(before)
            run(kube + ["annotate", *target, "fixture-generation=changed"])
            run(
                kube
                + ["patch", *target, "--type=json", "--patch", json.dumps(stale_patch)],
                failure=True,
            )
            run(
                [sys.executable, str(ROOT / "runner-count-ownership.py"), "--check"],
                failure=True,
                env=environment,
            )
            run(
                [sys.executable, str(ROOT / "runner-count-ownership.py"), "--apply"],
                env=environment,
            )
            run(
                [sys.executable, str(ROOT / "runner-count-ownership.py"), "--check"],
                env=environment,
            )
            assert read()["spec"] == before["spec"]
            assert read()["metadata"]["annotations"]["fixture-generation"] == "changed"
            upgrade("minRunners=0,maxRunners=0")
            drain_revision = current_revision()
            assert read()["spec"] == dict(minRunners=0, maxRunners=0, cpu="1500m")
            upgrade("minRunners=4,maxRunners=4,cpu=2,failAfterApply=true", failure=True)
            assert read()["spec"] == dict(minRunners=4, maxRunners=4, cpu="2")
            run(
                helm
                + [
                    "rollback",
                    "compendium",
                    drain_revision,
                    "-n",
                    "arc-runners",
                    "--timeout",
                    "5s",
                ]
            )
            assert read()["spec"] == dict(minRunners=0, maxRunners=0, cpu="1500m")
            upgrade("minRunners=4,maxRunners=4,cpu=2,failAfterApply=false")
            upgrade("minRunners=0,maxRunners=0")
            trial_drain_revision = current_revision()
            assert read()["spec"] == dict(minRunners=0, maxRunners=0, cpu="2")
            upgrade("minRunners=4,maxRunners=8,cpu=1500m")
            assert read()["spec"] == dict(minRunners=4, maxRunners=8, cpu="1500m")
            run(
                helm
                + [
                    "rollback",
                    "compendium",
                    trial_drain_revision,
                    "-n",
                    "arc-runners",
                    "--timeout",
                    "5s",
                ]
            )
            assert read()["spec"] == dict(minRunners=0, maxRunners=0, cpu="2")
            print(
                "PASS: "
                + version
                + "; real SSA conflict, stale-version refusal, metadata-only handoff, both drains, partial activation failure and zero-count rollback"
            )
        finally:
            if made_namespace:
                run(kube + ["delete", "namespace", "arc-runners", "--wait=false"])
            if made_crd:
                run(kube + ["delete", "crd", CRD, "--wait=false"])


if __name__ == "__main__":
    main()
