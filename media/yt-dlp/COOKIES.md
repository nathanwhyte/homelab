# Regenerating `cookies.txt` for yt-dlp

`cookies.txt` is a **Netscape-format** cookie file yt-dlp consumes via
`--cookies /yt-dlp-archive/cookies.txt`. It's only needed for **age-restricted
or member-only** videos — the 6 playlists are public, so the pipeline runs
fine without it (the 2026-06-24 archive reseed confirmed this: 102/102/103/101/15/21
IDs listed with no cookies).

The bootstrap Job's first run deleted the file ([[BUG-1023]]); it has since been
re-staged. This runbook covers **refreshing** it when YouTube rotates the
session cookies and age-restricted downloads start failing.

## Where it lives + how it's read

| Item                       | Value                                                                                                                                      |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| PVC path                   | `/yt-dlp-archive/cookies.txt` (the `media` PVC, RWO, wemby-pinned)                                                                         |
| Read by                    | daily playlist CronJobs + the reseed Job — both pass `--cookies /yt-dlp-archive/cookies.txt` **only when the file exists** (`if [ -f … ]`) |
| Effect of dropping it in   | picked up on the next CronJob run — **no manifest change, no restart**                                                                     |
| R2 backup                  | **excluded** (`--exclude=cookies.txt` in both bootstrap + backup) — never pushed to R2; keep on the PVC only                               |
| Current state (2026-06-24) | present, 898 B, owned `1000:1000`, mode `644`                                                                                              |

## Step 1 — generate the file on the MacBook

You're logged into YouTube in a browser on the MacBook (the account that has
access to any restricted videos). Two options:

### Option A — browser extension (canonical, most reliable)

1. Install a Netscape-format cookies exporter:
   - Chrome/Edge/Brave: **"Get cookies.txt LOCALLY"** (id `cclelndahbckbenkjhflpdbgdldlbecc`)
   - Firefox: **"cookies.txt"**
2. Open the extension, filter to `youtube.com` (include related `.google.com`
   if the exporter offers it), export, save as `cookies.txt`.
3. Verify it's Netscape format:

   ```bash
   head -5 cookies.txt
   ```

   First line must be `# Netscape HTTP Cookie File` (or `# HTTP Cookie File`)
   with tab-separated `domain<TAB>flag<TAB>path<TAB>secure<TAB>expiration<TAB>name<TAB>value`
   rows. If it's JSON, you picked the wrong exporter.

### Option B — `yt-dlp --cookies-from-browser` (no extension)

yt-dlp can pull cookies straight from a local browser profile and write them
out as a Netscape file in one shot:

```bash
# rm first: if cookies.txt already exists yt-dlp may not refresh it (yt-dlp#13863)
rm -f cookies.txt
# Exits code 2 ("You must provide at least one URL") even on success — that's expected.
yt-dlp --cookies-from-browser firefox --cookies cookies.txt 2>/dev/null || true
# Confirm it actually wrote a Netscape file:
head -2 cookies.txt
```

Caveats:

- **Exports cookies for ALL sites** in that browser profile, not just YouTube
  (yt-dlp FAQ). Treat the file as a credential.
- **Firefox is the most reliable** across platforms. Chrome on macOS will
  prompt once for keychain access; Chrome on Windows locks the cookie DB while
  the browser is open (close it first, or use Firefox).
- If the file is empty/non-Netscape, fall back to Option A.

## Step 2 — ship it onto the PVC

Use the **tailnet** kubectl context (you're off-LAN). The `media` PVC is RWO
and must be written from wemby.

### Method 1 — `kubectl cp` into the always-on `yt-dlp-archive-push` pod

`yt-dlp-archive-push` is a long-running Deployment on wemby that mounts the
`media` PVC **read-write** at `/yt-dlp-archive` (verified). It's the simplest
target. Do **not** use the `media-nfs-gateway` pod — that mount is read-only.

```bash
kubectl --context tailnet cp cookies.txt yt-dlp/yt-dlp-archive-push:/yt-dlp-archive/cookies.txt
# Fix ownership/perms to match what the cronjob pods (fsGroup 1000) can read:
kubectl --context tailnet exec -n yt-dlp yt-dlp-archive-push -- \
  sh -c 'chown 1000:1000 /yt-dlp-archive/cookies.txt && chmod 644 /yt-dlp-archive/cookies.txt'
```

(If `chown` errors with `Operation not permitted`, the pod isn't running as
root — use Method 2 instead, which is deterministic about ownership.)

### Method 2 — dedicated staging pod (deterministic ownership)

Spins up a one-shot pod running as `1000:1000` with the PVC mounted RW, pipes
the file in over stdin, then deletes itself. Guarantees `1000:1000` / `644`
without depending on the push pod's user:

```bash
kubectl --context tailnet run yt-dlp-cookie-stage --rm -i --restart=Never \
  --image=busybox \
  --overrides='{"spec":{"nodeName":"wemby","securityContext":{"runAsUser":1000,"fsGroup":1000},"containers":[{"name":"stage","image":"busybox","command":["sh","-c","cat > /yt-dlp-archive/cookies.txt && chmod 644 /yt-dlp-archive/cookies.txt && ls -l /yt-dlp-archive/cookies.txt"],"volumeMounts":[{"name":"media","mountPath":"/yt-dlp-archive"}]}],"volumes":[{"name":"media","persistentVolumeClaim":{"claimName":"media"}}]}}' \
  < cookies.txt
```

## Step 3 — verify

Confirm the file landed and yt-dlp can use it without a login error:

```bash
# File present + correct owner/mode:
kubectl --context tailnet exec -n yt-dlp yt-dlp-archive-push -- ls -l /yt-dlp-archive/cookies.txt

# Dry flat-list one playlist with the cookies (no download). Pick any playlist URL:
kubectl --context tailnet exec -n yt-dlp yt-dlp-archive-push -- \
  yt-dlp --flat-playlist --print id --cookies /yt-dlp-archive/cookies.txt \
  'https://youtube.com/playlist?list=PL8RARzxYAJTSCbN1q27kUcNl2OV_nw1Fb' | head
```

If it prints IDs with no `Sign in to confirm you're not a bot` / login error,
the cookies are good. The next daily CronJob run will pick them up automatically.

## Notes / gotchas

- **Don't commit it.** It's a credential. It's already excluded from the R2
  backup; keep it on the PVC only, never in git.
- **Expiration.** YouTube session cookies (`__Secure-3PSID` / `SID` variants)
  rotate. A regenerated file is good for days-to-weeks, not forever. Re-run
  Step 1 + Step 2 when age-restricted downloads start failing.
- **Only needed for restricted content.** While the playlists stay public,
  cookies are optional. Re-stage only if/when a playlist gains age-restricted
  or member-only videos.
- **YouTube-specific export guide:** <https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies>
  (incognito/fresh-session recommendation for the most reliable YouTube cookies).
