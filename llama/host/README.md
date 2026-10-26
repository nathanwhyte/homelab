# Host Ollama on timmy

The host placement dates to September 2026 (IMPR-1075): systemd owns timmy's Ollama daemon,
and Kubernetes consumers reach it through the selector-less Service and EndpointSlice in
`llama/ollama-service.yaml`. The pod Deployment is retired. This guide describes the
IMPR-1426 source contract; its new verification and recovery paths have not yet received
live host acceptance. Historical measurements in the Modelfiles retain their original dates.

| Piece | Installed location or consumer | Responsibility |
| --- | --- | --- |
| `model-posture.json` and `llama/ollama/*.Modelfile` | `/opt/ollama-host/` and its `modelfiles/` directory | Release, resident profiles, runtime settings, complete recipes, and explicit blob pins |
| `ollama_prepare.py` | `/opt/ollama-host/` | Preparation, adoption, verification receipts, and generated artifacts |
| `ollama_runtime.py` | `/opt/ollama-host/` | Daemon/runner evidence and failure stop tied to one daemon generation |
| `ollama_signals.py` | `/opt/ollama-host/` | Signal deferral across resource ownership and explicit interruptible work |
| `ollama_journal.py` | `/opt/ollama-host/` | Bounded boot/monotonic startup reads through host libsystemd |
| `ollama_recovery.py` and `ollama_attestation.py` | Host preparation/exporter directories; generated HTTP client | Idle recovery authorized by a root-owned receipt under the preparation lock |
| `ollama.service.d/homelab.conf` | `/etc/systemd/system/ollama.service.d/` | Generated daemon environment and failure-stop policy |
| `ollama-warm.sh` and `ollama-warm.service` | `/opt/ollama-host/`, `/etc/systemd/system/` | Compatibility entrypoint and synchronous post-start adoption |
| `99-homelab-dgpu-runpm.rules` | `/etc/udev/rules.d/` | Keeps the 9070 XT out of runtime suspend, which evicts VRAM to RAM (BUG-1242) |
| `install-host-ollama.sh` | Run on timmy with sudo | Installation ordering, migration gates, and final verification |
| `homelab-ollama-deploy.sh` | `/usr/local/sbin/`, root-owned | Passwordless deployment of GitHub `main` only |
| `homelab-deploy.sudoers` | `/etc/sudoers.d/homelab-deploy` | Exact wrapper modes and scoped unit controls |
| `llama/ollama/ollama-exporter.py` | `/opt/ollama-exporter/` | Prometheus metrics on port 9111 |

Host placement keeps one owner of port 11434, avoids Kubernetes hostPort/ServiceLB DNAT
arbitration, and lets the daemon survive k3s restarts and node drains. `ollama.service`
continues to run as user/group `ollama`. The warm unit uses `ExecStart=+` only for its
root-owned administrative verifier: reading `/proc`, systemd properties and the journal,
and retaining authority to stop the exact daemon it inspected.

## Preparation and acceptance

Edit model facts in `model-posture.json` and the referenced Modelfiles. Generate the daemon
drop-in and `llama/ollama-jobs.yaml` with `ollama_prepare.py render`; do not synchronize tag,
context, slot, or pin literals by hand in callers. The release pin comes from the JSON;
the installer rejects a contrary `OLLAMA_VERSION` environment override.

Preparation asks the pinned Ollama release to compile a unique candidate from each required
recipe. It reads the candidate back, checks explicit weights/projector pins, copies the
canonical manifest to the serving name when necessary, and reads the serving identity back.
The manifest comparison covers the consumer's full recipe semantics, including inherited
layers, templates/defaults, ordered stop strings, renderer/parser selection, and parameters
other than `num_ctx`. Gemma has explicit weights and projector pins; a bare base tag name is
still a moving reference. A successful create/copy request alone is insufficient.

These profiles remain selectable through the configuration's `resident` field. Rendering
derives the loaded-model limit from the selected resident set. Selection is not proof that
a profile fits the GPU: each selected profile must pass its own runtime verification.

