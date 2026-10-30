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

- **Capacity and placement**: `maxRunners: 7` with `minRunners: 4` (8 until
  2026-10-30). vault-checks runs four parallel jobs per push (`script suites
  (unit)` ∥ `script suites (e2e)` ∥ `script suites (landing)`, `lint`; the
  duplicate-ID check is a step of `lint` since 2026-10-30), and a run peaks at
  4–5 pods because the e2e lane holds its pod while the other lanes finish. 7
  is what wemby + timmy can place under the 1-CPU request and the maxSkew-1
  spread (timmy admits 3, wemby then at most 4); an eighth pod sat Pending.
  Four warm runners (not 1) means a push starts immediately: pods need
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

The checked-in baseline remains min 4/max 7 runners (max 8 until 2026-10-30)
with a 1-CPU request, 1.5-CPU limit, and 512Mi/2Gi memory request/limit. The
trial ran as Helm revision 10 from 2026-10-19 and was ended 2026-10-30 with
`--runner-cpu-drain` (revision 11) then `--runner-cpu-baseline` (revision 12);
compendium IMPR-1363 records what it measured. The opt-in trial keeps four
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

Run these commands from this repository's worktree. All three modes require an existing deployed controller and scale-set
release at matching chart versions, then pin that installed version. An
explicit conflicting `ARC_CHART_VERSION` is rejected. They update only the
`compendium` scale-set release: no controller, namespace, network-policy,
credential, or chart-version changes. `jq` and the `helm-diff` plugin must
already be installed for the preflight.

