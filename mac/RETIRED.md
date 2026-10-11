# RETIRED — MacBook → cluster telemetry (2026-10-11)

Everything that shipped MacBook metrics or logs into the cluster is gone as of
this change. `mac/` now holds only the ollama-serving pieces.

## Why

It had never worked, and three separate failures hid each other.

1. **The transport was dead.** pop's Alloy scraped fine and appended to its WAL,
   but every `remote_write` was refused:

   ```text
   "Failed to send batch, retrying" err="Post http://192.168.1.19:30909/api/v1/write:
     dial tcp 192.168.1.19:30909: connect: connection refused"
   ```

   NodePorts 30909 (prometheus) and 31080 (loki gateway) are refused on every
   node, while 31920 (copyparty) is open on the same nodes — so kube-proxy
   NodePorts work here in general, and the fault is specific to those two
   Services. Prometheus holds **zero** series carrying Alloy's
   `source="macbook-m5"` label: not ollama, not macmon, not the freshness metric
   added and then removed again in BUG-1212. Diagnosing why the NodePorts are
   unbound is deliberately not part of this retirement — see "If it is ever
   wanted back".

2. **The exporters beneath it were dead too.** `mac-gpu-exporter` pointed at a
   path that stopped existing when this repo became a bare repo with worktrees;
   it had failed 10,781 times and served nothing (that one was retired earlier,
   in `BUG-1212`'s follow-up).

3. **Nothing alerted.** Alloy retries a failed write forever and no rule watches
   the writer, so a pipeline that delivered nothing looked exactly like a quiet
   one. The two Mac dashboards have therefore been drawing empty panels for as
   long as anyone can say.

Retiring it is cheaper than repairing it, and the metrics were duplicated
anyway: macmon covers the Mac's CPU/GPU/power/RAM locally.

## What was removed

| Removed | Was |
| --- | --- |
| `mac/docker-compose.yml` | The `ollama-exporter` + `alloy` containers |
| `mac/alloy/config.alloy` | Scrapes 9111/9113/9114, tails the ollama log, ships to the cluster |
| `mac/exporters/ollama-exporter.py` | pop's ollama metrics |
| `mac/exporters/macbook-backup-exporter.py` | The backup-freshness metric (added, then withdrawn — it had no route) |
| `mac/launchd/com.nathanwhyte.macmon.plist` | `macmon serve` on `:9113` |
| `mac/launchd/com.nathanwhyte.macbook-backup-exporter.plist` | The freshness exporter agent |
| `grafana/dashboards/mac-metrics.json`, `grafana/manifests/mac-metrics-dashboard.yaml` | The Mac Metrics dashboard, `macmon_*` only |
| `grafana/manifests/macbook-backup-alerts.yaml` | `MacbookBackupStale` / `MacbookBackupMetricMissing` |

## What was kept, and one repair

- **Kept** (still serving, not telemetry): `launchd/com.user.ollama-serve.plist`,
  `pf/com.user.ollama-serve.anchor`, `docs/ollama-pop-fw.md`.
- **Repaired**: `gpu/dashboards/gpu-overview-dashboard.yaml` and
  `grafana/dashboards/ollama-and-gpu-metrics.json` both mixed `macmon_*` into
  cluster-GPU and ollama panels. Two of the GPU expressions *added* a macmon
  term (`DCGM + amdgpu + macmon`), and in PromQL an addition with a missing
  series yields no data at all — so those panels were blank because of the dead
  Mac series, not because of anything wrong with the cluster GPUs. The macmon
  terms are gone and one divisor went `/ 3` → `/ 2`; the cluster's own GPU
  panels should now render again.

## Consequences to know about

- **`git log` is the history.** The manifests are deleted rather than retained,
  unlike `hermes/RETIRED.md` and `mem0/TORN-DOWN.md`, which keep theirs for
  reference. Nothing here is a deployable service, so there is nothing to
  resurrect by accident.
- **`grafana/nodeport-networkpolicy.yaml` is now stale in one respect.** Its
  `prometheus-nodeport-lockdown` and `loki-gateway-nodeport-lockdown` policies
  allow pop's three addresses as "the only legitimate external client", and that
  client no longer exists. The in-cluster allow rules are still load-bearing
  (Grafana queries Prometheus through them), so only the pop `ipBlock` entries
  are dead. Removing them is a follow-up, not part of this change.
- **The backup has no freshness signal now.** `MacbookBackupStale` was the one
  piece of this pipeline that was genuinely wanted, and retiring the transport
  took its route away with it — it never had a chance to fire. Carrying that
  signal some other way (a pop-local check, rather than a round trip through the
  cluster) is an open decision; the marker it reads,
  `~/Library/Application Support/macbook-backup/last-success`, is unchanged and
  still stamped on every clean run.
- **To uninstall on pop**: `launchctl bootout gui/$(id -u)/com.nathanwhyte.macmon`
  and `.../com.nathanwhyte.macbook-backup-exporter`, then remove their plists
  from `~/Library/LaunchAgents/`, and `docker compose -f mac/docker-compose.yml
  down -v` before the compose file is gone. `macmon` itself came from Homebrew.

## If it is ever wanted back

Two questions need answering first, in this order: why the 30909/31080
NodePorts are unbound (the whole Mac→cluster path depends on them, and nothing
else uses them), and whether a pipeline whose write failures are invisible
should get an alert on the writer before anything else is added to it.
