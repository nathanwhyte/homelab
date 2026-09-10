# Reapply values without upgrading charts

Grafana, Harbor, OpenWebUI, Dashboard, Garage, Longhorn and NVIDIA use
`scripts/helm-deploy.py`. Python 3 and Helm are required. The helper queries the
release in its namespace and explicitly passes its deployed chart version to
Helm. Lookup failures, unexpected chart identities and non-deployed release
states stop the operation. An empty successful lookup means first install: the
helper resolves the available chart version and explicitly selects that version.

Chart upgrades remain deliberate Helm operations with an explicit `--version`
and reviewed values. Subsequent deploy-script runs reuse the resulting version.
Version reuse does not establish chart content integrity or make values changes
safe automatically.

Each of these seven scripts accepts `--dry-run`. This runs server-side Helm
simulation and exits before Kubernetes manifest applies, probe patches or
rollout operations. It checks the Helm releases only, not those later operations.
Rendered output is suppressed because chart NOTES and ConfigMaps may contain
credentials. Configure the required Helm repositories before simulation.
Harbor's `--diff` also exits before applies and propagates plugin errors.

Garage uses a local chart. Set `GARAGE_CHART_DIR` to its directory if the nested
Garage checkout is absent. The helper rejects a local chart whose version differs
from the deployed version; Helm's `--version` cannot select different local files.
Matching version metadata alone does not prove identical chart contents.

A chart argument is treated as local only when it is an absolute path or
starts with `./` or `../`; such a path must exist or the helper exits
non-zero with a clear error. Any other ref — `harbor/harbor`, `grafana/loki`,
a bare chart name — goes through the remote repo/alias path regardless of
whether a same-named directory happens to exist in the current working
directory.

A release name need not equal its chart name. NVIDIA's release is
`gpu-operator-1774050554`, a generated name kept because live resources are
annotated to it; the helper matches on chart identity (`gpu-operator-<version>`
in the `helm list` row), so the two differing is fine. The test stub models this
with a release-to-chart map rather than assuming the names agree.

## Verification on 2026-09-06

All six scripts then present passed live server-side dry-runs using Helm `v4.2.4`
(Headlamp's script has since been removed, 2026-09-08).
Installed chart versions and release revisions were unchanged afterward:

| Namespace/release                         | Chart version | Revision before/after |
| ----------------------------------------- | ------------- | --------------------- |
| grafana/kube-prometheus-stack             | 87.17.0       | 20 / 20               |
| grafana/k8s-monitoring                    | 3.8.4         | 2 / 2                 |
| grafana/loki                              | 7.1.0         | 14 / 14               |
| harbor/harbor                             | 1.19.1        | 5 / 5                 |
| openwebui/open-webui                      | 15.2.0        | 7 / 7                 |
| kubernetes-dashboard/kubernetes-dashboard | 7.14.0        | 2 / 2                 |
| garage/garage                             | 0.9.2         | 2 / 2                 |

Headlamp was retired and absent at the time: its first-install simulation resolved
`0.45.0` using temporary Helm repository settings and created no release; the script
and manifests were deleted on 2026-09-08. Garage used upstream tag `v2.2.0`, commit
`582b168b6a985108c68aca45effae1d73203d6c3`, chart `script/helm/garage` (`0.9.2`)
from a temporary checkout. NVIDIA's existing `v26.3.3` pin equals the deployed
chart; that script was not deployed.

Run `python3 scripts/test-helm-deploy.py` for hermetic regression tests. They cover
lookup errors, version identity, prerelease versions, local-chart mismatches,
first-install selection, dry-run and diff flags, and all seven script entry points
from an unrelated working directory with a kubectl stub that rejects every call.
These tests do not exercise real upgrades or establish application health.

## Longhorn adoption on 2026-09-10 (IMPR-1094)

`longhorn/deploy-longhorn.sh` was the last near-miss script still carrying a
hardcoded pin. It had held `CHART_VERSION="1.11.0"` while `1.12.0` was live, so
running it as written would have issued `helm upgrade --version 1.11.0` against
32 attached volumes. Longhorn does not support downgrades, which makes that a
data-integrity risk rather than a downtime one; it was caught only because the
version was checked by hand first.

Verified live against the deployed release (`longhorn-1.12.0`, revision 8):

```text
longhorn-system/longhorn: reusing deployed chart longhorn 1.12.0
longhorn-system/longhorn: server dry-run passed; chart version 1.12.0
```

The release was still `longhorn-1.12.0` at revision 8 afterwards — the dry-run
changed nothing. The version is now read from the cluster instead of the file,
so the pin cannot go stale again.

`gpu/nvidia/deploy-nvidia-gpu.sh` still hardcodes `--version v26.3.3` and has
never been cross-checked against live `helm list -n gpu-operator`. It is the one
remaining pinned script.

## NVIDIA adoption on 2026-09-10 (IMPR-1148)

The remaining pinned script is migrated; no homelab deploy script now decides a
chart version from a file.

Unlike Longhorn, this pin was **not** wrong. The cross-check the IMPR-1094 TODO
asked for was done first: the script's `v26.3.3` equalled the live release
(`gpu-operator-1774050554`, chart `gpu-operator-v26.3.3`, deployed, revision 5),
so running it as written would have re-applied the version it was already on.
It was migrated because nothing kept that true, and because upstream had already
moved to `v26.7.0` — leaving the pin as both "what is deployed" and "what we
would like deployed" at once.

The blast radius is why it mattered more than its size suggests: `values.yaml`
sets `driver.enabled: false` under a warning not to flip it back, because
[BUG-1102] moved the NVIDIA driver to a host dkms install after the operator's
container rebuild cost manu its GPU for an hour. An unreviewed chart move on this
release is the one that could quietly reintroduce operator-managed drivers.

Moving the release from `v26.3.3` to `v26.7.0` is deliberately **not** part of
this change. It is a chart upgrade on a GPU-serving release with a post-BUG-1102
values contract and wants its own review of the upstream changelog, particularly
anything touching `driver.*`. The migration does not perform it: `helm-deploy.py`
reuses whatever is deployed.
