# yt-dlp Dockerfile for the IDEA-019 revival
#
# Image: nathanwhyte/yt-dlp:2026.06.10-yt-dlp
# Base:  python:3.12-alpine
#
# Notable changes from the retired ~/code/repos/k8s/media/yt-dlp/docker/Dockerfile:
#   - KEEP deno: re-added 2026-06-11 after the first validation run
#     showed the "No supported JavaScript runtime could be found" warning.
#     `yt-dlp-ejs` is bundled with `yt-dlp[default]`, but EJS needs an
#     actual JS runtime (Deno, Node, Bun, etc.) to execute the challenge
#     solver. Without one, YouTube extraction still works for most videos
#     but some formats are missing and PO-token-gated videos will fail.
#     The `yt-dlp` wiki's recommended default is Deno; Alpine ships
#     `deno-2.7.x` so it's a one-line apk add.
#   - DROP AtomicParsley: Q3 closed with "drop from image, re-add only if a
#     metadata issue surfaces on a real yt-dlp run." ffmpeg handles the
#     metadata cases we actually use; chapters are intentionally skipped.
#   - DROP kubectl: was only used for PVC cleanup, which is now just
#     `kubectl exec` from the operator workstation.
#   - DROP u2c.py: copyparty upload is gone in the new design (direct PVC
#     writes per the IDEA-019 plan). The new design treats copyparty as an
#     optional read layer, not an upload intermediary.
#   - DROP mutagen / pycryptodome / certifi: optional yt-dlp extras that
#     aren't required for the YouTube + audio extraction path we use.
#   - KEEP bgutil-ytdlp-pot-provider: in-process POT token generation,
#     eliminates the bgutil-provider sidecar from the retired architecture.
#
# Build:
#   docker buildx build --platform=linux/amd64 \
#     -t nathanwhyte/yt-dlp:2026.06.10-yt-dlp \
#     --push media/yt-dlp/docker/
# Mac/p4 cross-build verified 2026-09-06 (linux/amd64, dropped-deps list)
# Push:
#   (handled by --push above; or `docker push` after a plain `docker build`)
FROM python:3.12-alpine

# Install system dependencies
# - ffmpeg: required for merging video/audio and post-processing
# - ca-certificates: for SSL/TLS validation when talking to YouTube / S3
# - deno: JS runtime for yt-dlp-ejs challenge solving. Without it,
#   `yt-dlp[default]` still works but some formats are missing and
#   PO-token-gated videos fail. The EJS README on the yt-dlp wiki
#   recommends Deno as the default; Alpine's `deno` package is
#   ~25 MB and is kept up to date.
RUN apk add --no-cache \
    ffmpeg \
    ca-certificates \
    deno \
    && rm -rf /var/cache/apk/*

# Install yt-dlp and the in-process POT plugin
# - yt-dlp[default]: main package; the `default` extra group bundles
#   `yt-dlp-ejs` (the JavaScript challenge solver), certifi, mutagen,
#   pycryptodome, and websockets — all the things the old Dockerfile
#   installed individually. EJS needs a JS runtime (deno, above).
# - bgutil-ytdlp-pot-provider: featured plugin maintained by a yt-dlp
#   maintainer; runs in-process so the new design drops the bgutil-provider
#   Deployment + ClusterIP Service from the retired architecture.
RUN pip install --no-cache-dir \
    "yt-dlp[default]" \
    bgutil-ytdlp-pot-provider

# Create a non-root user matching the fsGroup on the Pod spec
RUN addgroup -g 1000 ytdlp && \
    adduser -D -u 1000 -G ytdlp ytdlp

# Ensure the per-playlist download dir exists with the right ownership
RUN mkdir -p /downloads /yt-dlp-archive && \
    chmod 775 /downloads /yt-dlp-archive && \
    chown -R ytdlp:ytdlp /downloads /yt-dlp-archive

WORKDIR /app

USER ytdlp

# Default entrypoint is yt-dlp; the CronJob's command overrides the args.
ENTRYPOINT ["yt-dlp"]
