# Self-hosted GitHub Actions runners (ARC) — IDEA-1094

actions-runner-controller (the modern **gha-runner-scale-set** architecture) on
the K3s cluster. GitHub stays the forge; only compute moves in-cluster.
Ephemeral runner pods, scale-to-zero between jobs, no privileged containers.

| Piece           | Value                                                                                                                                             |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| Charts          | `oci://ghcr.io/actions/actions-runner-controller-charts/{gha-runner-scale-set-controller,gha-runner-scale-set}`, pinned 0.14.2 in `deploy-arc.sh` |
| Controller      | release `arc`, ns `arc-systems` (1 replica, idles at tens of MB)                                                                                  |
| Scale set       | release `compendium`, ns `arc-runners` → repo `nathanwhyte/compendium`                                                                            |
| `runs-on` label | `homelab-arc` (`runnerScaleSetName`; the same label is reused for any future repo's scale set)                                                    |
| Runner image    | `registry.nathanwhyte.dev/ci/actions-runner:latest` (custom, `images/runner/`)                                                                    |
| Container mode  | none (plain steps only) — unprivileged; a job declaring `container:` will fail                                                                    |
| Scheduling      | controller, listener defaults, and runner pods all avoid timmy (control plane + heaviest memory)                                                  |

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
   change each job's `runs-on: ubuntu-latest` to `runs-on: homelab-arc`. Push a
   branch and watch the run before merging. `actions/checkout`, `setup-uv`,
   `setup-python`, and `cache` all work self-hosted; the custom image bakes in
   uv/node/make so cache misses stay cheap.

## Verifying

```bash
kubectl get pods -n arc-systems                                  # controller
kubectl get pods -n arc-runners                                  # listener idles; runner pods appear per job
gh api repos/nathanwhyte/compendium/actions/runners --jq '.runners[].name'
gh workflow run "vault checks" -R nathanwhyte/compendium         # workflow_dispatch smoke test
```

## Operational notes

- **Scale-to-zero is the chart default** (`minRunners`/`maxRunners` unset →
  scale down to 0 when idle). We set `maxRunners: 3` to match vault-checks'
  three parallel jobs and protect the cluster.
- **Chart upgrades**: bump `ARC_CHART_VERSION` (or the default in
  `deploy-arc.sh`) and keep controller + scale-set versions matched. Known
  upstream wart: after some chart upgrades the listener wedges until its
  `AutoscalingListener` is deleted and recreated
  (actions-runner-controller#3726) — `kubectl delete autoscalinglistener -n
arc-systems --all` is safe; the controller recreates them.
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
- **Model-in-the-loop CI (the homelab-specific win)**: runner pods are on the
  cluster network, so a workflow step can reach timmy's Ollama
  (`http://192.168.1.19:11434`) for the IDEA-1091 measured-skill regression
  suites — impossible from GitHub-hosted runners. Any such job counts as a
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
