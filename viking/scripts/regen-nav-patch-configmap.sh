#!/usr/bin/env bash
# Regenerate viking/manifests/openviking-nav-patch-configmap.yaml (prod) from the
# canonical IMPR-1185 sources in viking/manifests/test/nav-patch/. The test stack
# builds its own ConfigMap (ov-nav-patch) from the same two files via its
# kustomization, so prod and test cannot drift as long as this is re-run after any
# edit and the result is committed with it. `--check` exits non-zero when the
# committed manifest is stale.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$DIR/manifests/test/nav-patch"
OUT="$DIR/manifests/openviking-nav-patch-configmap.yaml"

render() {
	kubectl create configmap openviking-nav-patch \
		-n viking \
		--from-file=sitecustomize.py="$SRC/sitecustomize.py" \
		--from-file=ov_nav_patch.py="$SRC/ov_nav_patch.py" \
		--dry-run=client -o yaml
}

if [[ "${1:-}" == "--check" ]]; then
	if diff -q <(render) "$OUT" >/dev/null; then
		echo "openviking-nav-patch ConfigMap is current"
	else
		echo "openviking-nav-patch ConfigMap is STALE: run $0" >&2
		exit 1
	fi
else
	render >"$OUT"
	echo "Regenerated $OUT"
fi
