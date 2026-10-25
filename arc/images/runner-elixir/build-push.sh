#!/usr/bin/env bash

# Build and push the webway ARC runner image (Elixir/OTP + PostgreSQL on top of
# the compendium runner image) to Harbor.
#
# Same mechanics as ../runner/build-push.sh: cross-builds linux/amd64 from the
# MacBook with the Docker Desktop builder and pushes with the keychain login.
# Push ../runner first when its Dockerfile changed — this image builds FROM
# its :latest.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

REGISTRY="registry.nathanwhyte.dev"
PROJECT="ci"
IMAGE="actions-runner-elixir"
TAG="${1:-$(date +%Y%m%d)}"
REF="$REGISTRY/$PROJECT/$IMAGE:$TAG"
LATEST_REF="$REGISTRY/$PROJECT/$IMAGE:latest"

if ! command -v docker >/dev/null; then
	echo "docker not installed." >&2
	exit 1
fi

echo "Building $REF (linux/amd64)..."
docker buildx build \
	--builder "${BUILDX_BUILDER:-desktop-linux}" \
	--platform linux/amd64 \
	--push \
	-t "$REF" \
	-t "$LATEST_REF" \
	"$SCRIPT_DIR"

echo -e "\nPushed $REF (+ :latest)"
echo "New webway runner pods pick it up on next scale-up; no redeploy needed unless the values changed."
