# Capture validity rollout

The capture-validity rollout addresses BUG-1174 and deploys the already merged BUG-1177 renderer. The user authorized completion and cluster control on 2026-09-23, advancing the originally planned September 29 ChatLog boundary. This is a new IDEA-1100 configuration epoch; earlier ChatLogs are not rewritten or rescored as though they used this renderer.

## Lock-directory race

On the deployed v0.4.20 image, two independent RAGFS clients sharing the test S3 bucket reproduce the reported error deterministically. Client A caches a missing `history` directory. Client B creates it. A's lock acquisition receives `AlreadyExists` from `mkdir`, then receives its cached `NotFound` from the verification `stat`, raising `failed to create lock dir … already exists`.

`ov_s3_cache_patch.py` disables the existing Rust S3 plugin's metadata cache at the Python serializer boundary. The public configuration does not expose this setting in v0.4.20. Both stat and directory-list caches are disabled; other backends, locks, and lock error handling remain unchanged. This increases S3 metadata traffic in exchange for cross-worker visibility. The patch checks the installed version and exact serializer source hash, logs whether it applied, and can be disabled with `OV_S3_CACHE_PATCH=0` followed by a rollout.

Reproduce against ov-test only:

```bash
kubectl -n viking exec -i deploy/openviking-test -- /app/.venv/bin/python -B - < viking/scripts/test-s3-lockdir-race.py
```

The script asserts that caching reproduces the failure and disabling it allows acquisition and release. It retains a unique, small synthetic prefix in `openviking-agfs-test`; no production storage or model calls are involved. The first checked-in-script run passed at prefix `capture-validity-probe/b17c5890b5ce44488296380520e7f79b`.

## Completion tracking

The pre-roll control `capture-check-0bd2769099-0` archived three exact messages and extracted one memory at 22:50:04Z, but its task alternated between pending and running across HTTP requests while its completion marker stayed absent. Embedding queue work had finished. v0.4.20 keeps request-scoped queue tracking in a process-local singleton; a different worker can consume the embedding without completing the originating worker's tracker. Session Phase 2 waits up to 1,800 seconds before continuing after a timeout.

The standalone and test servers therefore use one worker. This retains asynchronous request handling and concurrent session commits, while keeping queue completion and task state in the same process. Returning to multiple workers requires shared completion tracking or a verified upstream fix; do not shorten the wait timeout or fabricate completion markers.

## Validation

- 28 extraction, ChatLog, and S3 patch unit checks pass.
- The S3 patch's version and source guard accepts the real installed v0.4.20 serializer; its output disables the cache.
- The live two-client S3 regression fails in the expected way with caching and passes without it.
- Concurrent HTTP commits, production extraction, ChatLog content, and the fresh pilot finish path must be checked after rollout before their vault entries are resolved.

Roll out the test ConfigMap and Deployment first. After verifying fresh commits and exact archived content, apply only the production ConfigMaps and Deployment changes. Preserve the queue and archive evidence, restore the temporarily suspended compendium-sync CronJob, and retain the existing BUG-1179 drainer configuration.