| Selected resident | Intended loaded models | Additional prepared standby | Loaded-model limit |
| --- | --- | --- | --- |
| `gemma4:vlm` | Gemma | Qwen FIM | 1 |
| `qwen2.5-coder:fim` | Qwen FIM and Gemma | Deepseek instruct | 2 |
| `deepseek-coder-v2:fim` | Deepseek FIM | Deepseek instruct, Qwen FIM and Gemma | 1 |
| `deepseek-coder-v2:instruct` | Deepseek instruct | Deepseek FIM, Qwen FIM and Gemma | 1 |

The default remains Gemma E4B Q8_0, `num_ctx=65536`, `num_batch=2048`, four slots, and one
loaded model; Qwen FIM is a prepared rollback choice. These are configured values, not fresh
measurements. The Gemma E4B-to-12B rollback changes its recipe and approved blob pins under
the same `gemma4:vlm` name, then takes full adoption. It is not an extra resident selector.
The compatibility shell accepts `--reconcile-only` for preparation without loading and
`--ov-pair-only` for preparation of Qwen/Gemma without loading. Neither publishes runtime
acceptance. Global Python options (`--config`, `--recipes`, `--resident`, `--url`, `--state`)
precede the command.
`RESIDENT_TAG` in the caller's environment is ignored. Explicit `--resident` selects a
temporary profile for that command; it does not update installed configuration. Production
selection changes belong in `model-posture.json`, followed by rendering and full adoption.

Warmup serializes preparation and loads the intended residents sequentially. Acceptance
checks `/api/ps` identity, context and GPU residency, the daemon's actual environment and
systemd properties, runner process arguments, and each runner's `/props`. Startup journal
evidence must match the current boot, systemd invocation, daemon PID, runner command and
process start time. It proves context, slots, batch/micro-batch, flash attention, KV-cache,
Vulkan device identity and complete GPU layer offload. Missing, rotated-out, malformed,
ambiguous, or stale evidence fails closed; API availability is not a substitute.

The host must provide `libsystemd.so.0`. Its journal API seeks directly to the captured
boot and monotonic birth window, with exact invocation and daemon-PID filters, then reads
forward through startup. A startup ends at its first listening event; all start markers
inside the birth allowance are still considered for ambiguity. Later request logs and
unrelated journal metadata cannot displace this proof. Reads copy only identity, monotonic
timestamp and message fields, capped at 256 KiB per field, 16 MiB and 20,000 rows per
startup, with a 20-second deadline checked around native calls. These bounds apply to the
startup traversal, not the daemon's accumulated uptime. Retention must still preserve
the startup records; this verifier does not archive journal evidence or wait for new rows.

Runner birth is measured in the journal's monotonic clock through Linux TASKSTATS;
`/proc` boot-clock ticks identify the process generation, rather than being converted with
the current suspend offset. This requires `CONFIG_TASKSTATS`, an unshifted monotonic time
namespace, and root with `CAP_NET_ADMIN`; the existing warm unit's `ExecStart=+` supplies
administrative privileges. The measured birth interval must be no wider than one proc tick.
A valid but wider interval allows up to three independent samples under one runner pidfd;
each sample checks that the same process generation is still alive. Protocol, access,
timeout, identity and cancellation errors are terminal. Unavailable or still-ambiguous timing
refuses verification; a failed adoption attempts to stop its captured daemon and requires
the recovery procedure below. Plain observational verification can be retried after correcting
the cause. Suspend before or after process creation does not change the clock
binding. Live kernel support and timing still need validation during authorized host adoption.

Only successful acceptance atomically publishes `/var/lib/ollama-preparation/verified.json`.
Public Ollama aliases are not proof of host acceptance: API clients can copy them. The receipt binds configuration,
recipes, manifest digests and daemon/runner generations. The revision includes the entire
recipe text, including comments. Old `/run/ollama/models-ready` markers are not acceptance
evidence. `verify` checks the receipt and current state again; it does not rewarm models.
The exporter reads this root-owned record through `/preparation/recover`; HTTP callers
cannot write it. The endpoint checks the current daemon generation and holds the same lock
as preparation throughout recovery. Legacy witness tags are retired across the reserved
namespace, including models removed from the current catalog.

