# Self-hosted GitHub Actions runners (ARC) — IDEA-1094

actions-runner-controller (the modern **gha-runner-scale-set** architecture) on
the K3s cluster. GitHub stays the forge; only compute moves in-cluster.
Ephemeral runner pods, four warm runners between jobs, no privileged containers.

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
| Scheduling      | controller stays off timmy; runner and listener pods stay off manu; runner pods have a hard two-node spread across timmy and wemby                                                                               |

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
   `setup-uv`, and `cache` all work self-hosted; the custom image bakes in
   uv/node/make and a uv-managed CPython, so `setup-python` is unnecessary (it
   would re-download the interpreter every job — see the tool-cache note
   below) and cache misses stay cheap. Note the runner
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

- **Capacity and placement**: `maxRunners: 8` with `minRunners: 4`. vault-checks
  runs four parallel jobs per push (`script suites (unit)` ∥ `script suites
  (e2e)`, `lint`, `formatting`), and a run peaks at 4–5 pods because the e2e
  lane holds its pod while the other lanes finish — so 8 covers two concurrent
  runs. Four warm runners (not 1) means a push starts immediately: pods need
  create → schedule → pull → register before GitHub can assign a job, and with
  one warm pod the rest of the run queues behind that lead time. Runner pods are
  spread across manu and wemby by a soft `topologySpreadConstraint`
  (`maxSkew: 1`, `whenUnsatisfiable: ScheduleAnyway`) — without it they all land
  on wemby, because the runner image is cached only there and `ImageLocality`
  outweighs the resource-fit scores that favour the emptier manu. Verify
  placement after a values change:

  ```bash
  kubectl get pods -n arc-runners \
    -o custom-columns=NAME:.metadata.name,NODE:.spec.nodeName
  ```

- **Measured runner footprint** (see the comment block on `resources` in
  `runner-scale-set-compendium-values.yaml`, which is the source of truth and
  carries the query forms). Original 1h sample over 49 pods: idle ~37 MiB / ~4m
  CPU, median ~425 MiB / ~333m, peak 736 MiB / 1.14 cores. Re-measured
  2026-09-14 over 2 days: **peak 809 MiB / 739m per pod, p90 CPU 522m**.
  The baseline now requests 512Mi/1000m and limits 2Gi/1500m; the September 15
  placement change bounded runner CPU on timmy. The memory limit came down from the 4Gi BUG-1099 set
  for a `pre-commit --all-files` spike that `PRE_COMMIT_NO_CONCURRENCY=1`
  (IMPR-1131) has since removed. What the refresh did change is CFS throttling,
  now 15.2% peak as a deduplicated fleet ratio against the 2.8% the 1h sample
  recorded; bounding aggregate per-node burn is tracked in IMPR-1166 and is not
  a per-pod sizing question.
  - Deduplicate before summing. Two ServiceMonitors (`prom-kubelet` and
    `kube-prometheus-stack-kubelet`) scrape the same kubelet cAdvisor endpoint
    and both carry `job="kubelet"`, so every container series exists twice and
    `sum()` doubles. Use an inner `max by (pod)`. `max`/`quantile` are unaffected,
    which is why the per-pod figures above needed no correction.
- **Cold start is ~1s, not the bottleneck**: `container_start_time_seconds`
  minus `kube_pod_created` measured a median of 1.0s (max 26s, and the 26s
  cases pulled the 679MB runner image on a node that lacked it). `imagePullPolicy`
  is `Always`, but layers are node-cached so the normal path is a fast manifest
  check. The cost of a fresh pod is therefore lead time before GitHub can assign
  a job, not pull time — which is what `minRunners` covers.

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
  job (no tool-cache persistence), which is why the image pre-bakes uv/node
  and, since IMPR-1131, a uv-managed CPython (`PYTHON_VERSION` build arg).
  `actions/setup-python` on ARC re-downloaded the interpreter every job
  (~15s) because `$RUNNER_TOOL_CACHE` is empty in a fresh pod; vault-checks no
  longer uses it. The image sets `UV_PYTHON_DOWNLOADS=never`, so a job that
  needs a Python the image lacks fails loudly — bump the build arg and the
  vault's `.python-version` together. GitHub's cache service (`actions/cache`,
  `setup-uv` `enable-cache`) works unchanged on self-hosted and is the right
  tool for per-repo state (uv cache, pre-commit envs); a PVC-backed
  `/opt/hostedtoolcache` was rejected as heavier than the image bake.
