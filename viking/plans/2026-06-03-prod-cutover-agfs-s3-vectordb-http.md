# Production cutover: `agfs:s3` + `vectordb:http` together

**Date:** 2026-06-03
**Status:** proposed (validation complete, prod cutover planned)
**Author:** Claude
**Prerequisite:** the 2026-06-03 non-prod validation in `viking/docs/2026-06-03-ov-test-agfs-s3-vectordb-http.md` (commit `ba59efd`)

## Overview

Cut the production `openviking` Deployment over from
`agfs.backend: local` + `vectordb.backend: local` to
`agfs.backend: s3` (Garage bucket `openviking-agfs`) + `vectordb.backend: http`
(against a now-running `ov-vectordb` Deployment serving the `context`
collection). Both changes are applied as a single config cutover; the
existing 93Mi of indexed content is **wiped and not re-ingested as part
of this plan** — re-sync from the source corpora is a follow-up task.

After the cutover, the production openviking has the same
`agfs:s3`+`vectordb:http` layout that the non-prod test validated:
sibling writes no longer serialize on the AGFS subtree lock, and the
hot-path bottleneck moves to the VLM/embedding concurrency (which is
still `max_concurrent: 1` in this cutover; the concurrency bump is
a follow-up).

## Current state (verified 2026-06-03 17:12 CDT)

| Component                                                                         | State                                                                                                                                    | Notes                                                                                                                                                         |
| --------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `openviking` Deployment                                                           | 1/1 on timmy, healthy, 24d uptime                                                                                                        | Single-instance, Recreate strategy                                                                                                                            |
| `openviking-standalone-config`                                                    | `agfs: local`, `vectordb: local`, `server.workers: 1`, `vlm.max_concurrent: 1`, `embedding.max_concurrent: 1`                            | The config the Deployment uses                                                                                                                                |
| `openviking-config`                                                               | `agfs: s3` (Garage `openviking-agfs`), `vectordb: local`                                                                                 | The experimental config; was used for the 2026-06-03 partial cutover. Not referenced by the active Deployment.                                                |
| `openviking-data` PVC                                                             | 10Gi, 93Mi used, contains `viking/default/resources/{compendium,bugs,homelab,dipdash,dotfiles,personal-compendium}` and `viking/_system` | Longhorn SSD, single replica                                                                                                                                  |
| `ov-vectordb` Deployment                                                          | 0/0 (scaled to 0 after 2026-06-03 rollback)                                                                                              | Image `ghcr.io/volcengine/openviking:v0.3.14`, command `python -m openviking.storage.vectordb.service.server_fastapi`, `VIKINGDB_PERSIST_PATH=/data/vikingdb` |
| `ov-vectordb-data` PVC                                                            | 10Gi, bound                                                                                                                              | Was empty after the 2026-06-03 cleanup                                                                                                                        |
| `openviking-s3-credentials` secret                                                | exists, owner: openviking Deployment                                                                                                     | Required by the config-rewrite init container when `agfs.backend: s3`                                                                                         |
| `openviking-agfs` S3 bucket                                                       | exists, ID `10b909dc265b2913`, created 2026-05-07                                                                                        | Used by the experimental config; empty on the cutover path because we're wiping everything                                                                    |
| Test stack (`openviking-test`, `ov-vectordb-test`, `openviking-agfs-test` bucket) | all running (68min uptime)                                                                                                               | To be torn down in Phase 0                                                                                                                                    |
| `viking/tools/index-homelab.py` and `viking/tools/index-projects.py`              | `temp_path` → `temp_file_id` already fixed in commit `ba59efd`                                                                           | Will be used by the follow-up sync, not this plan                                                                                                             |
| Production search                                                                 | working (10 results for "GPU fan control")                                                                                               | Goes offline during the cutover window                                                                                                                        |

## Desired end state

After this plan:

1. `openviking` Deployment uses `agfs.backend: s3` against
   `openviking-agfs` and `vectordb.backend: http` against
   `ov-vectordb:5000` (collection `context`).
2. `ov-vectordb` Deployment is at 1/1, healthy, serving the
   `context` collection backed by its own 10Gi PVC.
3. The 93Mi of previously-indexed content is **gone** (intentionally).
   Search returns 0 results for any query. The 5 top-level resource
   directories in the old PVC (`compendium`, `bugs`, `homelab`,
   `dipdash`, `dotfiles`, `personal-compendium`) do not yet exist
   in the new S3 bucket.
4. `/ready` reports `{"agfs":"ok","vectordb":"ok","api_key_manager":"ok","vlm":"ok"}`.
5. A `temp_upload` + commit of one test resource round-trips through
   the S3 AGFS bucket and the ov-vectordb HTTP service. The resource
   is findable via `/api/v1/search/search` after the embedding
   pipeline runs (about 30-45s for the VLM, plus embed time).
6. The test stack is gone (`openviking-test`, `ov-vectordb-test`,
   `openviking-agfs-test` bucket all deleted).
7. `CLAUDE.md` is updated to reflect `ov-vectordb` at 1/1, prod
   `agfs: s3`, prod `vectordb: http`.

Verification commands (run after the plan completes):

