# yt-dlp revival (IDEA-019)

YouTube playlist downloader on the homelab K3s cluster. One custom
Docker image, one 200 Gi PVC on wemby, six daily CronJobs (one enabled,
five suspended until the validation run proves the pipeline).

## Files

| File | Purpose |
|------|---------|
| `docker/Dockerfile` | `nathanwhyte/yt-dlp:2026.06.10-yt-dlp` build |
| `namespace.yaml` | `yt-dlp` namespace |
| `longhorn-node-wemby-tag.yaml` | Patches the wemby Longhorn Node with a `wemby` tag |
| `longhorn-hdd-wemby-storageclass.yaml` | Dedicated SC: 2 replicas, HDD, wemby-pinned |
| `media-pvc.yaml` | 200 Gi PVC, RWO, on `longhorn-hdd-wemby` |
| `yt-dlp-config.yaml` | `yt-dlp.conf` ConfigMap |
| `yt-dlp-playlists.yaml` | 6 playlist URLs + names + per-playlist extra args |
| `cronjob-validation.yaml` | Enabled CronJob for playlist 2 ("Gotta Keep These Somewhere") |
| `cronjobs.yaml` | 5 suspended CronJobs (playlists 1, 3, 4, 5, 6) |
| `setup.sh` | Idempotent bootstrap (apply in dep order) |
| `operator.sh` | Subcommand helper: `run-job`, `logs`, `archive-list`, etc. |
| `scripts/r2-consolidate.py` | Pre-existing R2 directory consolidation tool (unrelated to yt-dlp) |

## First-time setup

1. **Build the image** (from repo root):
   ```
   docker build -t nathanwhyte/yt-dlp:2026.06.10-yt-dlp media/yt-dlp/docker/
   docker push  nathanwhyte/yt-dlp:2026.06.10-yt-dlp
   ```
   The Dockerfile is `python:3.12-alpine` + ffmpeg +
   `yt-dlp[default]` (bundles `yt-dlp-ejs` for JS challenge solving) +
   `bgutil-ytdlp-pot-provider` (in-process POT tokens, no sidecar).

2. **Apply manifests**:
   ```
   bash media/yt-dlp/setup.sh
   ```
   This is idempotent. Re-run it after manifest edits to re-converge.

3. **Verify the PVC** binds to a wemby-backed Longhorn volume:
   ```
   kubectl get pvc -n yt-dlp media
   # STATUS should be Bound, the VOLUME name maps to a Longhorn
   # volume whose spec.nodeSelector contains "wemby".
   ```

## Validation run

Don't wait for the 02:10 UTC schedule — trigger the validation run
manually:

```
kubectl create job --from=cronjob/yt-dlp-playlist-2 -n yt-dlp test-validation
kubectl logs -n yt-dlp -l job-name=test-validation -f
```

Or via the operator script:
```
media/yt-dlp/operator.sh run-job 2
media/yt-dlp/operator.sh logs test-validation
```

What to look for in the logs:
- `[youtube] Extracting URL: ...` for each video in the playlist
- `[download] Destination: /downloads/Gotta Keep These Somewhere/<title>.webm`
- `[download] 100% of ~<size>MiB`
- Exit code 0; no `ERROR: Sign in to confirm you're not a bot` (would
  indicate YouTube is blocking the cluster's egress IP — see Q7 below)

After the run, the archive file should have one line per processed
video:
```
kubectl exec -n yt-dlp <debug-pod> -- tail /yt-dlp-archive/archive-playlist-2.txt
```
(`<debug-pod>` is any pod mounting the `media` PVC, e.g. a one-off
`alpine:3.20` via `kubectl run --rm -it --image=alpine:3.20 ...`.)

## Enabling the other 5 playlists

Once the validation run is clean:

```
for n in 1 3 4 5 6; do
  media/yt-dlp/operator.sh enable $n
done
```

Or flip `suspend: true` → `suspend: false` in `cronjobs.yaml` and
re-apply with `kubectl apply -f media/yt-dlp/cronjobs.yaml`.

## Operator script

```
Usage: media/yt-dlp/operator.sh <command> [args]

  run-job <N> [extra-args]   Manually run CronJob N now (N in 1..6)
  list-jobs                  List Jobs and CronJobs in the namespace
  logs [job|pod]             Stream logs (most recent if no arg)
  archive-list               Show per-playlist archive stats
  archive-reset <N>          Wipe archive-playlist-N.txt (re-downloads all)
  list-cronjobs              Show schedule + suspend state for all 6
  enable <N>                 Un-suspend CronJob N
  disable <N>                Suspend CronJob N
  download <URL> [args...]   Ad-hoc single-video download
  reap-jobs                  Delete completed/failed Jobs older than 1h
```

## Open questions

- **Q5 (retention policy)** — deferred. Q4 (6 playlists) is the upper
  bound on storage pressure; revisit once the 200 Gi PVC is >70% full.
- **Q7 (bgutil in-process config block)** — the validation run uses
  the plugin's default config. If YouTube challenges the cluster's
  egress IP, add a `[yt_dlp_plugins.bgutil]` block to `yt-dlp.conf`
  in `yt-dlp-config.yaml`, rebuild the image, and retry.

## Why this design

- **No sidecar**: `bgutil-ytdlp-pot-provider` runs in-process; the
  `nathanwhyte/bgutil-provider` Deployment + Service from the retired
  setup is gone. The K8s surface shrinks from 2 Deployments + 1
  Service + 2 ConfigMaps to 1 image + 2 ConfigMaps + 6 CronJobs.
- **No copyparty**: direct writes to the 200 Gi PVC. The retired
  setup had a 2-node copyparty Deployment + 600 Gi of media/archive
  PVCs for upload — none of that is needed for personal use.
- **wemby-pinned storage**: `longhorn-hdd-wemby` SC with
  `numberOfReplicas: 2` + `nodeSelector: wemby` gives 2-replica
  redundancy on the wemby HDDs (the retired SC had `replicas: 1` and
  no node pin). The `wemby` Longhorn node tag is bootstrapped once
  via `longhorn-node-wemby-tag.yaml`.
- **RWO, single-Job-at-a-time**: schedules are staggered ~2h10m apart
  so no two CronJobs ever contend for the PVC. If parallel runs are
  ever wanted, switch the PVC to RWX or give each playlist its own.
- **No Deno, no AtomicParsley, no kubectl, no u2c.py**: stripped from
  the image. `yt-dlp[default]` bundles `yt-dlp-ejs` so the JS
  challenge solver works without a JS runtime; without AtomicParsley
  the `--preset-alias mp4` flag is a no-op and is dropped.