## Change and validate source

From the repository root, after editing the posture or any recipe:

```bash
uv run --no-project python llama/host/ollama_prepare.py render --root .
uv run --no-project python llama/host/ollama_prepare.py render --root . --check
uv run --no-project bash llama/host/tests/run-tests.sh
pre-commit run --files llama/host/model-posture.json llama/host/ollama.service.d/homelab.conf llama/ollama-jobs.yaml
```

Include every changed source/recipe path in the hook invocation too. Rendering and these
hermetic suites make no live model calls and the dispatcher refuses root. The installer integration seam is available only
to a non-root test process; root rejects its filesystem override. Passing tests, hooks and
review does not establish live readiness. Merge and deployment each need their own approval.

## Adopt a reviewed revision

These are deployment instructions, not a record of operations performed. Use one reviewed,
merged revision for the host and its generated recovery CronJob. Keep the existing Service,
EndpointSlice, pull CronJob and GPU-hold workload outside a routine posture deployment.
Before changing the host, suspend only the existing `ollama-warm` CronJob and let its active
Job finish (or explicitly cancel it). This prevents the previous recovery client, which
does not share the new lock, from interfering during migration. Preserve its current
manifest for recovery. The generated replacement explicitly sets `suspend: false`, so the
scoped apply after verified host adoption resumes the schedule.

1. On timmy, install through the trusted-main wrapper:

   ```bash
   sudo homelab-ollama-deploy.sh
   sudo git -C /opt/homelab-src rev-parse HEAD
   sudo python3 /opt/ollama-host/ollama_prepare.py verify
   ```

   The normal installer refuses an unreachable API before host installation changes. It
   prepares against the reachable daemon before changing host files, then installs all
   recipes/modules, restarts the daemon, waits for the warm unit, and runs
   `verify --stop-on-failure`. The host transaction has its own lock. Completion requires
   current receipt, residency and runtime evidence, not an asynchronous job
   acknowledgement or `/api/version` response. The deployed SHA is also logged in
   `/var/log/homelab-deploy.log`.

2. If bootstrapping or recovering an unavailable/mismatched daemon is intended, use the
   explicit mode instead:

   ```bash
   sudo homelab-ollama-deploy.sh --bootstrap-recovery
   ```

   It still checks cutover prerequisites, stops existing managed units, installs the pinned
   release and files, then performs the same final adoption checks. It permits initially
   absent units. It does not automatically roll back to another model or restart an
   unverified fallback. The wrapper and sudoers grant must first include this exact mode;
   see the passwordless setup section below.

3. After host verification succeeds, match the local checkout to the deployed SHA and
   extract only the `ollama-warm` CronJob from the generated multi-document file:

   ```bash
   warm_manifest=$(mktemp)
   uv run --no-project --with pyyaml python -c '
   import sys, yaml
   docs = list(yaml.safe_load_all(open(sys.argv[1])))
   matches = [d for d in docs if d and d.get("kind") == "CronJob" and d.get("metadata", {}).get("name") == "ollama-warm"]
   if len(matches) != 1:
       raise SystemExit("expected exactly one ollama-warm CronJob")
   yaml.safe_dump(matches[0], sys.stdout)
   ' llama/ollama-jobs.yaml > "$warm_manifest"
   kubectl diff -f "$warm_manifest"
   # Review the diff; apply only this object when the deployment is authorized.
   kubectl apply -f "$warm_manifest"
   kubectl -n llama get cronjob ollama-warm -o yaml
   rm "$warm_manifest"
   ```

   `kubectl diff` returns 1 for differences and greater than 1 for an error; investigate an
   error before applying. Read back the embedded recovery revision and intended residents.
   Do not apply all of `llama/ollama-jobs.yaml`: its second object is the separate suspended
   pull template. Observe the next authorized recovery run and relevant consumer paths
   before recording live acceptance.

### Files-only