```bash
# 1. Deployments healthy
kubectl -n viking get deploy openviking ov-vectordb
#   openviking    1/1   1            1
#   ov-vectordb   1/1   1            1

# 2. /ready 4/4 ok (via ingress)
curl -fsS -u 'noot:...' https://context.nathanwhyte.dev/api/v1/ready
#   {"checks":{"agfs":"ok","vectordb":"ok","api_key_manager":"ok","vlm":"ok"},...}

# 3. Active config has the new backends
kubectl -n viking get configmap openviking-standalone-config -o jsonpath='{.data.ov\.conf}' \
  | python3 -c 'import json,sys; c=json.load(sys.stdin); print("agfs:",c["storage"]["agfs"]["backend"],"vectordb:",c["storage"]["vectordb"]["backend"])'
#   agfs: s3 vectordb: http

# 4. S3 bucket is being written to
kubectl -n garage exec garage-0 -c garage -- /garage bucket list | grep openviking-agfs
#   ... openviking-agfs       (size > 0 after a test write)

# 5. Test stack is gone
kubectl -n viking get deploy openviking-test ov-vectordb-test
#   Error from server (NotFound): deployments.apps "openviking-test" not found

# 6. Rollback path is documented and tested (see Phase 5)
```

## What we're NOT doing

- **Re-ingesting the existing 93Mi.** This plan wipes the prod
  content. A separate "compendium sync" plan will re-populate the
  knowledge base from the source corpora. Until that runs, search
  returns nothing.
- **Bumping `vlm.max_concurrent` or `embedding.max_concurrent` from 1.** The validation ran at `max_concurrent: 1` and the
  cutover stays at those values. The concurrency bump is a follow-up
  that needs separate load-testing against the embedder on manu
  (CUDA, 8 parallel slots) and the ROCm VLM on timmy.
- **Scaling up the worker StatefulSet or coordinator.** `ov-worker`
  and `ov-coordinator` stay at 0/0. Multi-replica indexing
  is downstream of "single-instance parallel ingest working" which
  the bump-concurrency follow-up gates.
- **Touching the experimental `openviking-config` ConfigMap.** It
  remains in the cluster as the historical record of the 2026-06-03
  partial cutover. The active Deployment references
  `openviking-standalone-config`.
- **Touching Garage or its S3 credentials.** Both are unchanged.
- **Touching `embedder-llamacpp` (manu CUDA) or `llamacpp-rocm-llm`
  (timmy).** These are the existing inference endpoints that the
  cutover's openviking config will keep using.
- **Touching the ingress (`openviking-basicauth`,
  `openviking-mcp-ingress`).** The cutover is at the application
  layer; the public URL stays the same.

## Implementation approach

Five phases, executed in order, with a manual pause between each so
the operator can confirm before proceeding. Each phase is **idempotent
or trivially recoverable** — if any phase fails, rollback is the
inverse of that phase alone (we'll verify rollback at the end of
Phase 5, not by failing and recovering).

The cutover is **not atomic** end-to-end: there is a window (Phase 2
through Phase 4) where the openviking pod is restarting with the new
config while the old data has been wiped. The user-visible
consequence is: search returns 0 results, ingress returns 503, for
~2-3 minutes. This is acceptable because the user has chosen to
re-ingest in a separate follow-up.

## Phase 1: Tear down the test stack

**Goal:** remove the validation artifacts so the cluster state going
into Phase 2 is unambiguous.

The test stack uses distinct names (`openviking-test`,
`ov-vectordb-test`), distinct PVCs (`openviking-test-data`,
`ov-vectordb-test-data`), and a distinct S3 bucket
(`openviking-agfs-test`). It cannot collide with the production
resources, but leaving it running creates two ambiguities for the
next operator: (a) the test bucket's data is still hot; (b) the test
configmap could be mistakenly applied if someone `kubectl apply`s
the test kustomize bundle again.

### Changes required

#### 1.1 Delete the test kustomize bundle

**File:** (cluster op, no file change)
**Action:** `kubectl delete -k viking/manifests/test/`

This removes:

- `Deployment/openviking-test`
- `Service/openviking-test`
- `PVC/openviking-test-data`
- `Deployment/ov-vectordb-test`
- `Service/ov-vectordb-test`
- `PVC/ov-vectordb-test-data`
- `ConfigMap/openviking-test-config`

The cluster's image registry can't pull a small kubectl image, so
the test stack has no TTL sidecar — this `kubectl delete` is the
intended teardown path (also documented in
`viking/manifests/test/kustomization.yaml`).

#### 1.2 Delete the test S3 bucket

**File:** (cluster op, no file change)
**Action:** `kubectl -n garage exec garage-0 -c garage -- /garage bucket delete openviking-agfs-test`

The test bucket is empty at this point (the validation wrote 20
docs total, all under `viking://resources/test/...`, totaling well
under 1Mi). Deletion is safe; we don't need a recursive delete.
The same call also removes the access-key grant (Garage deletes the
key policy atomically with the bucket).

#### 1.3 Verify

