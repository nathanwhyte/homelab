#!/usr/bin/env bash

# Build and push the custom ARC runner image to Harbor (IDEA-1094).
#
# Runs from the MacBook (arm64), so the build cross-compiles to linux/amd64 —
# all three cluster nodes are amd64. Requires a prior
# `docker login registry.nathanwhyte.dev` with an account that can push to the
# `ci` project (create the project + a robot account in the Harbor UI first;
# see ../../ARC.md).

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

REGISTRY="registry.nathanwhyte.dev"
PROJECT="ci"
IMAGE="actions-runner"
TAG="${1:-$(date +%Y%m%d)}"
REF="$REGISTRY/$PROJECT/$IMAGE:$TAG"
LATEST_REF="$REGISTRY/$PROJECT/$IMAGE:latest"

if ! command -v docker >/dev/null; then
	echo "docker not installed." >&2
	exit 1
fi

echo "Building $REF (linux/amd64)..."
# :latest is what the scale-set values reference (so values stay stable);
# the date tag exists for rollback.
docker buildx build \
	--platform linux/amd64 \
	--push \
	-t "$REF" \
	-t "$LATEST_REF" \
	"$SCRIPT_DIR"

echo -e "\nPushed $REF (+ :latest)"
echo "New runner pods pick it up on next scale-up; no redeploy needed unless the values changed."
