# yt-dlp revival (IDEA-019)

YouTube playlist downloader on the homelab K3s cluster. One custom
Docker image, one 300 Gi PVC on wemby, six daily CronJobs (one enabled,
five suspended until the validation run proves the pipeline).

## Files

| File | Purpose |
|------|---------|
| `docker/Dockerfile` | `nathanwhyte/yt-dlp:2026.10.17-yt-dlp` build |
| `namespace.yaml` | `yt-dlp` namespace |
| `longhorn-node-wemby-tag.yaml` | Patches the wemby Longhorn Node with a `wemby` tag |
| `longhorn-hdd-wemby-storageclass.yaml` | Dedicated SC: HDD, wemby-pinned (volume runs 1 replica) |
| `media-pvc.yaml` | 300 Gi PVC, RWO, on `longhorn-hdd-wemby` |
| `yt-dlp-config.yaml` | `yt-dlp.conf` ConfigMap |
| `yt-dlp-playlists.yaml` | 6 playlist URLs + names + per-playlist extra args |
| `cronjob-validation.yaml` | Enabled CronJob for playlist 2 ("Gotta Keep These Somewhere") |
| `cronjobs.yaml` | 5 suspended CronJobs (playlists 1, 3, 4, 5, 6) |
| `setup.sh` | Idempotent bootstrap (apply in dep order) |
| `operator.sh` | Subcommand helper: `run-job`, `logs`, `archive-list`, etc. |
| `scripts/r2-consolidate.py` | Pre-existing R2 directory consolidation tool (unrelated to yt-dlp) |

## First-time setup

1. **Build the image** (from repo root):

   ```bash
   docker build -t nathanwhyte/yt-dlp:2026.10.17-yt-dlp media/yt-dlp/docker/
   docker push  nathanwhyte/yt-dlp:2026.10.17-yt-dlp
   ```

   The Dockerfile is `python:3.12-alpine` + ffmpeg + deno +
   `yt-dlp[default]` pinned to a release (bundles `yt-dlp-ejs` for JS
   challenge solving). No PO-token provider: the default clients don't
   need one (BUG-1243).

2. **Apply manifests**:

   ```bash
   bash media/yt-dlp/setup.sh
   ```

   This is idempotent. Re-run it after manifest edits to re-converge.

3. **Verify the PVC** binds to a wemby-backed Longhorn volume:

   ```bash
   kubectl get pvc -n yt-dlp media
   # STATUS should be Bound, the VOLUME name maps to a Longhorn
   # volume whose spec.nodeSelector contains "wemby".
   ```

## Validation run

Don't wait for the 02:10 UTC schedule — trigger the validation run
manually:

```bash
kubectl create job --from=cronjob/yt-dlp-playlist-2 -n yt-dlp test-validation
kubectl logs -n yt-dlp -l job-name=test-validation -f
```

Or via the operator script:

```bash
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

```bash
kubectl exec -n yt-dlp <debug-pod> -- tail /yt-dlp-archive/archive-playlist-2.txt
```

(`<debug-pod>` is any pod mounting the `media` PVC, e.g. a one-off
`alpine:3.20` via `kubectl run --rm -it --image=alpine:3.20 ...`.)

## Enabling the other 5 playlists

Once the validation run is clean, pre-seed each playlist's archive file
from the R2 backup of the prior architecture (see the **R2 archive-seeding**
section below), then enable the CronJobs:

```bash
for n in 1 3 4 5 6; do
  media/yt-dlp/operator.sh seed-archive $n --apply
  media/yt-dlp/operator.sh enable $n
done
```

Or flip `suspend: true` → `suspend: false` in `cronjobs.yaml` and
re-apply with `kubectl apply -f media/yt-dlp/cronjobs.yaml`.

## R2 archive-seeding

The CronJob templates use `--download-archive` to skip already-downloaded
videos. For each playlist, the archive file at
`/yt-dlp-archive/archive-playlist-N.txt` is the source of truth for "what
do we have." When bringing up a new playlist, you can pre-seed that
archive file with the IDs of the videos already backed up in R2.

**Where the IDs come from.** The R2 keys under
`backups/cluster/homelab-k3s/volumes/archive/media/YouTube/<name>/` are
named by video title, not by video ID — so the seeder can't recover
them from the R2 listing. The ID list has to come from somewhere else.
The typical source is a one-time `yt-dlp --flat-playlist` extraction:

```bash
# Pull the IDs for one playlist without downloading any video.
yt-dlp --flat-playlist --print id '<playlist_url>' > /tmp/playlist-3.ids
```

`--flat-playlist` is a metadata-only fetch (a few KB, not GB).

Procedure (per playlist):

```bash
# 1. Get the IDs (one-off, a few seconds)
yt-dlp --flat-playlist --print id '<playlist_url>' > /tmp/playlist-3.ids