```bash
kubectl -n viking get deploy openviking-test ov-vectordb-test
#   Error from server (NotFound)
kubectl -n viking get pvc openviking-test-data ov-vectordb-test-data
#   Error from server (NotFound)
kubectl -n garage exec garage-0 -c garage -- /garage bucket list | grep test
#   (no output)
```

### Success criteria

#### Automated

- [x] `kubectl delete -k viking/manifests/test/` exits 0
- [x] All four test resources (deploy, svc, pvc) for both test pods are gone
- [x] `openviking-agfs-test` bucket is gone from `garage bucket list`

#### Manual

- [x] `kubectl get all -n viking` shows only the production resources
      (openviking, ov-vectordb at 0, the inference endpoints, the
      ingress, and the experimental coordinator/worker/merge at 0/0)
- [x] No `openviking-test*` resources anywhere in the cluster

**Pause here.** Confirm the test stack is gone before moving to Phase 2.

---

## Phase 2: Edit the production ConfigMap to use the new backends

**Goal:** stage the new `agfs:s3`+`vectordb:http` config in
`openviking-standalone-config` so the cutover in Phase 3 is a single
`kubectl apply` + `kubectl delete pod`.

### Changes required

#### 2.1 Edit `viking/manifests/openviking-standalone-configmap.yaml`

**File:** `viking/manifests/openviking-standalone-configmap.yaml`
**Changes:** three edits in the `ov.conf` JSON

| Field                        | Before                   | After                                                                                                         |
| ---------------------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------- |
| `storage.agfs.backend`       | `"local"`                | `"s3"`                                                                                                        |
| `storage.agfs`               | `{ "backend": "local" }` | full S3 block (see below)                                                                                     |
| `storage.vectordb.backend`   | `"local"`                | `"http"`                                                                                                      |
| `storage.vectordb.url`       | (absent)                 | `"http://ov-vectordb.viking.svc.cluster.local:5000"`                                                          |
| `storage.vectordb.name`      | `"context"`              | unchanged (the test configmap used `"context-test"` to avoid colliding; prod gets the real collection name)   |
| `storage.vectordb.dimension` | `768`                    | unchanged                                                                                                     |
| `storage.vectordb` (rest)    | (no other fields)        | (no other fields; the `http` adapter does not need `host`/`port`; it only needs `url` per the v0.3.14 schema) |

The `storage.agfs.s3` block is identical to what
`viking/manifests/openviking-configmap.yaml` already has:

```json
"agfs": {
  "backend": "s3",
  "s3": {
    "bucket": "openviking-agfs",
    "endpoint": "http://garage.garage.svc.cluster.local:3900",
    "region": "garage",
    "access_key": "${S3_ACCESS_KEY}",
    "secret_key": "${S3_SECRET_KEY}",
    "use_path_style": true
  }
}
```

The `${S3_ACCESS_KEY}` / `${S3_SECRET_KEY}` placeholders are
populated by the `config-rewrite` init container in
`viking/manifests/openviking-deployment.yaml:38-46` when
`agfs.backend == "s3"`. That init container is already
backend-agnostic and needs no change.

#### 2.2 Apply the ConfigMap

**File:** (cluster op)
**Action:** `kubectl apply -f viking/manifests/openviking-standalone-configmap.yaml`

**Do not delete the pod yet.** The ConfigMap is mounted as
`config-template`; the openviking pod's
`config-out` emptyDir is rendered at pod startup by the
`config-rewrite` init container. The running pod is still using
the in-memory copy of the old config; it won't pick up the change
until the pod restarts.

This is intentional: it gives us a moment to **verify the
ConfigMap syntax is valid** before restarting the pod.

#### 2.3 Verify the ConfigMap

```bash
kubectl -n viking get configmap openviking-standalone-config -o jsonpath='{.data.ov\.conf}' \
  | python3 -c '
import json, sys
c = json.load(sys.stdin)
print("agfs.backend:", c["storage"]["agfs"]["backend"])
print("agfs.s3.bucket:", c["storage"]["agfs"]["s3"]["bucket"])
print("vectordb.backend:", c["storage"]["vectordb"]["backend"])
print("vectordb.url:", c["storage"]["vectordb"]["url"])
print("vectordb.name:", c["storage"]["vectordb"]["name"])
'
#   agfs.backend: s3
#   agfs.s3.bucket: openviking-agfs
#   vectordb.backend: http
#   vectordb.url: http://ov-vectordb.viking.svc.cluster.local:5000
#   vectordb.name: context
```

If pydantic v2 validation ever rejects the new config, it would
fail at the `config-rewrite` init container (i.e. the pod would
CrashLoopBackOff). The pod is still running with the old config;
we catch the bad config only after Phase 3.

### Success criteria

#### Automated

- [ ] `python3 -c 'import json; json.load(open(...))'` parses the
      ConfigMap JSON
- [ ] `kubectl apply -f viking/manifests/openviking-standalone-configmap.yaml` exits 0
- [ ] The `kubectl get configmap ... -o jsonpath` probe above prints
      the new values
- [ ] The `openviking` pod is still 1/1, ready, and serving search
      (the running pod has the old config still)

#### Manual

- [ ] The diff in `git diff viking/manifests/openviking-standalone-configmap.yaml`
      is exactly the three edits above (no accidental field renames)