- **Model-in-the-loop CI (the homelab-specific win)**: runner pods sit next to
  timmy's Ollama (`http://192.168.1.19:11434`), so the IDEA-1091
  measured-skill regression suites can run in CI — impossible from
  GitHub-hosted runners. It's **off by default**: enable the commented Ollama
  block in `network-policies.yaml` first. Any such job counts as a
  **local-model consumer**: keep it serialized (one job at a time, e.g. a
  dedicated concurrency group) so it never runs alongside an interactive
  session's use of timmy's GPU.

## Bounded CPU trial (IMPR-1363)

The checked-in baseline remains min 4/max 8 runners with a 1-CPU request,
1.5-CPU limit, and 512Mi/2Gi memory request/limit. The opt-in trial keeps four
warm runners but caps the pool at four and raises each runner's limit to
2 CPUs. The existing hard two-node spread, CPU request, memory settings,
image, security context, and network policy stay unchanged. At steady state
the trial admits two runners per node, bounding runner CPU at four of timmy's
eight reported CPUs. Do not combine the 2-CPU limit with the baseline max 8:
four runners on timmy could consume all of its control-plane CPU capacity.

This tests individual-job latency at the cost of burst capacity. More than four
concurrent jobs can wait longer. The October 19 sample
[vault run 39261417269](https://github.com/nathanwhyte/compendium/actions/runs/39261417269)
started jobs after about one second; lint and e2e both ran on wemby. Their peak
one-minute CPU rates were 1.36 and 1.05 cores, with deduplicated CFS throttled
period fractions of 10.2% and 7.4%. Those fractions are not predicted wall-time
savings: serial work and wemby's older CPU may dominate. A separate observed
checkout stall lasted over 30 minutes; this CPU trial does not establish or
fix its cause.

Run these commands from this repository's worktree, after the current workload
has drained. Both modes require an existing deployed controller and scale-set
release at matching chart versions, then pin that installed version. An
explicit conflicting `ARC_CHART_VERSION` is rejected. They update only the
`compendium` scale-set release: no controller, namespace, network-policy,
credential, or chart-version changes. `jq` and the `helm-diff` plugin must
already be installed for the preflight.

```bash
# Review the exact scale-set change before applying the trial.
bash arc/deploy-arc.sh --runner-cpu-trial --diff
bash arc/deploy-arc.sh --runner-cpu-trial

# Restore the checked-in baseline, also after reviewing its diff.
bash arc/deploy-arc.sh --runner-cpu-baseline --diff
bash arc/deploy-arc.sh --runner-cpu-baseline
```

Diff and apply use the same values and installed chart version. Both reset
release values to the checked-in base file before adding any trial overrides;
review the diff for other live-versus-file drift before applying. Baseline mode
restores that file, not an arbitrary previous Helm revision. A regular deploy
also uses the baseline, so keep the trial window bounded and record its start,
end, and deployed chart version with IMPR-1363.

Compare repeated runs of the same commit and job mix before, during, and after
the trial. Record job queue and execution durations separately, node placement,
checkout time, CPU use/throttled periods, and control-plane health. Use an inner
`max by (pod, container)` before summing duplicate kubelet scrape series.
Restore baseline if queue time or control-plane health regresses; retain the
trial only if the end-to-end result improves. Successful Helm rollout alone
does not demonstrate a speedup.

A future dedicated fast worker pool would remove the control-plane CPU budget
constraint. A modern 8-core host with local NVMe and 16–32GB RAM could begin
with two runners, each requesting 2 CPUs/2Gi and limited to 3 CPUs/4Gi, leaving
host CPU headroom. Route lint/e2e explicitly to that separate scale set and
benchmark against the current pool before increasing concurrency. No dedicated
hardware or additional scale set is provisioned by this trial.

## Teardown

```bash
helm uninstall compendium -n arc-runners
helm uninstall arc -n arc-systems        # after ALL scale sets are gone
kubectl delete -f arc/namespaces.yaml
```

Scale sets must be uninstalled before the controller (the chart's finalizers
need the controller alive to deregister runners from GitHub).
