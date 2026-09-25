#!/usr/bin/env bash
# Regenerate viking/manifests/openviking-nav-patch-configmap.yaml (prod) from the
# canonical sources in viking/manifests/test/nav-patch/ (IMPR-1185 nav patch,
# BUG-1176 extract, BUG-1177 ChatLog, BUG-1174 S3 cache, BUG-1180 memory guard and
# IMPR-1200 event abstract patches, one loader).
# The test stack builds its own ConfigMap (ov-nav-patch) from the same seven files via its
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
		--from-file=ov_extract_patch.py="$SRC/ov_extract_patch.py" \
		--from-file=ov_chatlog_patch.py="$SRC/ov_chatlog_patch.py" \
		--from-file=ov_s3_cache_patch.py="$SRC/ov_s3_cache_patch.py" \
		--from-file=ov_memory_guard_patch.py="$SRC/ov_memory_guard_patch.py" \
		--from-file=ov_event_abstract_patch.py="$SRC/ov_event_abstract_patch.py" \
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
