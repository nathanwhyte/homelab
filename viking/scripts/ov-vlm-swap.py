#!/usr/bin/env python3
"""Swap prod OpenViking's VLM route (IMPR-1311), stdlib only.

Reads the LIVE ``openviking-standalone-config`` ConfigMap and replaces only the ``vlm``
block of its ``ov.conf``; every other key stays exactly as it runs. ``restore`` writes
the ``vlm`` block from this repo's ``openviking-standalone-configmap.yaml`` (the reviewed
baseline: the LiteLLM router since IMPR-1311). ``gemma`` writes the direct gemma4:vlm
route, bypassing the router; ``haiku`` writes the direct Haiku route of the round-2
window. Each target's key Secret is checked before any change. Without ``--apply``
it prints the ``kubectl diff`` and changes nothing. With ``--apply`` it replaces that one
ConfigMap under the resourceVersion it read (a concurrent edit makes it fail with a
Conflict instead of being overwritten), restarts ``deployment/openviking`` (the
config-rewrite init container only runs at pod start) and waits for the rollout. When
the ConfigMap already matches, ``--apply`` still restarts, so a retry recovers a
restart that failed.

Prerequisites for ``haiku`` (see viking/docs/2026-10-13-impr-1311-haiku-vlm-round2.md):
the ``anthropic-vlm-key`` Secret holds a nonempty ``API_KEY`` (checked before any
change) and the pod runs ``ov_claude_params_patch.py`` (Haiku 5.5 rejects the
``temperature`` OpenViking always sends).
"""

import argparse
import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

NS = "viking"
CM = "openviking-standalone-config"
DEPLOY = "deployment/openviking"
REPO_CM = (
    Path(__file__).resolve().parent.parent
    / "manifests"
    / "openviking-standalone-configmap.yaml"
)

# Round 1 (compendium IMPR-1311): thinking is on by default for Haiku 5.5 and cut 3 of
# 12 Working Memory rewrites at 8192 output tokens; with it disabled and a 16384 cap all
# 12 completed. max_tokens also caps memory extraction (extract_loop), whose default is
# 32768. The key is not workspace-scoped, so the workspace rides in a header.
HAIKU_VLM = {
    "provider": "openai",
    "model": "claude-haiku-5-5",
    "api_key": "${ANTHROPIC_KEY}",
    "api_base": "https://api.anthropic.com/v1/",
    "extra_headers": {"anthropic-workspace-id": "wrkspc_01Mt86Lk5sxm3HSnaM1PbWSA"},
    "extra_request_body": {"thinking": {"type": "disabled"}},
    "max_tokens": 16384,
    "max_concurrent": 4,
    "timeout": 300,
}

# The direct route before the router: Ollama on timmy through chat-ollama.
GEMMA_VLM = {
    "provider": "openai",
    "model": "gemma4:vlm",
    "api_key": "${OLLAMA_KEY}",
    "api_base": "http://chat-ollama.llama.svc.cluster.local:11434/v1",
    "max_concurrent": 4,
    "timeout": 1200,
}

# The placeholder in a vlm block -> the Secret and key the init container reads for it.
KEY_SECRETS = {
    "${ANTHROPIC_KEY}": ("anthropic-vlm-key", "API_KEY"),
    "${LITELLM_KEY}": ("litellm-vlm", "LITELLM_MASTER_KEY"),
    "${OLLAMA_KEY}": ("ollama-api-key", "API_KEY"),
}


def kubectl(*args, stdin=None, check=True):
    return subprocess.run(
        ["kubectl", *args], input=stdin, capture_output=True, text=True, check=check
    )


def repo_ov_conf() -> dict:
    """The ``ov.conf`` block scalar from the repo ConfigMap, parsed without a YAML library."""
    lines = REPO_CM.read_text().splitlines()
    start = lines.index("  ov.conf: |") + 1
    body = []
    for line in lines[start:]:
        if line.strip() and not line.startswith("    "):
            break
        body.append(line[4:])
    return json.loads("\n".join(body))


def live_cm() -> dict:
    cm = json.loads(kubectl("get", "configmap", CM, "-n", NS, "-o", "json").stdout)
    meta = cm["metadata"]
    # Keep identity, labels, annotations and resourceVersion: `kubectl replace` with the
    # read resourceVersion fails with a Conflict instead of overwriting a change made to
    # any other ov.conf key since this read.
    cm["metadata"] = {
        k: meta[k]
        for k in ("name", "namespace", "labels", "annotations", "resourceVersion")
        if k in meta
    }
    return cm


def require_key(vlm: dict) -> None:
    """The init container falls back to an empty api_key, which OV refuses at start."""
    secret = KEY_SECRETS.get(vlm.get("api_key", ""))
    if secret is None:
        return
    name, key = secret
    out = kubectl(
        "get", "secret", name, "-n", NS,
        "-o", f"jsonpath={{.data.{key}}}", check=False,
    )  # fmt: skip
    if out.returncode != 0 or not out.stdout.strip():
        sys.exit(f"Secret {NS}/{name} is missing or has no {key}; not applying")


def restart() -> None:
    print(kubectl("rollout", "restart", DEPLOY, "-n", NS).stdout.strip())
    print(
        kubectl("rollout", "status", DEPLOY, "-n", NS, "--timeout=600s").stdout.strip()
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("target", choices=("restore", "gemma", "haiku", "status"))
    ap.add_argument(
        "--apply",
        action="store_true",
        help="apply the ConfigMap and restart OpenViking",
    )
    a = ap.parse_args()

    cm = live_cm()
    conf = json.loads(cm["data"]["ov.conf"])
    if a.target == "status":
        vlm = {k: v for k, v in conf.get("vlm", {}).items() if k != "api_key"}
        print(json.dumps(vlm, indent=2))
        return 0

    targets = {"haiku": HAIKU_VLM, "gemma": GEMMA_VLM}
    new_vlm = (
        copy.deepcopy(targets[a.target])
        if a.target in targets
        else repo_ov_conf()["vlm"]
    )
    if a.apply:
        require_key(new_vlm)
    if conf.get("vlm") == new_vlm:
        # The ConfigMap may match while the pod still runs the old config (a restart
        # that failed after an earlier apply), so --apply still restarts.
        print(f"vlm already matches {a.target}")
        if a.apply:
            restart()
        return 0
    conf["vlm"] = new_vlm
    cm["data"]["ov.conf"] = json.dumps(conf, indent=2) + "\n"

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "configmap.json"
        path.write_text(json.dumps(cm))
        diff = kubectl("diff", "-f", str(path), check=False)
        if diff.returncode > 1:
            print(diff.stderr, file=sys.stderr)
            return diff.returncode
        print(diff.stdout or "(no diff)")
        if not a.apply:
            print(
                "dry run: re-run with --apply to apply this ConfigMap and restart OpenViking"
            )
            return 0
        replaced = kubectl("replace", "-f", str(path), check=False)
        if replaced.returncode != 0:
            print(replaced.stderr, file=sys.stderr)
            print(
                "ConfigMap changed since it was read (or replace failed); re-run to see a fresh diff",
                file=sys.stderr,
            )
            return replaced.returncode
        print(replaced.stdout.strip())
    restart()
    return 0


if __name__ == "__main__":
    sys.exit(main())