`sudo homelab-ollama-deploy.sh --files-only` copies eligible files and reloads systemd
without restarting or warming. It requires an installed preparation configuration, byte-identical
recipe revisions, and a reachable pinned daemon. It does not compile or pull unused models. It refuses posture, version,
pin, resident selection, effective unit/drop-in, or recipe changes. Even comment-only recipe
edits require full adoption: their byte/revision change would otherwise strand the existing
receipt. Comments in hand-authored unit files may change if their directives stay the same.
The generated drop-in must still match its generator byte for byte: change its comments in
`dropin()` and rerender, rather than editing generated output. Such regenerated comment-only
changes pass the directive comparison. A legacy host without the new configuration requires full migration.

This mode also installs and applies the dedicated 9070 XT runtime-PM rule (BUG-1242),
as full adoption does. It reloads udev, triggers the `1002:7550` PCI device, waits for
udev to settle, and requires `power/control=on` before completing. Applying that policy
resumes the card without restarting Ollama. The Raphael iGPU keeps its runtime-PM policy.

## Failure and recovery boundaries

Warmup and the installer's final verification invalidate failed acceptance and ask the
runtime adapter to stop the daemon generation captured through a Linux pidfd. They never
fall back to signalling a numeric PID that might have been reused. The signal is `SIGHUP`:
the generated drop-in clears and sets `RestartPreventExitStatus=SIGHUP`, clears
`RestartForceExitStatus`, and requires `KillMode=control-group`, `SendSIGKILL=yes` and a
30-second stop timeout. Systemd readback uses the spelling `HUP`. These settings prevent
`Restart=always` from immediately reviving the failed daemon and keep its runners under
systemd cleanup. `SIGTERM` is not an interchangeable stop mechanism here.

If the stop policy, pidfd support, captured generation or journal evidence cannot be
validated, the operation reports failure instead of substituting a broader kill. Treat that
as unresolved: inspect `journalctl -u ollama -u ollama-warm`, correct the mismatch, and use
the explicit recovery workflow. A failed verifier must not be reported as a successful
stop unless its stop evidence confirms it. Direct manual unit controls remain available
for an operator; they do not establish a new acceptance receipt by themselves.

Each candidate create has a 900-second limit, including missing-base downloads. Keep the
model store after a timeout: cached blobs and resumable partial downloads may be reused on
retry. An adopting command invalidates acceptance and attempts to stop its captured daemon,
including one started by bootstrap. After confirming that stop and correcting any failure,
retry the trusted deployment with `--bootstrap-recovery`; the same bound and full proof apply.
Standalone `prepare`/`pair` commands do not stop the daemon and can retry the same command.
If preparation times out in the normal installer's pre-change gate, the host installation
and daemon remain unchanged: rerun the same deployment command and mode after correcting
the failure. That gate does not require a bootstrap recovery merely because creation timed out.

The generated recovery CronJob runs every 15 minutes, unprivileged and HTTP-only. It posts
its expected revision and residents to the existing exporter on port 9111. The exporter
runs as `ollama`, validates the root-owned receipt and current process generation, and takes
the preparation lock before loading. A busy lock or nonempty `/api/ps` makes recovery do no
work. When idle, the adapter checks the pinned version and serving digests against the
receipt, rechecks idle state, then issues load-only requests and verifies the resulting
resident digests, contexts and GPU residency. Neither endpoint nor CronJob can publish
acceptance, prepare recipes, change systemd, stop the daemon, or perform the privileged
journal proof. Missing/stale receipts require host adoption; forged Ollama aliases authorize
nothing. A successful load acknowledgement does not make recovery succeed.

The shared lock serializes recovery with host adoption. The repeated idle check narrows a
separate courtesy race with ordinary inference callers; it is not atomic admission. Another client
can load between the final observation and recovery's request, including between co-resident
loads. Recovery can then fail its final readback; HTTP offers no transaction that reserves
the GPU against such a caller. Plan host adoption or sensitive GPU work in a quiet window.

