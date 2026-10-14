#!/usr/bin/env python3
"""IMPR-1311 round 2: swap prod OpenViking's VLM between Claude Haiku 5.5 and the repo's
VLM (gemma4:vlm), stdlib only.

Reads the LIVE ``openviking-standalone-config`` ConfigMap and replaces only the ``vlm``
block of its ``ov.conf``; every other key stays exactly as it runs. ``haiku`` writes
the block below; ``restore`` writes the ``vlm`` block from this repo's
``openviking-standalone-configmap.yaml`` (the reviewed baseline). Without ``--apply``
it prints the ``kubectl diff`` and changes nothing. With ``--apply`` it applies that one
ConfigMap, restarts ``deployment/openviking`` (the config-rewrite init container only
runs at pod start) and waits for the rollout.

Prerequisites for ``haiku`` (see viking/docs/2026-10-13-impr-1311-haiku-vlm-round2.md):
the ``anthropic-vlm-key`` Secret exists and the pod runs ``ov_claude_params_patch.py``
(Haiku 5.5 rejects the ``temperature`` OpenViking always sends).
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
    # Keep identity and labels; drop server-managed fields so the object applies cleanly.
    cm["metadata"] = {
        k: meta[k] for k in ("name", "namespace", "labels", "annotations") if k in meta
    }
    cm["metadata"].get("annotations", {}).pop(
        "kubectl.kubernetes.io/last-applied-configuration", None
    )
    return cm


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("target", choices=("haiku", "restore", "status"))
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

    new_vlm = copy.deepcopy(HAIKU_VLM) if a.target == "haiku" else repo_ov_conf()["vlm"]
    if conf.get("vlm") == new_vlm:
        print(f"vlm already matches {a.target}; nothing to do")
        return 0
    conf["vlm"] = new_vlm
    cm["data"]["ov.conf"] = json.dumps(conf, indent=2) + "\n"

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(cm, fh)
        path = fh.name
    diff = kubectl("diff", "-f", path, check=False)
    if diff.returncode > 1:
        print(diff.stderr, file=sys.stderr)
        return diff.returncode
    print(diff.stdout or "(no diff)")
    if not a.apply:
        print(
            "dry run: re-run with --apply to apply this ConfigMap and restart OpenViking"
        )
        return 0
    if a.target == "haiku":
        kubectl("get", "secret", "anthropic-vlm-key", "-n", NS)  # raises when missing
    print(kubectl("apply", "-f", path).stdout.strip())
    print(kubectl("rollout", "restart", DEPLOY, "-n", NS).stdout.strip())
    print(
        kubectl("rollout", "status", DEPLOY, "-n", NS, "--timeout=600s").stdout.strip()
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
