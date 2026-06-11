# yt-dlp Dockerfile for the IDEA-019 revival
#
# Image: nathanwhyte/yt-dlp:2026.06.10-yt-dlp
# Base:  python:3.12-alpine
#
# Notable changes from the retired ~/code/repos/k8s/media/yt-dlp/docker/Dockerfile:
#   - DROP deno: `yt-dlp[default]` bundles `yt-dlp-ejs` (npm-equivalent
#     JavaScript challenge solver), so Deno is no longer needed for EJS.
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
#   docker build -t nathanwhyte/yt-dlp:2026.06.10-yt-dlp media/yt-dlp/docker/
# Push:
#   docker push nathanwhyte/yt-dlp:2026.06.10-yt-dlp
FROM python:3.12-alpine

# Install system dependencies
# - ffmpeg: required for merging video/audio and post-processing
# - ca-certificates: for SSL/TLS validation when talking to YouTube / S3
RUN apk add --no-cache \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/cache/apk/*

# Install yt-dlp and the in-process POT plugin
# - yt-dlp[default]: main package; the `default` extra group bundles
#   `yt-dlp-ejs` (the JavaScript challenge solver), certifi, mutagen,
#   pycryptodome, and websockets — all the things the old Dockerfile
#   installed individually.
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
