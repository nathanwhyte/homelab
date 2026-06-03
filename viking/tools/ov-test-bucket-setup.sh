#!/usr/bin/env bash
# One-time (idempotent) setup for the openviking-agfs-test S3 bucket in
# the live Garage cluster. Run from a host with kubectl + access to the
# garage namespace — does NOT use a Job in the cluster (the cluster's
# image registry doesn't have a small kubectl image we can pull).
#
# Re-runs are safe: the bucket create step is skipped if the bucket
# already exists, and `garage bucket allow` is idempotent on its
# access-key matchers.
set -euo pipefail

BUCKET="${BUCKET:-openviking-agfs-test}"
KEY_ID="${KEY_ID:-GKe02cc7ed645326937f7e185e}"
GARAGE_POD="${GARAGE_POD:-garage-0}"
GARAGE_NS="${GARAGE_NS:-garage}"

echo "Checking if bucket ${BUCKET} already exists in ${GARAGE_NS}/${GARAGE_POD}..."
LIST=$(kubectl -n "${GARAGE_NS}" exec "${GARAGE_POD}" -c garage -- /garage bucket list 2>&1 || true)
# `garage bucket list` prints rows like:  <id>  <date>  <global-aliases>
# Match by the global-alias column being exactly our bucket name.
if echo "${LIST}" | awk 'NR>1 {print $3}' | grep -qx "${BUCKET}"; then
  echo "  -> bucket ${BUCKET} already present, skipping create"
else
  echo "Creating bucket ${BUCKET}..."
  kubectl -n "${GARAGE_NS}" exec "${GARAGE_POD}" -c garage -- /garage bucket create "${BUCKET}"
fi

echo "Granting key ${KEY_ID} owner on ${BUCKET} (idempotent)..."
kubectl -n "${GARAGE_NS}" exec "${GARAGE_POD}" -c garage -- /garage bucket allow \
  --owner --read --write "${BUCKET}" --key "${KEY_ID}" || \
  echo "  (allow returned non-zero; treating as already-granted)"

echo "Verifying..."
kubectl -n "${GARAGE_NS}" exec "${GARAGE_POD}" -c garage -- /garage bucket list
echo "Bucket ${BUCKET} is ready."