- [ ] No other file changed (`git status` shows only this ConfigMap)

**Pause here.** The ConfigMap is staged; nothing has restarted yet.

---

## Phase 3: Wipe prod data and restart openviking

**Goal:** with the new config staged, drain the old data and
restart the openviking pod so it picks up the new backends against
empty S3 + empty vectordb.

The wipe happens **before** the restart so the new openviking
process doesn't try to import the old local AGFS tree (it
wouldn't, but emptying first is cleaner than letting the process
discover an empty AGFS and decide what to do).

### Changes required

#### 3.1 Wipe the openviking-data PVC's AGFS content

**File:** (cluster op, no file change)
**Action:** `kubectl -n viking exec deploy/openviking -- sh -c 'rm -rf /app/data/viking /app/data/temp /app/data/bot /app/data/.openviking.pid; find /app/data -name ".path.ovlock" -delete 2>/dev/null || true; find /app/data -name "LOCK" -delete 2>/dev/null || true'`

This removes:

- `/app/data/viking/` — the AGFS content tree (resources, _system,
  temp, session, user, agent). 93Mi total.
- `/app/data/temp/` — leftover temp upload directory.
- `/app/data/bot/` — bot state, not used in this cluster but in
  the same path.
- All stale path locks.

It does **not** remove the PVC itself, so the Longhorn volume
retains its identity and any snapshot/replication policy. The next
openviking pod that mounts this PVC will see an empty
`/app/data/viking` directory.

The `/app/data/vectordb/` path is also empty at this point (prod
was on `vectordb: local` until 2026-06-03, then we wiped the local
store as part of the rollback).

This is **destructive** — it cannot be undone without restoring
from a Longhorn snapshot or re-ingesting. Per the user's decision,
this is the intended state. The cutover is the wipe; re-ingest
is the follow-up.

#### 3.2 Scale up `ov-vectordb`

**File:** (cluster op, no file change)
**Action:** `kubectl -n viking scale deploy ov-vectordb --replicas=1`

The Deployment has been at 0/0 since the 2026-06-03 rollback. The
manifest is unchanged; the only thing that needs to happen is
`replicas=1`. The image and command are already correct
(`ghcr.io/volcengine/openviking:v0.3.14`,
`python -m openviking.storage.vectordb.service.server_fastapi`,
`VIKINGDB_PERSIST_PATH=/data/vikingdb`).

Wait for the pod to be ready:

```bash
kubectl -n viking wait --for=condition=ready pod -l app=ov-vectordb --timeout=120s
```

The `ov-vectordb` PVC was already bound and empty; no PVC
recreation is needed. The `context` collection will be created on
first write.

#### 3.3 Restart the openviking pod

**File:** (cluster op, no file change)
**Action:** `kubectl -n viking delete pod -l app=openviking`

The Deployment has `strategy: Recreate`, so the rolling-update
mechanism is disabled. The way to pick up a new ConfigMap is to
delete the pod. The new pod will:

1. Run `config-rewrite` init container with the new template,
   injecting `${S3_ACCESS_KEY}` and `${S3_SECRET_KEY}` from the
   secret into the rendered `ov.conf`.
2. Run `temp-cleanup` init container (idempotent on an empty PVC).
3. Run `patch-syncdiff` init container (idempotent; the
   `semantic_processor.py` regex match either applies or skips).
4. Start the openviking container with the new config.

The `lock-cleanup` sidecar starts alongside the main container and
will be present in the new pod.

#### 3.4 Wait for ready

```bash
kubectl -n viking wait --for=condition=ready pod -l app=openviking --timeout=180s
```

Then probe `/ready` directly (port-forward or via ingress):

```bash
kubectl -n viking port-forward svc/openviking 1933:1933 &
PF_PID=$!
trap "kill $PF_PID 2>/dev/null || true" EXIT
sleep 1
curl -fsS http://127.0.0.1:1933/api/v1/ready
#   {"checks":{"agfs":"ok","vectordb":"ok","api_key_manager":"ok","vlm":"ok"}}
```

If `/ready` reports anything other than the 4 expected subsystems
as `ok`, the cutover has failed. Jump to Phase 5 rollback
(`# Rollback from a failed cutover`).

### Success criteria

#### Automated

- [ ] `kubectl -n viking get deploy` shows `openviking 1/1` and
      `ov-vectordb 1/1`
- [ ] `kubectl -n viking wait --for=condition=ready pod -l app=openviking`
      exits 0
- [ ] `kubectl -n viking wait --for=condition=ready pod -l app=ov-vectordb`
      exits 0
- [ ] `curl /api/v1/ready` returns 200 with all 4 checks = `ok`

#### Manual

- [ ] The openviking pod log shows `mode=s3` for the AGFS plugin
      and `mode=http` for the vectordb backend (proves the
      config-rewrite worked)
- [ ] No pydantic validation errors in the openviking pod log
      (would indicate the ConfigMap schema is wrong)
- [ ] No `Failed to acquire` lock messages in the first 60s of the
      new pod's log (would indicate the AGFS connection failed)
- [ ] `/health` returns 200 via the public ingress
      `https://context.nathanwhyte.dev/health`