The receipt rename publishes completed proof. For `warm` and
`verify --stop-on-failure`, adoption starts after acquiring the preparation lock and
capturing the daemon generation. From that boundary the CLI owns `SIGINT` and `SIGTERM`
until process exit. Cancellation during earlier lock acquisition or generation capture
does not modify acceptance or stop a daemon: adoption authority has not begun.
Cancellation during adoption before command completion removes the receipt and attempts a generation-bound
stop, including cancellation after publication or during the final response flush. Ordinary
work raises a dedicated cancellation exception outside the `OSError`/`Exception` retry
hierarchies, and later signals can raise again. Resource contexts defer both signals before
handoff to `with`, retaining the mask through acquisition and release. Short file publication
and snapshot writes finish under that mask; long preparation and descriptor scopes enable
delivery only around explicit external operations. During an existing exception's unwind,
cancellation is recorded without replacing that exception so cleanup can run. Checks before
and after preparation operations reject recorded or kernel-pending cancellation if an inner
handler swallowed the exception or a critical section deferred delivery. Further signals
cannot interrupt cleanup. Publication locks wait at most one second and the core preparation
lock waits at most 30 seconds. Invalidation stops starting new cleanup requests after 30
seconds; an in-flight HTTP request retains its transport timeout. The captured-generation
stop is attempted even if invalidation fails or exhausts this budget. Kernel-stuck filesystem
calls are outside these in-process bounds.
After durable publication and a flushed response, the CLI blocks both signals and checks for
recorded or pending cancellation. An empty check completes the operation; later signals remain
blocked through runtime and lock release and cannot cancel the completed adoption. The CLI
never restores default signal handlers during that release.
Plain `verify` does not invalidate acceptance or stop the daemon when it fails or is cancelled.
An uncatchable process
death before the rename cannot publish partial acceptance; after a completed rename, the
durable receipt records completed proof even if the command's final reply was lost. Read it
back with `verify` before claiming success. Abandoned candidate UUIDs are recorded before
creation and cleaned on the next preparation under the shared lock, never by pruning the store.

Atomic writers keep unfinished bytes in a separate `.ollama-write-staging` directory.
Publication and staging reclamation share a directory lock; only committed UUID-named JSON
intents authorize candidate cleanup. Interrupted staging is discarded without interpreting
its contents. Files and directories are created with restrictive modes before publication,
independent of a permissive umask; replay rejects unsafe existing state without repairing it.
Publication and receipt invalidation sync their parent directory before returning.

## Passwordless setup and ordinary operations

The passwordless grant covers a root-owned wrapper that deploys GitHub `main` from
`/opt/homelab-src`. It fetches, resets and cleans that checkout and pins `REPO_DIR` to it;
edits under a user's home cannot become root code through this path. The grant permits exact
mode arguments and specific Ollama unit controls, with no `SETENV` or wildcard command.

The wrapper and sudoers privilege definition do not update themselves. A human must install
their reviewed versions with a password, including when adding `--bootstrap-recovery`:

```bash
# First setup only, if the root-owned checkout is absent:
sudo git clone https://github.com/nathanwhyte/homelab.git /opt/homelab-src
# Use the reviewed main revision in this root-owned checkout before installing:
sudo /opt/homelab-src/llama/host/install-sudo-deploy.sh
```

The setup refuses a user-writable source tree, validates sudoers before and after install,
and confirms the grant with `sudo -l -U noot`. Its installed filename has no dot because sudo
ignores dotted names under `sudoers.d`. Its `--check` and `--uninstall` modes inspect/remove
the grant.

| Task | Command or source change |
| --- | --- |
| Report host state | `sudo homelab-ollama-deploy.sh --check` |
| Change model/runtime/release | Edit the posture/recipe, render, validate, review, merge, then adopt |
| Check current acceptance | `sudo python3 /opt/ollama-host/ollama_prepare.py verify` |
| Restart manually | `sudo systemctl restart ollama`; wait for warm completion and run `verify` |
| Inspect residency | `curl -fsS http://192.168.1.19:11434/api/ps` is a snapshot, not full acceptance |
| Inspect physical VRAM | Read `power/control`, `mem_info_vram_used` and `mem_info_gtt_used` under `/sys/bus/pci/devices/0000:03:00.0/`; `ollama ps` counts GTT as GPU |
| Inspect metrics | `curl -fsS http://192.168.1.19:9111/metrics` |
| Pull an unrelated model | Separately authorize the suspended `ollama-pull` template's model and Job |

