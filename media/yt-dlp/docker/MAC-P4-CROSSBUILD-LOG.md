# Mac/p4 Dockerfile Cross-Build Log

- **Date:** 2026-09-06
- **Task:** TASK-1051 item 4 — cross-build of the dropped-deps list (IDEA-1019)
- **Host:** macOS (arm64, aarch64)
- **Target platform:** linux/amd64 (cluster arch)

## Build command

```bash
docker buildx build --builder=desktop-linux --platform=linux/amd64 \
  -t yt-dlp-crossbuild-test media/yt-dlp/docker/
```

No `--push`; local build only.

## Outcome

**SUCCESS** (exit code 0). The image compiled cleanly for linux/amd64.

## Dropped-deps finding

The IDEA-1019 plan proposed dropping Deno, AtomicParsley, kubectl, u2c.py,
mutagen, pycryptodome, and certifi. The current Dockerfile already reflects
those decisions (Deno re-added; the rest dropped). The cross-build confirms
the dropped-deps list does not break the build:

- **Deno** — KEPT (re-added 2026-06-11). `apk add deno` (2.7.4-r2) installs
  cleanly; required for `yt-dlp-ejs` challenge solving.
- **AtomicParsley** — safely dropped. Not referenced by any build step.
- **kubectl** — safely dropped. Not referenced by any build step.
- **u2c.py** — safely dropped. Not referenced by any build step.
- **mutagen** — safely dropped as an explicit install, BUT note: it is
  re-introduced transitively via `yt-dlp[default]` (pip resolved
  `mutagen-1.48.1`). So it is not actually absent from the image; it is just
  no longer pinned/installed individually.
- **pycryptodome** — same as mutagen: dropped as an explicit install, but
  `yt-dlp[default]` pulls `pycryptodomex-3.23.0` transitively.
- **certifi** — same: dropped explicitly, but `yt-dlp[default]` pulls
  `certifi-2026.7.22` transitively.

Net: the "dropped" Python deps (mutagen, pycryptodome, certifi) are still
present in the image because `yt-dlp[default]` declares them as extras. The
drop only removed the _explicit_ individual `pip install` lines, not the
packages themselves. This is a documentation nuance, not a build failure.

## Build detail

- Base image: `python:3.12-alpine` (sha256:b64631e0...)
- `apk add` resolved 109 packages (ffmpeg 8.1.2-r0, deno 2.7.4-r2, etc.),
  total 254.6 MiB.
- `pip install "yt-dlp[default]" bgutil-ytdlp-pot-provider` succeeded:
  yt-dlp 2026.8.19, yt-dlp-ejs 0.8.0, bgutil-ytdlp-pot-provider 1.3.2.
- Image manifest: `sha256:ebcfa986e678de6e8133782a1029b8db9214d47e156a7aa7b0a0e28c4ec49ff8`
