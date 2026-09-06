#!/usr/bin/env python3
"""Read an owner-only R2 JSON file and provision the exporter Secret without logging values."""

import argparse
import base64
import json
import os
from pathlib import Path
import stat
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path.home() / "code/keys/homelab-backup-freshness-r2.json",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    info = args.path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Credential file must be owner-only (chmod 600)")
    data = json.loads(args.path.read_text())
    required = {
        "RCLONE_CONFIG_R2_TYPE",
        "RCLONE_CONFIG_R2_ACCESS_KEY_ID",
        "RCLONE_CONFIG_R2_SECRET_ACCESS_KEY",
        "RCLONE_CONFIG_R2_ENDPOINT",
    }
    if not isinstance(data, dict) or not required <= data.keys():
        raise ValueError("Missing required R2 configuration keys")
    if any(
        not key.startswith("RCLONE_CONFIG_R2_")
        or not isinstance(value, str)
        or not value
        for key, value in data.items()
    ):
        raise ValueError("Invalid R2 configuration key or value")
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": "backup-freshness-r2-credentials", "namespace": "grafana"},
        "data": {
            key: base64.b64encode(value.encode()).decode()
            for key, value in data.items()
        },
    }
    command = [
        "kubectl",
        "apply",
        "--server-side",
        "--field-manager=backup-freshness-deploy",
        "-f",
        "-",
    ]
    if args.dry_run:
        command.append("--dry-run=server")
    # Capture both streams: API validation errors must not echo secret payloads.
    result = subprocess.run(
        command, input=json.dumps(secret), text=True, capture_output=True
    )
    if result.returncode:
        raise RuntimeError(
            "Secret provisioning failed; output suppressed to protect credentials"
        )
    print(
        "grafana/backup-freshness-r2-credentials: "
        + ("validated" if args.dry_run else "provisioned")
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(
            "Credential provisioning failed; verify the owner-only JSON file and kubectl access"
        )