**Pause here.** The new openviking is up against the new backends
and ready to receive writes. Do not proceed to Phase 4 if any of
the 4 readiness checks is not `ok`.

---

## Phase 4: Smoke-test the new stack end-to-end

**Goal:** prove that a write through the new openviking lands in
both the S3 bucket and the ov-vectordb's `context` collection,
and that search returns it.

This is the analogue of step 4-6 in the validation driver
(`viking/tools/ov-test-validate.sh`), but as a one-shot curl
sequence rather than a script.

### Changes required

#### 4.1 Probe write

**File:** (cluster op)
**Action:** a `temp_upload` + commit round-trip, then a polled
search loop (up to 5 minutes) for the doc to become searchable

The VLM is `max_concurrent: 1`, so each doc takes ~30-45s to
summarize. A single 60-second wait is usually enough but can fail
if anything else is in the queue. The polled loop matches the
validation driver's behavior (`viking/tools/ov-test-validate.sh`
step 6).

```bash
KEY=$(kubectl -n viking get secret openviking-api-key -o jsonpath='{.data.api-key}' | base64 -d)
URL=https://context.nathanwhyte.dev

# Step 1: temp_upload
TMP=$(mktemp); echo "post-cutover smoke test $(date -Iseconds)" > "$TMP"
TFID=$(curl -fsS -X POST \
  -H "X-API-Key: $KEY" -H "X-OpenViking-Account: default" -H "X-OpenViking-User: noot" \
  -F "file=@${TMP};filename=cutover-smoke.txt;type=text/plain" \
  "$URL/api/v1/resources/temp_upload" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["temp_file_id"])')
rm -f "$TMP"

# Step 2: commit
URI="viking://resources/cutover-smoke/$(date +%s)"
curl -fsS -X POST \
  -H "X-API-Key: $KEY" -H "X-OpenViking-Account: default" -H "X-OpenViking-User: noot" \
  -H "Content-Type: application/json" \
  -d "{\"temp_file_id\":\"$TFID\",\"to\":\"$URI\"}" \
  "$URL/api/v1/resources"
#   {"status":"ok","result":{"status":"success",...}}

# Step 3: verify S3 bucket now has the file
kubectl -n garage exec garage-0 -c garage -- /garage bucket list
#   openviking-agfs   (size > 0)

# Step 4: poll for the doc to become searchable.
# VLM is max_concurrent=1; one small doc vectorizes in ~30-45s.
# The poll runs up to 5 minutes, every 10s. Exits 0 on hit, 1 on timeout.
deadline=$(( $(date +%s) + 300 ))
total=0
while (( $(date +%s) < deadline )); do
  total=$(curl -fsS -X POST \
    -H "X-API-Key: $KEY" -H "X-OpenViking-Account: default" -H "X-OpenViking-User: noot" \
    -H "Content-Type: application/json" \
    -d '{"query":"post-cutover smoke test","limit":5}' \
    "$URL/api/v1/search/search" \
    | python3 -c 'import json,sys
try: print(json.load(sys.stdin)["result"]["total"])
except Exception: print(0)')
  if (( total >= 1 )); then
    echo "smoke doc is searchable after $(( $(date +%s) - (deadline - 300) ))s (total=$total)"
    break
  fi
  sleep 10
done
if (( total < 1 )); then
  echo "FAIL: smoke doc did not become searchable within 5 minutes"
  echo "  Check openviking pod log for the smoke URI's vectorize status"
  exit 1
fi
```

If the doc never becomes searchable after 5 minutes, the
openviking log will tell us whether the doc is in the semantic
queue, whether the VLM call completed, and whether the embedding
was sent to ov-vectordb. The most likely cause is an
ov-vectordb connection issue, which would show up as a
`vectordb: unreachable` warning at log time of the vectorize
call (not at startup — the `vectordb: ok` at startup is
self-reported by the adapter; the actual write path checks again).

#### 4.2 Verify the S3 file landed

**File:** (cluster op, no file change)
**Action:** confirm the AGFS file is in the S3 bucket

The easiest check: list objects in the `openviking-agfs` bucket
via the S3 API. The Garage pod has the `garage` CLI but it doesn't
list object keys — only bucket names. We need an `s3cmd` or direct
S3 API call.

```bash
# Use the openviking-s3-credentials secret to issue an S3 list
KEY_ID=$(kubectl -n viking get secret openviking-s3-credentials -o jsonpath='{.data.AWS_ACCESS_KEY_ID}' | base64 -d)
SECRET=$(kubectl -n viking get secret openviking-s3-credentials -o jsonpath='{.data.AWS_SECRET_ACCESS_KEY}' | base64 -d)
kubectl -n viking run s3-probe --rm -i --restart=Never --image=python:3.12-slim --quiet -- sh -c "
pip install --quiet boto3 && python3 -c '
import boto3
s3 = boto3.client(\"s3\", endpoint_url=\"http://garage.garage.svc.cluster.local:3900\",
                  aws_access_key_id=\"$KEY_ID\", aws_secret_access_key=\"$SECRET\",
                  region_name=\"garage\")
r = s3.list_objects_v2(Bucket=\"openviking-agfs\", Prefix=\"viking/default/resources/cutover-smoke/\")
print(\"objects:\", r.get(\"KeyCount\"))
for o in r.get(\"Contents\", []):
    print(\"  \", o[\"Key\"], o[\"Size\"], \"bytes\")
'"
```