# 2. Dry-run
media/yt-dlp/operator.sh seed-archive 3 --ids-file /tmp/playlist-3.ids

# 3. Apply (writes the archive file to the media PVC)
media/yt-dlp/operator.sh seed-archive 3 --ids-file /tmp/playlist-3.ids --apply

# 4. Run the corresponding Job once
media/yt-dlp/operator.sh run-job 3

# 5. Tail logs
media/yt-dlp/operator.sh logs
```

For multiple playlists in one shot, lay out files at
`/tmp/yt-dlp-archives/archive-playlist-N.txt` (one per playlist, in yt-dlp
archive format or just bare IDs) and run:

```bash
media/yt-dlp/operator.sh seed-archive --all \
    --ids-dir /tmp/yt-dlp-archives --apply
```

Flags (passed to the underlying `r2-seed-archive.py`):

- `--ids-file <path>` — single playlist, IDs from a file.
- `--ids-stdin` — single playlist, IDs from stdin (pipe `yt-dlp
  --flat-playlist --print id`).
- `--ids-dir <path>` — `--all` mode, reads
  `<path>/archive-playlist-N.txt` per playlist.
- `--check-r2` — also enumerate the R2 prefix and print the file count
  as a sanity check (R2 keys are title-named and don't carry IDs; this
  only confirms the prefix has roughly the expected media count).
- `--apply` — actually write to the PVC. Default is dry-run.
- `--force` — overwrite an existing archive file on the PVC.

The seeder is idempotent: re-running without `--force` is a no-op if the
file already exists. The archive file is staged on the PVC with UID
1000 / GID 1000 / mode 0644, matching the `fsGroup: 1000` on the
CronJob template.

## Operator script

```text
Usage: media/yt-dlp/operator.sh <command> [args]

  run-job <N> [extra-args]   Manually run CronJob N now (N in 1..6)
  list-jobs                  List Jobs and CronJobs in the namespace
  logs [job|pod]             Stream logs (most recent if no arg)
  archive-list               Show per-playlist archive stats
  archive-reset <N>          Wipe archive-playlist-N.txt (re-downloads all)
  seed-archive <N|--all>     Stage an archive-playlist-N.txt on the
                             media PVC from an operator-supplied ID
                             list. Pass --ids-file, --ids-stdin, or
                             --ids-dir to provide the IDs, then
                             --apply to write.
  list-cronjobs              Show schedule + suspend state for all 6
  enable <N>                 Un-suspend CronJob N
  disable <N>                Suspend CronJob N
  download <URL> [args...]   Ad-hoc single-video download
  reap-jobs                  Delete completed/failed Jobs older than 1h
```

## Open questions

- **Q5 (retention policy)** — deferred. Q4 (6 playlists) is the upper
  bound on storage pressure; revisit once the 300 Gi PVC is >70% full.
- **Q7 (PO tokens)** — closed 2026-10-17 (BUG-1243). The bgutil plugin
  was never "in-process": it needs an HTTP server on `:4416` or the
  server's JS on disk, so it never produced a token and was dropped.
  If YouTube starts enforcing PO tokens on the default clients, run the
  `brainicism/bgutil-ytdlp-pot-provider` server as a sidecar and
  reinstall the plugin.

## Why this design

- **No sidecar**: the `nathanwhyte/bgutil-provider` Deployment +
  Service from the retired setup is gone, and no PO-token provider
  replaces it. The K8s surface shrinks from 2 Deployments + 1
  Service + 2 ConfigMaps to 1 image + 2 ConfigMaps + 6 CronJobs.
- **Failing Jobs mean something**: every Job runs yt-dlp through
  `run-yt-dlp.sh` (in `yt-dlp-config.yaml`), which tolerates per-video
  errors and fails on the rest, so a client-wide block like the
  2026-08 android_vr 403s alerts instead of passing as `Completed`.
- **No copyparty**: direct writes to the 300 Gi PVC. The retired
  setup had a 2-node copyparty Deployment + 600 Gi of media/archive
  PVCs for upload — none of that is needed for personal use.
- **wemby-pinned storage**: `longhorn-hdd-wemby` SC with
  `nodeSelector: wemby` keeps the volume on wemby's HDD. It runs
  **1 replica** (wemby has a single HDD disk), so the weekly R2 backup
  is the only other copy. The `wemby` Longhorn node tag is
  bootstrapped once via `longhorn-node-wemby-tag.yaml`.
- **RWO, single-Job-at-a-time**: schedules are staggered ~2h10m apart
  so no two CronJobs ever contend for the PVC. If parallel runs are
  ever wanted, switch the PVC to RWX or give each playlist its own.
- **No Deno, no AtomicParsley, no kubectl, no u2c.py**: stripped from
  the image. `yt-dlp[default]` bundles `yt-dlp-ejs` so the JS
  challenge solver works without a JS runtime; without AtomicParsley
  the `--preset-alias mp4` flag is a no-op and is dropped.