**Hold one exclusive operator maintenance window for the entire transition,
in both directions.** Coordinate with other operators and deployment jobs so
none can change this scale set until replacement and verification finish. The
helper does not acquire a lock; use its explicit drain mode below. An idle-job snapshot
does not fence new arrivals: ARC 0.14.2's installed `immediate` strategy can
overlap old busy runners with their replacement, exceeding the intended
aggregate CPU bound ([versioned controller contract](https://github.com/actions/actions-runner-controller/blob/gha-runner-scale-set-0.14.2/controllers/actions.github.com/autoscalingrunnerset_controller.go#L58-L71)).

Choose the target; **baseline restoration follows the same complete drain
procedure as trial activation**. Preview it before pausing admission:

```bash
arc_cpu_target=--runner-cpu-trial # Use --runner-cpu-baseline for restoration.
bash arc/deploy-arc.sh "$arc_cpu_target" --diff
```

Inspect count ownership before changing admission. A legacy `kubectl-patch`
entry already co-owned these fields on the October 19 deployment. The helper
below only removes that exact count-only `Update` entry when Helm `Apply`
already owns both counts. It preserves all spec values and all other owners;
UID and resource-version tests make a concurrent change fail the patch. An
unexpected owner, extra owned fields, or missing Helm ownership stops for
investigation. This is a one-time metadata handoff, not a conflict override.

```bash
python3 arc/runner-count-ownership.py
# Review the metadata-only JSON patch, then apply and re-check.
python3 arc/runner-count-ownership.py --apply
python3 arc/runner-count-ownership.py --check
```

Save the current working revision and chart/template evidence for diagnosis
before draining. Then pause admission through Helm, preserving the installed
values and pod template. Review a count-only diff (and the chart's derived
values hash) before applying; stop on any other change.

```bash
helm history compendium -n arc-runners --output json
bash arc/deploy-arc.sh --runner-cpu-drain --diff
bash arc/deploy-arc.sh --runner-cpu-drain
```

After a successful drain, capture its deployed revision. Verify its stored
values and live counts are zero; retain this checkpoint outside the shell
before applying either CPU target. Recovery must return to this zero-count
revision, never the previous nonzero revision.

```bash
arc_cpu_drain_revision="$(
  helm list --namespace arc-runners --deployed --filter '^compendium$' --output json |
    jq -er 'if length == 1 and .[0].status == "deployed" then
      .[0].revision | tonumber | select(. > 0)
      else error("one deployed compendium revision is required") end'
)"
helm history compendium -n arc-runners --output json
helm get values compendium -n arc-runners --revision "$arc_cpu_drain_revision" --output json |
  jq -e '.minRunners == 0 and .maxRunners == 0'
```

Confirm that captured revision is the successful drained deployment and its
`gha-runner-scale-set` chart version matches the deployed controller reported
by the helper preflight. If capture or verification fails, stop before changing
the CPU template. Do not patch counts with kubectl, including with
`--field-manager=helm`: an `Update` owner can still conflict with Helm `Apply`.

Zero minimum and maximum prevent new runner pods for arriving jobs
([GitHub's drain procedure](https://docs.github.com/en/actions/how-tos/manage-runners/use-actions-runner-controller/deploy-runner-scale-sets#example-jobs-queue-draining)).
Keep admission at zero while existing jobs finish. Repeat these read-only
checks until **both print `true` and exit 0**. The second check requires no
runner pods or EphemeralRunners across any old or current set; any remaining
EphemeralRunnerSet must have zero desired and observed replicas. Do not
delete busy runners to make the checks pass.

```bash
kubectl get autoscalingrunnerset homelab-arc-compendium -n arc-runners -o json |
  jq -e '.spec.minRunners == 0 and .spec.maxRunners == 0'
kubectl get pods,ephemeralrunners,ephemeralrunnersets -n arc-runners \
  -l actions.github.com/scale-set-name=homelab-arc-compendium -o json |
  jq -e 'all(.items[]; .kind == "EphemeralRunnerSet" and
    (.spec.replicas // 0) == 0 and .status.currentReplicas == 0)'
```

With both checks satisfied and the exclusive window still held, re-preview
and apply the selected target. This is the point that reopens admission.
Keep the window through replacement: verify the AutoscalingRunnerSet returns
to `Running`, old runner sets have no remaining runners, and new runner pods
have the intended CPU limit and node placement before releasing it.

```bash
bash arc/deploy-arc.sh "$arc_cpu_target" --diff
bash arc/deploy-arc.sh "$arc_cpu_target"
kubectl get autoscalingrunnersets,ephemeralrunnersets,pods -n arc-runners \
  -l actions.github.com/scale-set-name=homelab-arc-compendium -o wide
```

If interrupted after pausing, leave admission paused; do not automatically
resume it. Inspect live state after any failed apply because it may have
partially reopened admission. If the latest release remains `deployed`, use
`--runner-cpu-drain` again and repeat both drain checks before changing templates.

If Helm instead reports `failed` or a pending state such as `pending-upgrade`
or `pending-rollback`, all helper modes deliberately refuse that state. Keep
the exclusive window while recovering to the captured zero-count revision.
Confirm the original deploy,
rollback, or Helm process has terminated before recovery: a pending state can mean a legitimate
operation is still running, and rollback must not race it. First inspect history, the
captured drained revision's values and manifest, and the currently deployed controller:

```bash
helm history compendium -n arc-runners
helm get values compendium -n arc-runners --revision "${arc_cpu_drain_revision:?Restore the captured zero-count revision}" --output json |
  jq -e '.minRunners == 0 and .maxRunners == 0'
helm get manifest compendium -n arc-runners --revision "${arc_cpu_drain_revision:?Restore the captured zero-count revision}"
helm list --namespace arc-systems --deployed --filter '^arc$' --output json
```

Proceed only when the explicit positive revision is still retained, was
verified drained before this transition, and its chart version still matches
the deployed controller. Check its resources, CPU/count settings, and template
against the recorded drained deployment, including zero counts in the manifest.
Do not choose revision `0`, omit the revision,
or guess from the latest history row. If no trusted matching revision exists,
stop without changing the CPU template and investigate; do not edit Helm storage/history,
delete release secrets, or force an upgrade/rollback.

Preview the exact rollback. The installed `helm diff rollback` interface
compares stored revisions, so also compare the retained manifest with live
objects to expose partial activation and any other drift. Inspect
both diffs; stop on unexpected resources or configuration changes.

```bash
helm diff rollback compendium "${arc_cpu_drain_revision:?Restore the captured zero-count revision}" -n arc-runners --suppress-secrets
helm get manifest compendium -n arc-runners --revision "${arc_cpu_drain_revision:?Restore the captured zero-count revision}" |
  kubectl diff -f -
```

Only after review, restore that explicit revision with a bounded wait:

```bash
helm rollback compendium "${arc_cpu_drain_revision:?Restore the captured zero-count revision}" -n arc-runners --wait --timeout 5m
```

Rollback restores the retained zero-count chart/values, so it does not reopen
admission. Keep the window held and repeat both drain checks before any further
template change. Confirm the latest
Helm revision is `deployed` and chart parity still holds, then re-preview/apply
`--runner-cpu-baseline` through the normal helper. If rollback fails or is
interrupted, inspect live counts and runners and stop for diagnosis;
any later recovery repeats this fenced procedure with the same verified
revision. Do not bypass the helper's deployed-state guard or retry another
revision blindly. If the initial count-only drain failed before any zero-count
checkpoint existed, no CPU-template change has been authorized yet: after
confirming the original Helm process terminated, recover that initial operation
to its recorded working revision (same pod template), then start the drain again.
That initial recovery can resume the original runner configuration; it must
never be used after a CPU-target apply. Helm's bounded `--wait` is not proof of the ARC pod rollout;
retain the live old-set, CPU-limit, and node-placement checks above.

The helper's diff and apply use the same values and installed chart version. Trial and baseline reset
release values to the checked-in base file before adding any trial overrides;
review the diff for other live-versus-file drift before applying. Baseline mode
restores that file, not an arbitrary previous Helm revision. A regular deploy
also uses the baseline, so keep the trial window bounded and record its start,
end, and deployed chart version with IMPR-1363.

Drain uses `--reuse-values` and records zero counts in Helm's release history.
Activation checks Helm-only ownership, live zero counts, and absence of old
runners immediately before applying. Keep the exclusive operator window: these
checks detect unsafe state but cannot serialize another operator's deployment.

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

### Ownership regression tests

`python3 arc/test-deploy-arc.py` runs in pre-commit and covers the ownership
handoff's fail-closed contract, drain predicates, mode scoping, and recovery
commands. The `arc-ownership-integration` hook also runs the actual Helm/Kubernetes
regression whenever deployment, ownership or runbook files change:

```bash
python3 arc/check-runner-count-ownership.py
```

It requires Helm 4, kubectl and a running local Linux Docker engine reached by
a Unix socket. Missing tools, an unavailable engine or remote Docker fail the
check; they never count as a skipped pass. The driver starts a uniquely named,
digest-pinned K3s API without agents, publishes a random loopback-only port,
and passes its generated credentials and server explicitly to the test. Docker
needs privileged mode for this disposable container. No live Kubernetes context
is consulted. Every Docker operation is pinned to the validated Unix socket,
even if another terminal changes the active context. Credentials are copied
into a caller-owned private file, so rootful Linux Docker works for a nonroot
committer too. The driver defers handled signals until it owns each launched
process, kills the integration process group before cleanup on timeout or
interruption, then removes only its own labeled container and anonymous volumes.

An interrupted `docker create` can leave daemon-side creation pending. The
driver retries identity-checked cleanup for ten seconds; if the outcome remains
unknown, it fails with the path to a private `arc-ssa-recovery-*/container.json`
receipt instead of claiming removal. The receipt preserves the original socket,
container name and ownership token. Inspect that named container on that socket,
compare its `compendium.arc-ssa-test` label with the receipt token, and remove it
only on a match. Retain the receipt until the launch outcome is reconciled.
The pinned image is reused from the local cache; only a missing image is pulled.
Driver tests cover endpoint pinning,
caller-owned credentials, delayed creation, recovery receipts and descendants.

For debugging with an already prepared disposable API, the underlying test
also accepts an explicit kubeconfig and loopback server:

```bash
python3 arc/test-runner-count-ownership-integration.py \
  --kubeconfig /private/tmp/impr-1363-ssa/kubeconfig \
  --server https://127.0.0.1:16443
```

The integration test requires an explicit loopback API, refuses an existing
`arc-runners` namespace or ARC scale-set CRD, and cleans up its fixture. It
reproduces the real legacy-owner conflict, checks resource-version rejection,
performs the metadata-only handoff, drains in both directions, and recovers
from a post-upgrade failure after counts and CPU values changed. It exercises
Helm 4 and a real API server, not an ARC controller or runner workload. It
passed with Helm 4.3.1 and a disposable K3s 1.36.2 container on October 19.

## Teardown

```bash
helm uninstall compendium -n arc-runners
helm uninstall arc -n arc-systems        # after ALL scale sets are gone
kubectl delete -f arc/namespaces.yaml
```

Scale sets must be uninstalled before the controller (the chart's finalizers
need the controller alive to deregister runners from GitHub).