This lists the keys under `viking/default/resources/cutover-smoke/`
(where the `temp_upload` + commit landed them). Expected: at least
the original text file and the L0/L1 metadata files.

#### 4.3 Verify the vectordb stored the embedding

**File:** (cluster op, no file change)
**Action:** port-forward to `ov-vectordb` and query

```bash
kubectl -n viking port-forward svc/ov-vectordb 5000:5000 &
PF_PID=$!
trap "kill $PF_PID 2>/dev/null || true" EXIT
sleep 1
curl -fsS http://127.0.0.1:5000/health
#   {"status":"ok"}

# Search via the openviking server (it proxies to vectordb through
# the http adapter; this is the user-facing path).
```

### Success criteria

#### Automated

- [ ] `temp_upload` returns 200 with a `temp_file_id`
- [ ] `commit` returns 200 with `status: success`
- [ ] `s3.list_objects_v2` for `openviking-agfs` shows at least
      one object under `viking/default/resources/cutover-smoke/`
- [ ] `ov-vectordb /health` returns 200
- [ ] The 5-minute search poll exits 0 (smoke doc is findable)

#### Manual

- [ ] The smoke-test URI is findable via the public ingress
      `https://context.nathanwhyte.dev/api/v1/search/search`
- [ ] No `resource is busy` rejections in the openviking pod log
      during the write (would indicate the AGFS lock contention
      hasn't been eliminated)
- [ ] No pydantic v2 validation errors in the openviking pod log

**Pause here.** The cutover is verified end-to-end. Don't proceed
to Phase 5 until the search returns the smoke-test doc.

---

## Phase 5: Rollback verification and follow-up documentation

**Goal:** the cutover is committed, the rollback path is
documented and tested, and the follow-up tasks are clearly
recorded.

### Changes required

#### 5.1 Document the rollback path

**File:** `viking/docs/2026-06-03-ov-cutover-rollback.md` (new)
**Changes:** write a 1-page document titled "OpenViking
agfs:s3 + vectordb:http production cutover — rollback procedure"

The document describes (in this order):

1. **Why rollback is required** — cutover cut search to 0 results,
   and the sync plan hasn't been executed yet. If the cluster
   needs to serve existing traffic before the sync runs, rollback
   the cutover and re-ingest later.

2. **Rollback steps** (the inverse of the cutover, in order):

   a. Edit `viking/manifests/openviking-standalone-configmap.yaml`:
   - `storage.agfs.backend: "s3"` → `"local"`
   - Delete the `storage.agfs.s3` block
   - `storage.vectordb.backend: "http"` → `"local"`
   - Delete `storage.vectordb.url`
     b. `kubectl apply -f viking/manifests/openviking-standalone-configmap.yaml`
     c. `kubectl -n viking scale deploy ov-vectordb --replicas=0`
     d. `kubectl -n viking delete pod -l app=openviking`
     e. Wait for ready, probe `/ready`. (The openviking pod will
     come back with an empty local AGFS — exactly the same state
     as if the cutover had been a clean wipe of `local`, which
     the old data was anyway at 2026-06-03.)

3. **What rollback does NOT recover** — the 93Mi of pre-cutover
   content. That data was destroyed in Phase 3 step 3.1. The
   rollback only restores the pre-cutover configuration; it
   cannot restore the data. The data is recoverable only by:
   - Re-running the reindex/sync plan (the follow-up), or
   - Restoring the `openviking-data` PVC from a Longhorn snapshot
     taken before Phase 3 step 3.1.

4. **Snapshot script** (one-liner, idempotent, safe to run any
   time before Phase 3.1) — create a Longhorn snapshot of the
   `openviking-data` PVC and tag it `pre-s3-cutover-2026-06-03`.
   Provide the exact command.

#### 5.2 Test the rollback path (in a safe, recoverable way)

**File:** (cluster op, no file change)
**Action:** simulate a rollback by **just changing the
ConfigMap**, then **reverting**. The actual data wipe in Phase 3
was destructive; we don't re-do the cutover. The test is: do the
ConfigMap swap, see that openviking can read it, then swap back.

```bash
# Save the current (post-cutover) ConfigMap
kubectl -n viking get configmap openviking-standalone-config -o yaml > /tmp/post-cutover-cm.yaml

# Swap to the pre-cutover shape
cat <<'EOF' | kubectl -n viking apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: openviking-standalone-config
  namespace: viking
data:
  ov.conf: |
    {
      "default_account": "default",
      "default_user": "noot",
      "storage": {
        "workspace": "/app/data",
        "transaction": { "lock_timeout": 30.0, "lock_expire": 300.0 },
        "vectordb": { "backend": "local", "name": "context", "dimension": 768 },
        "agfs": { "backend": "local" }
      },
      "code": { "code_summary_mode": "ast" },
      "rerank": { "threshold": 0.2 },
      "server": { "host": "0.0.0.0", "port": 1933, "workers": 1, "root_api_key": "${API_KEY}" },
      "log": { "level": "INFO", "output": "stdout" },
      "embedding": {
        "dense": { "provider": "openai", "model": "nomic-embed-text-v1.5",
                   "api_key": "sk-no-key-required",
                   "api_base": "http://embedder-llamacpp.viking.svc.cluster.local:8080/v1",
                   "dimension": 768, "batch_size": 128 },
        "max_concurrent": 1
      },
      "vlm": {
        "provider": "litellm", "model": "openai/current.gguf",
        "api_key": "sk-no-key-required",
        "api_base": "http://llamacpp-rocm-llm.viking.svc.cluster.local/v1",
        "max_concurrent": 1
      }
    }
EOF

# Wait for the new config to be staged (pod doesn't restart)
kubectl -n viking get configmap openviking-standalone-config -o jsonpath='{.data.ov\.conf}' \
  | python3 -c 'import json,sys; c=json.load(sys.stdin); print("agfs:",c["storage"]["agfs"]["backend"],"vectordb:",c["storage"]["vectordb"]["backend"])'
#   agfs: local vectordb: local

# Revert (the openviking pod is still running with the new s3/http
# config in its in-memory copy; we don't restart it)
kubectl apply -f /tmp/post-cutover-cm.yaml

# Verify
kubectl -n viking get configmap openviking-standalone-config -o jsonpath='{.data.ov\.conf}' \
  | python3 -c 'import json,sys; c=json.load(sys.stdin); print("agfs:",c["storage"]["agfs"]["backend"],"vectordb:",c["storage"]["vectordb"]["backend"])'
#   agfs: s3 vectordb: http
```

The point of this exercise: prove that the ConfigMap is the only
artifact that needs to change to flip between configurations, and
that the `config-rewrite` init container will produce a valid
config in either direction. We do NOT restart the pod during this
test (the running pod still has the post-cutover in-memory config,
which is what we want — verifying the next pod restart will be
fine is a separate concern handled by the rest of the test plan).

#### 5.3 Commit the changes

```bash
git add viking/manifests/openviking-standalone-configmap.yaml \
        viking/docs/2026-06-03-ov-cutover-rollback.md
git commit -m "..."
```

The commit message describes the cutover + the rollback doc.

#### 5.4 Update CLAUDE.md

**File:** `CLAUDE.md` (project root)
**Changes:**

- The `ov-vectordb` row in the "Service routing" table currently
  says "**scaled to 0**". Update to "1/1, http vector service
  backing openviking".
- The note above the table about "Parallel-writer migration note
  (2026-06-03, rolled back)" should be replaced with a new
  note: "Production openviking now runs on `agfs:s3` +
  `vectordb:http` (cutover 2026-06-03, commit pending). Content
  was wiped at cutover and will be re-ingested via the separate
  sync plan. See `viking/docs/2026-06-03-ov-cutover-rollback.md`
  for the rollback path."
- Update the `ov-vectordb` row's "Image" field — it was
  `ghcr.io/volcengine/openviking:v0.3.14` already, no change.

### Success criteria

#### Automated

- [ ] `git diff` shows only the intended files
- [ ] `git status` is clean
- [ ] The committed ConfigMap matches the post-cutover state

#### Manual

- [ ] `https://context.nathanwhyte.dev/api/v1/ready` returns
      4/4 ok
- [ ] A search for the smoke-test URI returns the doc
- [ ] The rollback doc is in `viking/docs/` and describes steps
      in the correct order
- [ ] The rollback doc lists the Longhorn snapshot command and
      warns that data wipe is destructive

**This is the end of the plan.** The cutover is complete,
documented, and verifiable.

---

## Open follow-up work (out of scope for this plan)

These tasks are **deliberately not** in this plan, but they
become possible / necessary once the cutover is done.

| Task                                                                                                                                                                     | Why it's a follow-up                                                                                                                                                                                   | Reference                                              |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------ |
| **Sync the compendium into the new openviking**                                                                                                                          | The 93Mi wipe in Phase 3 means search returns 0. The compendium and the other source corpora need to be re-ingested.                                                                                   | Separate plan, not yet drafted                         |
| **Bump `vlm.max_concurrent` and `embedding.max_concurrent` to 4**                                                                                                        | The non-prod validation ran at `max_concurrent: 1`; the cutover keeps that. Multi-concurrency is the next unlock. The validation hypothesis (sibling writes don't serialize) holds at any concurrency. | TBD; needs load test against embedder-llamacpp on manu |
| **Scale up the `ov-worker` StatefulSet**                                                                                                                                 | The 2026-05-07 design doc (`viking/docs/2026-05-07-ov-indexing-perf-design.md`) describes the worker pool. Multi-replica workers with shared `agfs:s3` is now safe.                                    | TBD; depends on the concurrency bump                   |
| **Remove the experimental `openviking-config` ConfigMap**                                                                                                                | It's the historical record of the 2026-06-03 partial cutover. Once we're confident the cutover is stable for a week, this ConfigMap can be deleted.                                                    | Cleanup, not blocking                                  |
| **Delete the experimental parallel stack** (`ov-coordinator`, `ov-merge`, `ov-worker` Deployments/StatefulSet)                                                           | Scaled to 0/0 since 2026-05-09. They were workarounds for `local` VectorDB; the cutover makes them unnecessary.                                                                                        | Cleanup, not blocking                                  |
| **Update the test plan (`viking/docs/compendium-ov-test-plan.md`)**                                                                                                      | The "what we learned" section needs to absorb the 2026-06-03 cutover result.                                                                                                                           | Documentation                                          |
| **Bring the GPU design doc up to date** (`viking/docs/2026-05-07-ov-indexing-perf-design.md` and `viking/docs/2026-06-01-openviking-parallelization-cross-reference.md`) | Both were written when the `local` VectorDB was assumed. The conclusions stand but the framing is now "single-instance is fine; we're done with parallelization workarounds."                          | Documentation                                          |

## References

- `viking/docs/2026-06-03-ov-test-agfs-s3-vectordb-http.md` —
  the non-prod validation that gates this plan (commit `ba59efd`)
- `viking/docs/2026-06-01-openviking-parallelization-cross-reference.md` —
  cross-reference of the parallelization story against upstream
  OpenViking source; explains why `local` was the bottleneck and
  what changed when we moved to `http` + `s3`
- `viking/docs/2026-05-09-agfs-local-migration.md` — the
  precedent cutover (single-instance local AGFS, May 9);
  this plan is its inverse — moving to S3 + HTTP
- `viking/manifests/openviking-standalone-configmap.yaml` —
  the ConfigMap edited in Phase 2
- `viking/manifests/openviking-deployment.yaml` — the
  Deployment that picks up the new ConfigMap; init containers
  are backend-agnostic and need no change
- `viking/manifests/ov-vectordb-deployment.yaml` — the
  Deployment scaled up in Phase 3 step 3.2
- `viking/tools/ov-test-validate.sh` — the validation driver
  from the prerequisite test plan
- `viking/tools/index-homelab.py` and `viking/tools/index-projects.py` —
  used by the (separate) sync plan; the `temp_path` →
  `temp_file_id` fix was made in commit `ba59efd`
- `viking/deploy-openviking.sh` — the canonical apply script;
  the new ConfigMap replaces the one referenced in step 2 of
  that script
- `viking/CLAUDE.md` — updated in Phase 5 step 5.4

## Risk register

| Risk                                                                                     | Likelihood                                    | Impact                                                                            | Mitigation                                                                                                                                                                                                                                                                                                                                             |
| ---------------------------------------------------------------------------------------- | --------------------------------------------- | --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| ConfigMap syntax error in Phase 2 → pydantic validation fails at pod startup             | Low                                           | openviking pod CrashLoopBackOff                                                   | Phase 2 pauses for human review; rollback path is one ConfigMap edit                                                                                                                                                                                                                                                                                   |
| `openviking-agfs` bucket has stale data from a prior run (e.g. the 2026-05-09 migration) | Low                                           | confusion about which content is the "real" content; search returns mixed results | Inspect the bucket with `s3 list_objects_v2` before cutover; if non-empty, decide whether to wipe it (likely yes, given we're wiping the local AGFS too)                                                                                                                                                                                               |
| `openviking-agfs` S3 credentials are wrong                                               | Low                                           | AGFS connection fails at startup, `/ready` reports `agfs: unreachable`            | Confirmed working in the test stack (which uses the same secret and the same endpoint); Phase 3 step 3.4 catches this with a readiness check                                                                                                                                                                                                           |
| ov-vectordb pod takes longer than 120s to become ready                                   | Low                                           | Phase 3 step 3.2 stalls                                                           | Longhorn SSD PVCs typically attach in <30s; the 120s timeout is generous; bump to 300s if needed                                                                                                                                                                                                                                                       |
| Rollback path is needed but the data was already wiped                                   | Certain if rollback is needed after Phase 3.1 | the 93Mi of pre-cutover content is gone                                           | Phase 5 step 5.1 documents this and prescribes the Longhorn snapshot for pre-cutover restore                                                                                                                                                                                                                                                           |
| Smoke test in Phase 4 doesn't return any search results within the 5-minute poll         | Low                                           | unclear whether the cutover worked                                                | The OV log will show whether the doc is in the semantic queue and whether the VLM/embedding completed. If the doc was committed (`/api/v1/resources` 200) but search returns 0, the VLM queue is just slow — the poll handles up to 5 minutes. If search still returns 0, the embedding pipeline is broken and the cutover has failed in a subtle way. |
| The test stack is still running in Phase 1 because the cluster has other operators       | Low                                           | the tear-down command fails                                                       | All test resources have `app: openviking-test` / `app: ov-vectordb-test` labels; `kubectl delete` is scoped to those resources and won't touch production                                                                                                                                                                                              |

## Implementation note (per the create_plan skill)

After completing each phase and its automated verification, pause
for human confirmation that the manual testing was successful
before proceeding to the next phase. This is critical because the
plan is destructive in Phase 3 step 3.1 — the AGFS wipe cannot be
undone, and the operator must explicitly confirm the cutover is
proceeding as intended.
