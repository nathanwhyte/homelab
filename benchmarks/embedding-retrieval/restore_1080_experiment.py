"""Restore the saved deployment when the control-plane connection returns."""

import argparse
import json
import subprocess
import time

from benchmark_1080_slots import DEPLOY, cleanup_restore, now


def main():
    from pathlib import Path

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("directory", type=Path)
    args = ap.parse_args()
    snapshot = json.loads((args.directory / "production-before.json").read_text())
    if snapshot["spec"]["replicas"] != 1:
        raise RuntimeError("Unexpected saved replica count")
    while True:
        try:
            probe = subprocess.run(
                [
                    "kubectl",
                    "--request-timeout=10s",
                    "-n",
                    "viking",
                    "get",
                    "deployment",
                    DEPLOY,
                    "-o",
                    "json",
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            probe = subprocess.CompletedProcess(exc.cmd, 1, "", str(exc))
        if probe.returncode:
            event = {"at": now(), "waiting_for_api": probe.stderr.strip()}
        else:
            current = json.loads(probe.stdout)
            if (
                current["metadata"]["uid"] != snapshot["metadata"]["uid"]
                or current["spec"]["template"] != snapshot["spec"]["template"]
            ):
                raise RuntimeError("Deployment changed; refusing automatic overwrite")
            try:
                cleanup_restore(snapshot, args.directory)
                return
            except (subprocess.SubprocessError, OSError) as exc:
                event = {"at": now(), "restoration_retry": str(exc)}
        with (args.directory / "restoration-attempts.jsonl").open("a") as stream:
            stream.write(json.dumps(event) + "\n")
        print(json.dumps(event), flush=True)
        time.sleep(30)


if __name__ == "__main__":
    main()
