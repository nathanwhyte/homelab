# OpenViking v0.4.20 → v0.5.1.1 version set (IMPR-1412)

GUIDE-1085 Phase 0.1–0.2, captured 2026-10-22 on pop. "After" read on 2026-10-31 on pop, except where it says otherwise.

## Target

| Field | Value |
| --- | --- |
| Target tag | `v0.5.1.1`, published 2026-10-20T12:40:03Z, the latest core release on 2026-10-22 (no `vX.Y.Z` after it) |
| Target digest | `sha256:551b1ae00f59fd7b1a2434ecc10a4860be34aae8b7627f5f57240be355b23217` (`docker buildx imagetools inspect`) |
| Current digest | `sha256:b9827753d035f4157b5b318865907fd18f738924ad6398c86feb1182f209825b` (v0.4.20, every running OpenViking container) |

## Baseline

| Field | Before | After |
| --- | --- | --- |
| Server `/health` | `v0.4.20`, `auth_mode: trusted` | `v0.5.1.1`, `Trusted` |
| Images (`v0.4.20`, 7 homelab lines) | `openviking-deployment.yaml:126,315`, `ov-vectordb-deployment.yaml:26`, `ovlock-janitor-cronjob.yaml:63`, `ov-worker-statefulset.yaml:133`, `test/openviking-test-deployment.yaml:106`, `test/ov-test-vectordb-deployment.yaml:26` | `v0.5.1.1`, moved by homelab#263 |
| Images (dotfiles) | `claude/ov-pilot/pending-canary.yaml:35` `v0.4.20` | `v0.5.1.1` |
| Runtime patches `EXPECTED_VERSION` | `v0.4.20`, 12 modules | `v0.5.1.1`, 11 modules: chatlog, event_abstract, extract, grep_scope, http_update, memory_guard, nav, pref_gate, recall_time, s3_cache, vectordb_agg |
| SDK pins | `openviking==0.4.10 openviking-sdk==0.1.16` in `compendium-sync-cronjob.yaml:138`, `compendium-sync-job.template.yaml:101`, `compendium-backfill-job.template.yaml:56`; vault `pyproject.toml:11` | `openviking==0.5.1.1 openviking-sdk==0.1.16` in the same three specs (homelab#280) and the vault `pyproject.toml` (compendium#2208) |
| CLI | `ov --version` → `openviking 0.4.20` on pop; dotfiles `auto-install.sh:363` `OPENVIKING_VERSION="0.4.20"` | `openviking 0.5.1.1` on pop; dotfiles `auto-install.sh:386` `OPENVIKING_VERSION="0.5.1.1"` |
| ovpilot plugin pin | `ov-pilot-plugin-check.sh:38` `PIN_VERSION="0.4.7-bug1259.1"` | `PIN_VERSION="0.7.12-bug1281.1"`, on both MacBooks since 2026-10-31 (workbook's read at its 04:31Z cutover) |
| `ovlock-janitor` suspend | `false` | `false` |
| `ov-lock-watchdog` suspend | `false` | `false` |
| `compendium-sync` suspend | `false` | `false` |

The work MacBook's `ov --version` is read there before Phase 5. After Phase 5, the user reported it updated; this file holds no reading of it from pop.
