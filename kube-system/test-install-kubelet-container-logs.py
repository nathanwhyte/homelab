#!/usr/bin/env python3
"""Run with python3 kube-system/test-install-kubelet-container-logs.py [script].

Scenarios for install-kubelet-container-logs.sh (BUG-1293), each run against a
temporary copy of the script and drop-in, a temporary k3s data dir, and stub
sudo, systemctl, k3s, curl and sleep on PATH. Checks that the script installs
and verifies on agents and the server, exits non-zero with the revert command
when the node does not come back, keeps the prior drop-in when the install
fails before the restart, and restarts nothing on a re-run or --dry-run.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402

HERE = Path(__file__).resolve().parent
SCRIPT = (
    Path(sys.argv[1])
    if len(sys.argv) > 1
    else HERE / "install-kubelet-container-logs.sh"
)
CONF = HERE / "kubelet-container-logs.conf"

STUBS = {
    # k3s-agent is the installed unit; everything else is logged.
    "systemctl": """#!/usr/bin/env bash
if [ "$1" = cat ]; then [ "$2" = k3s-agent ] && exit 0; exit 1; fi
echo "systemctl $*" >>"$STUB_LOG"
""",
    # Runs commands unprivileged. STUB_SERVER=1: the admin kubeconfig exists.
    # STUB_READY: the node's Ready status. STUB_SIZE: live containerLogMaxSize.
    # STUB_INSTALL_FAIL=1: install writes a partial file and fails.
    "sudo": """#!/usr/bin/env bash
case "$1" in
install)
  shift; args=()
  while [ $# -gt 0 ]; do
    case "$1" in -m|-o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac
  done
  if [ "${STUB_INSTALL_FAIL:-0}" = 1 ]; then head -c 20 "${args[0]}" >"${args[1]}"; exit 1; fi
  cp "${args[@]}" ;;
test)
  if [ "$3" = /etc/rancher/k3s/k3s.yaml ]; then [ "${STUB_SERVER:-0}" = 1 ]; exit; fi
  "$@" ;;
k3s)
  echo "k3s ${*:2}" >>"$STUB_LOG"
  if [ "$3" = get ] && [ "$4" = node ]; then echo "${STUB_READY:-True}"; fi
  if [ "$3" = get ] && [ "$4" = --raw ]; then
    echo "{\\"kubeletconfig\\":{\\"containerLogMaxSize\\":\\"${STUB_SIZE:-50Mi}\\",\\"containerLogMaxFiles\\":5}}"
  fi ;;
*) "$@" ;;
esac
""",
    # The kubelet's healthz. STUB_HEALTH=bad makes it fail.
    "curl": """#!/usr/bin/env bash
if [ "${STUB_HEALTH:-ok}" = ok ]; then printf ok; exit 0; fi
exit 7
""",
    "sleep": "#!/usr/bin/env bash\nexit 0\n",
}


def run(env_extra, prior=None, args=()):
    """Run the script once; return (proc, installed text or None, stub log, leftover files)."""
    with tempfile.TemporaryDirectory(prefix="kcl-") as root:
        root = Path(root)
        bindir = root / "bin"
        bindir.mkdir()
        for name, body in STUBS.items():
            (bindir / name).write_text(body)
            (bindir / name).chmod(0o755)
        kdir = root / "kube-system"
        kdir.mkdir()
        shutil.copy(SCRIPT, kdir / "install.sh")
        shutil.copy(CONF, kdir / "kubelet-container-logs.conf")
        ddir = root / "data" / "agent" / "etc" / "kubelet.conf.d"
        ddir.mkdir(parents=True)
        dest = ddir / "50-container-logs.conf"
        if prior is not None:
            dest.write_text(prior)
        log = root / "log"
        log.write_text("")
        env = dict(
            os.environ,
            PATH=f"{bindir}:{os.environ['PATH']}",
            STUB_LOG=str(log),
            **env_extra,
        )
        proc = subprocess.run(
            ["bash", str(kdir / "install.sh"), "--data-dir", str(root / "data"), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        installed = dest.read_text() if dest.exists() else None
        leftovers = sorted(p.name for p in ddir.iterdir() if p.name != dest.name)
        return proc, installed, log.read_text(), leftovers


def main():
    src = CONF.read_text()
    failures = []

    def check(name, ok, detail):
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")
        if not ok:
            failures.append(name)

    p, c, log, left = run({})
    check(
        "agent install",
        p.returncode == 0 and c == src and not left and "restart k3s-agent" in log,
        f"exit {p.returncode}, installed={c == src}, leftovers={left}",
    )

    p, c, log, left = run({"STUB_SERVER": "1"})
    check(
        "server install verifies 50Mi",
        p.returncode == 0 and c == src and '"containerLogMaxSize":"50Mi"' in p.stdout,
        f"exit {p.returncode}",
    )

    for name, env in [
        ("server NotReady", {"STUB_SERVER": "1", "STUB_READY": "False"}),
        ("kubelet never healthy", {"STUB_HEALTH": "bad"}),
        ("live size not 50Mi", {"STUB_SERVER": "1", "STUB_SIZE": "10Mi"}),
    ]:
        p, c, log, left = run(env)
        check(
            f"{name} fails with revert hint",
            p.returncode != 0 and "To revert" in p.stderr,
            f"exit {p.returncode}",
        )

    p, c, log, left = run({"STUB_INSTALL_FAIL": "1"}, prior="PRIOR\n")
    check(
        "failed install keeps the prior drop-in",
        p.returncode != 0 and c == "PRIOR\n" and not left and "restart" not in log,
        f"exit {p.returncode}, prior kept={c == 'PRIOR' + chr(10)}, leftovers={left}, restarted={'restart' in log}",
    )

    p, c, log, left = run({}, prior=src)
    check(
        "re-run is a no-op",
        p.returncode == 0 and "already installed" in p.stdout and "restart" not in log,
        f"exit {p.returncode}",
    )

    p, c, log, left = run({}, args=("--dry-run",))
    check(
        "dry run changes nothing",
        p.returncode == 0 and c is None and "restart" not in log,
        f"exit {p.returncode}",
    )

    print(f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
