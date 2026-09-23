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
- The complete 20-way burst on ov-test and the separate 20-way production burst each accepted 20/20 commits and retained all 80 input messages exactly. Each produced three completed extractions with readable memory bodies and 17 explicit extraction failures. Every failed task has `.failed.json` (200), no `.done` (404), and no `memory_diff.json` (404). There were no lock-directory failures. These were natural `parse_error` failures under shared VLM load; no token-limit failure injection was used.
- The real original parent `cc-a818851a-f961-4054-b435-c229eb435e98` final archive_006 matches all 61 saved live messages exactly, then leaves an empty live file. Task `f04c3c3a-d3fc-4361-8fe4-011f94ee7d22` completed in about 73 seconds with one add and one update. H1 passes 17/17 prompts and 344/344 tools across 815 messages.
- Its real `capacity_auto_resolver_deploy.md` event contains the user's one-word `yes` under the compendium peer, eight `assistant` labels and no blank labels. The 2,350-byte body SHA-256 is `038958e997dd1a62a430bd0c373f597245ff475c597e47ac0d04a8835040a255`. This verifies BUG-1177 in production.
- BUG-1179's actual fresh outage session queued four messages plus its final commit; the existing worker replayed all five without another Claude launch. Exact message read-back, final archive, extraction, H1 and subsequent recall passed. The recall exercise exposed an additional Stop/SessionEnd ordering race, fixed in [dotfiles #143](https://github.com/nathanwhyte/dotfiles/pull/143), merge `f878ec94368c6f1418506cf0d67dfb18f86e03f8`.
- Plugin `0.4.7-bug1179.2`, pin `df22c3bc4380799057cd1f1046f57f59ab0fd415`, serializes capture and reconciles the ended transcript before committing. The installed cache passed byte-for-byte verification. Actual fresh session `66be3cc4-7986-4432-835b-78ce822734cf` delivered two exact turns, completed a final archive, left no live messages, passed H1, and produced a readable event. The earlier stranded recall fixture was also explicitly finished and checked.

Redacted per-task receipts, artifact hashes, content-comparison results and private receipt hashes are in [2026-09-23-capture-validity.json](2026-09-23-capture-validity.json). Full transcripts stay in private `/private/tmp/capture-validity/` evidence. All acceptance fixtures are excluded from trial scoring.

## Remaining BUG-1176 gate

The original 73-message, 108,517-byte pilot archive was replayed during load on ov-test. All original content fields matched; only 31 server-generated `tool_output_group_id` values changed. The task nevertheless completed with a zero-operation diff. This is a parsed-zero result, not an exhausted retry disguised as success. The production failures prove the patch's failure visibility; they do not prove the cause of the historical five zero runs or the entry's real-pilot retry-recovery gate. BUG-1176 stays deployed and unresolved, with the H4/C1/C4 exclusions intact. Inspect the parsed operation decision and update-field validation before changing zero-result semantics: valid no-change sessions exist.

The fresh recall fixture assembled eight entries/1,503 tokens in about 8.4 seconds under load. Its correct answer proves retrieval, not the 500 ms H7 gate. No pilot scoring gate was relaxed.

## Operational closeout

Test rolled first; production rolled at approximately 23:19Z on 2026-09-23. Both deployments are Ready with one API worker. The existing scoped drainer configuration was preserved and the worker resumed after the plugin cutover. The temporary port-forward is stopped, all bounded extraction tests are terminal, and `compendium/compendium-sync` is restored to `suspend: false`. Original archives, queue backups, and small test prefixes are retained.
