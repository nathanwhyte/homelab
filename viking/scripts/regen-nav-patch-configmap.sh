#!/usr/bin/env bash
# Regenerate viking/manifests/openviking-nav-patch-configmap.yaml (prod) from the
# canonical sources in viking/manifests/test/nav-patch/: sitecustomize.py (the one
# loader) and every ov_*_patch.py beside it (IMPR-1185 nav patch, BUG-1176 extract,
# BUG-1177 ChatLog, BUG-1174 S3 cache, BUG-1180 memory guard, IMPR-1200 event
# abstract, BUG-1189 grep scope, IMPR-1215 recall time, BUG-1218 fetch-items,
# IMPR-1303 WM-update-skip, IMPR-1311 Claude-params and IMPR-1404 preference-gate).
# The file set is that glob (IMPR-1413), so a new patch needs no edit here. The test
# stack builds its own ConfigMap (ov-nav-patch) through its kustomization, which has
# to list the same files; both Deployments mount their ConfigMap whole.
#
# `--check` (a pre-commit hook) runs check-nav-patch-files.py, which renders both
# stacks with `kubectl kustomize` and exits non-zero when what either one mounts
# disagrees with the glob (a stale ConfigMap, a missing kustomization entry, an
# `items:` list or bad mount on a Deployment) or sitecustomize.py's TARGETS does.
# In a git work tree the check counts only staged or committed patch files, so a
# regen over an untracked patch fails until that patch is `git add`ed.
# It compares parsed objects, not bytes, so kubectl's output format does not
# matter. It needs kubectl and a python3 with PyYAML.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$DIR/manifests/test/nav-patch"
OUT="$DIR/manifests/openviking-nav-patch-configmap.yaml"

if [[ "${1:-}" == "--check" ]]; then
	exec python3 "$DIR/scripts/check-nav-patch-files.py" --root "$DIR/.."
fi

shopt -s nullglob
FILES=("$SRC/sitecustomize.py" "$SRC"/ov_*_patch.py)
shopt -u nullglob

args=()
for f in "${FILES[@]}"; do
	args+=("--from-file=$(basename "$f")=$f")
done
kubectl create configmap openviking-nav-patch -n viking "${args[@]}" \
	--dry-run=client -o yaml >"$OUT"
echo "Regenerated $OUT"
