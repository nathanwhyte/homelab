# Reapply values without upgrading charts

Grafana, Harbor, OpenWebUI, Dashboard, Headlamp and Garage use
`scripts/helm-deploy.py`. Python 3 and Helm are required. The helper queries the
release in its namespace and explicitly passes its deployed chart version to
Helm. Lookup failures, unexpected chart identities and non-deployed release
states stop the operation. An empty successful lookup means first install: the
helper resolves the available chart version and explicitly selects that version.

Chart upgrades remain deliberate Helm operations with an explicit `--version`
and reviewed values. Subsequent deploy-script runs reuse the resulting version.
Version reuse does not establish chart content integrity or make values changes
safe automatically.

Each of these six scripts accepts `--dry-run`. This runs server-side Helm
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

## Verification on 2026-09-06

All six scripts passed live server-side dry-runs using Helm `v4.2.4`.
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

Headlamp remains retired and absent: its first-install simulation resolved
`0.45.0` using temporary Helm repository settings and created no release.
Garage used upstream tag `v2.2.0`, commit
`582b168b6a985108c68aca45effae1d73203d6c3`, chart `script/helm/garage` (`0.9.2`)
from a temporary checkout. NVIDIA's existing `v26.3.3` pin equals the deployed
chart; that script was not deployed.

Run `python3 scripts/test-helm-deploy.py` for hermetic regression tests. They cover
lookup errors, version identity, prerelease versions, local-chart mismatches,
first-install selection, dry-run and diff flags, and all six script entry points
from an unrelated working directory with a kubectl stub that rejects every call.
These tests do not exercise real upgrades or establish application health.