PCI runtime suspend can move the model's VRAM buffers into GTT (system RAM) while
`ollama ps` still reports GPU residency. The BUG-1242 measurements on 2026-10-26 showed
57 MB VRAM / 9,648 MB GTT while suspended and 9,305 MB VRAM / 410 MB GTT while serving.
Those are measurements of that model and host state, not acceptance thresholds for all
resident profiles. Check `power/control=on`, `power/runtime_status=active`, and the physical
VRAM/GTT counters separately. The installer's policy readback and the preparation receipt
do not prove that all model bytes remain in physical VRAM. The cost of keeping the card
awake is its idle power draw.

## Original cutover and host maintenance

The September 2026 cutover sequence remains relevant only when moving back from the retired
pod: copy the Longhorn model store with `--sync-models`, stop the old host before
`--sync-identity`, replace the Service routing, wait for old pods/ServiceLB/DNAT rules to
disappear, then explicitly bootstrap the host. Model copying excludes partial downloads
and does not copy cloud identity. Identity sync requires an inactive host, preserves a
private backup under `/var/backups/ollama-identity.*`, and installs the identity with mode
0600. A loopback authentication probe before DNAT removal can reach the old pod.

Never warm the host while the pod still holds the GPU. The installer checks the selector-less
ClusterIP Service, absence of old Ollama and ServiceLB pods, the single `ollama-timmy-host`
EndpointSlice pointing at `192.168.1.19`, NAT rules, and the listener. `ss` alone cannot show
hostPort DNAT. Preserve deployment/configuration snapshots before a placement migration,
and verify LAN, Tailscale, cluster DNS, cloud proxy routing and Prometheus afterward.

Node maintenance (`scripts/node-maintenance.sh reboot <node> --spin-down`) does not scale
a retired Ollama Deployment. The host daemon rides through a drain and stops on host reboot.
Host memory still counts: the preflight's `HOST_MEMORY_RESERVATIONS` default is `timmy=16Gi`,
matching `service_properties.MemoryMax` in `model-posture.json`. Keep that reservation in
`scripts/node-maintenance.sh` in sync when changing the declared limit, then rerender.
For a tight drain that needs the RAM, run `ssh timmy sudo systemctl stop ollama` manually
before draining. The node's noninteractive sudo grant covers apt/reboot, so the maintenance
script cannot stop this unit itself. After maintenance, restore the host through the
authorized warm/verification workflow; a manual start alone is not acceptance.
The GPU-hold workload reserves both AMD device-plugin slots so unrelated cluster jobs do
not claim the card. Lending the GPU is a separately authorized sequence: stop the host,
scale GPU-hold to zero, run the workload, then restore the guard and perform host verification.

For a planned timmy outage, use `scripts/timmy-offline.sh down` and, after returning to
Ubuntu, `scripts/timmy-offline.sh up`. The outage script owns workload parking, CronJob
suspension and alert silences. It delegates the node cycle to `node-maintenance.sh`:
`preflight <node>` checks before parking, `drain <node> --spin-down --outage` starts the
drain without rebooting (`preflight --allow-repeat --outage` and `drain --spin-down --outage
--repeat` when `down` finds its own cordon), and `recover <node>` finishes a recorded
reboot or restores preparation that stopped before a drain. `cycle-status <node>` prints
`recorded` or `none` for saved reboot evidence; use `scripts/maintenance-active.py` for
bounded maintenance windows instead.

