# Self-hosted GitHub Actions runners (ARC) — IDEA-1094

actions-runner-controller (the modern **gha-runner-scale-set** architecture) on
the K3s cluster. GitHub stays the forge; only compute moves in-cluster.
Ephemeral runner pods, scale-to-zero between jobs, no privileged containers.

| Piece           | Value                                                                                                                                                                                                           |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Charts          | `oci://ghcr.io/actions/actions-runner-controller-charts/{gha-runner-scale-set-controller,gha-runner-scale-set}`, pinned 0.14.2 in `deploy-arc.sh`                                                               |
| Controller      | release `arc`, ns `arc-systems` (1 replica, idles at tens of MB)                                                                                                                                                |
| Scale set       | release `compendium`, ns `arc-runners` → repo `nathanwhyte/compendium`; its listener pod lives in `arc-systems`                                                                                                 |
| `runs-on` label | `homelab-arc-compendium` (`runnerScaleSetName`) — **unique per repo** (`homelab-arc-<repo>`): the chart names the AutoscalingRunnerSet and service accounts after it, so a shared name collides across releases |
| Runner image    | `registry.nathanwhyte.dev/ci/actions-runner:latest` (custom, `images/runner/`)                                                                                                                                  |
| Container mode  | none (plain steps only) — unprivileged; a job declaring `container:` will fail                                                                                                                                  |
| Job hardening   | runner container: `runAsNonRoot`, no privilege escalation (sudo disabled), all capabilities dropped, `RuntimeDefault` seccomp                                                                                   |
| Egress          | `network-policies.yaml`: runner pods get DNS + public internet only — all private ranges denied (LAN, cluster CIDRs, link-local)                                                                                |
| Scheduling      | controller, listener, and runner pods all pinned off timmy (control plane + heaviest memory; listener via `listenerTemplate`)                                                                                   |

## ⚠ Private repos only

A self-hosted runner on a public repo executes fork-PR code on the cluster —
GitHub itself advises against it. `nathanwhyte/compendium` and
`nathanwhyte/dotfiles` are private; **`nathanwhyte/homelab` is public and must
never get a scale set**. Check before adding one:
`gh repo view <repo> --json visibility`.

## One-time setup

1. **GitHub credential** — create a fine-grained PAT scoped to the target
   private repo(s) with **Administration: Read and write** (repository
   permission; this is repo-level registration on a personal account — no org
   needed). Then:

   ```bash
   cp arc/github-config-secret.yaml.example arc/github-config-secret.yaml
   # fill in github_token, then:
   kubectl apply -f arc/github-config-secret.yaml
   ```

   The filled copy is gitignored (`**/*secret*.yaml`). A GitHub App is the
   upstream-recommended upgrade if the PAT's expiry churn gets annoying
   (secret keys: `github_app_id` / `github_app_installation_id` /
   `github_app_private_key`).

2. **Harbor project** — create project `ci` in the Harbor UI
   (<https://registry.nathanwhyte.dev>), public pull (so no imagePullSecret is
   needed in arc-runners), plus a robot account with push if you don't want to
   push as a user.

3. **Runner image** — `docker login registry.nathanwhyte.dev`, then:

   ```bash
   arc/images/runner/build-push.sh
   ```

   Builds linux/amd64 from the Mac and pushes `:latest` + a date tag.

4. **Deploy**:

   ```bash
   arc/deploy-arc.sh          # or --diff first
   ```

5. **Cut a workflow over** — in compendium's `.github/workflows/vault-checks.yml`,
   change each job's `runs-on: ubuntu-latest` to `runs-on: homelab-arc-compendium`.
   Push a branch and watch the run before merging. `actions/checkout`,
   `setup-uv`, `setup-python`, and `cache` all work self-hosted; the custom
   image bakes in uv/node/make so cache misses stay cheap. Note the runner
   container blocks `sudo` and privilege escalation — vault-checks doesn't use
   either, but any future workflow step that does will fail by design.

## Verifying

```bash
kubectl get pods -n arc-systems                                  # controller + per-scale-set listener pods
kubectl get pods -n arc-runners                                  # empty until a job runs; ephemeral runner pods appear per job
gh api repos/nathanwhyte/compendium/actions/runners --jq '.runners[].name'
gh workflow run "vault checks" -R nathanwhyte/compendium         # workflow_dispatch smoke test
```

## Operational notes

- **Scale-to-zero is the chart default** (`minRunners`/`maxRunners` unset →
  scale down to 0 when idle). We set `maxRunners: 3` to match vault-checks'
  three parallel jobs and protect the cluster.
- **Egress isolation** (`network-policies.yaml`, enforced by k3s's embedded
  kube-router NetworkPolicy controller): runner pods in `arc-runners` can
  reach cluster DNS and the public internet, and nothing in RFC1918 /
  link-local space. A compromised action or dependency inside a legitimate job
  therefore can't scan the LAN or cluster services. Model-in-the-loop CI
  against timmy's Ollama is a deliberate opt-in — uncomment the
  `192.168.1.19:11434` block in the policy when that lands, nothing broader.
- **Chart upgrades are NOT in-place**: Helm does not upgrade ARC's CRDs, so
  `helm upgrade` across chart versions leaves the controller and CRDs skewed —
  `deploy-arc.sh` detects a version mismatch against the installed release and
  refuses. Upstream's procedure: uninstall every scale set, uninstall the
  controller, then re-run `deploy-arc.sh` at the new version (re-installing
  applies the new CRDs). Controller and scale-set chart versions must match
  (one `ARC_CHART_VERSION` knob).
- **Runner image upgrades**: the runner release pins which Node runtimes JS
  actions get (Node 20 is removed from the runner 2026-09-23 — the very
  deprecation wave that triggered IDEA-1094). Self-hosted does **not** sidestep
  action Node deprecations (actions run on the runner's bundled Node), but the
  `RUNNER_VERSION` build arg in `images/runner/Dockerfile` controls when we
  take each jump. Don't fall far behind — the Actions service enforces a
  minimum runner version.
- **Disk / tool-cache hygiene is ours now**: ephemeral pods start clean each
  job (no tool-cache persistence), which is why the image pre-bakes uv/node.
  If job setup time ever matters, a PVC-backed `/opt/hostedtoolcache` is the
  next lever.
- **Model-in-the-loop CI (the homelab-specific win)**: runner pods sit next to
  timmy's Ollama (`http://192.168.1.19:11434`), so the IDEA-1091
  measured-skill regression suites can run in CI — impossible from
  GitHub-hosted runners. It's **off by default**: enable the commented Ollama
  block in `network-policies.yaml` first. Any such job counts as a
  **local-model consumer**: keep it serialized (one job at a time, e.g. a
  dedicated concurrency group) so it never runs alongside an interactive
  session's use of timmy's GPU.

## Teardown

```bash
helm uninstall compendium -n arc-runners
helm uninstall arc -n arc-systems        # after ALL scale sets are gone
kubectl delete -f arc/namespaces.yaml
```

Scale sets must be uninstalled before the controller (the chart's finalizers
need the controller alive to deregister runners from GitHub).