Recovery state remains local to the Mac that started the work. Each mutating node-cycle
command takes the existing process lock; it does not lock the entire outage workflow
or offline interval. Cordon ownership does live in the cluster: every cordon
`node-maintenance.sh` makes sets the Node annotation `homelab/cordon-owner` to
`<purpose>:<ComputerName>` (`outage` for `down`, `drain`, or `reboot`) in the same write,
and `finish` clears it with the uncordon. A bare `drain` refuses a cordoned node; `drain
--repeat` re-drains only a cordon whose owner is this Mac with the same purpose, and a
repeat after a reboot replaces that reboot's proof. So `down` repeats only on this Mac's own
outage cordon, and refuses before any change on a hand cordon, a reboot or drain cycle's
cordon, or the other Mac's outage; run `up` on the Mac that started the outage, and never
uncordon another Mac's timmy outage. `finish` (and so `up`) checks the owner before its
Ready wait and again before it restores anything: it refuses a cordon that names another
Mac, and accepts an ownerless one. `kubectl drain` cordons by itself, so every drain is
followed by a check that the cordon is still this run's owned cordon.

These checks are made at moments, not held: nothing locks timmy across both Macs. Another
Mac's command, or a hand `kubectl cordon`, `uncordon` or `annotate` on the node, while a run
on this Mac is still going is outside what they cover. One that lands after `down`'s
preflight and leaves another owner (or none) on timmy is refused at the drain, after the
silence, suspends and parking; a hand uncordon keeps this Mac's owner, so the drain's own
re-cordon passes. What this Mac's `up` does next depends on where the refusal came:

- Before its cordon (no boot-ID record saved): `up` refuses while the other Mac's cordon is
  on timmy, naming it, and refuses an ownerless one; once that cordon is gone, `up` undoes
  this Mac's preparation.
- After its boot-ID record is saved: `up` first waits for timmy's boot ID to change, so
  before timmy reboots it times out without naming anyone. After a reboot it refuses a
  cordon that names the other Mac, naming it, and finishes an ownerless one (the drain's own
  re-cordon) as this Mac's. Once the other Mac has finished its cycle, this Mac's `up`
  cannot finish a cycle whose cordon is gone, and recovery is by hand: remove
  `~/.local/state/homelab-node-maintenance/timmy.boot-id` and re-run `up`, which then
  restores the preparation without a finish.

A `down` stopped between its cordon and its boot-ID record (a lost reply, or a kill) leaves
this Mac's owned cordon with no record: re-run `down`, or back it out as below and run `up`.

`down` writes its owner with the cordon, after its silence, suspends, parking and spin-down.
A `down` that stops before the cordon leaves nothing on timmy for the other Mac to refuse on,
so the other Mac's `down` would save the zeros it sees: as before this change, recover such a
`down` with `up` on the same Mac. A marker written before `down`'s first change is IDEA-1152.

`kubectl uncordon` keeps annotations, so back out a cycle with `kubectl uncordon timmy &&
kubectl annotate node timmy homelab/cordon-owner-`. An owner left on an uncordoned node is
stale: `status` lists it, and the next mutating command (`reboot`, a first `drain`,
`restore-services`, `recover` without a cycle) removes it. A hand uncordon followed by a
hand `kubectl cordon` with no such command between keeps the old owner, and that Mac's
`down` would take the cordon as its own. A cordon from before
owners were recorded has none; if it is this Mac's outage, claim it with `kubectl annotate
node timmy homelab/cordon-owner="outage:$(scutil --get ComputerName)" --overwrite` and re-run
`down`. A recorded cycle still requires proof of a changed boot ID and a
cordoned target; manually uncordoning it does not make `recover` a cancellation command.
If the Longhorn health check fails or is interrupted after uncordoning, the saved cycle
remains and strict `recover`/`finish` refuses a retry; that case still needs manual recovery.

## Placement rollback

A rollback to Kubernetes is an explicit placement change, separate from selecting one of
the preserved host model profiles. Suspend/delete the model CronJobs and finish active
model Jobs first; stop and disable host units; remove the host warm dependency; then restore
reviewed snapshots of the ConfigMap, Deployment (initially zero replicas) and LoadBalancer
Service. Diff and apply only those intended objects. Remove the host EndpointSlice and scale
the pod only after the host releases port 11434 and the GPU. Verify model output, cloud route,
metrics and LoadBalancer access. No preparation failure automatically performs this rollback.
