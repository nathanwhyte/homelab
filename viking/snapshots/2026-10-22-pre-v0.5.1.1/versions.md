# OpenViking v0.4.20 → v0.5.1.1 version set (IMPR-1412)

GUIDE-1085 Phase 0.1–0.2, captured 2026-10-22 on pop. "After" fills in during Phase 3–5.

## Target

| Field | Value |
| --- | --- |
| Target tag | `v0.5.1.1`, published 2026-10-20T12:40:03Z, the latest core release on 2026-10-22 (no `vX.Y.Z` after it) |
| Target digest | `sha256:551b1ae00f59fd7b1a2434ecc10a4860be34aae8b7627f5f57240be355b23217` (`docker buildx imagetools inspect`) |
| Current digest | `sha256:b9827753d035f4157b5b318865907fd18f738924ad6398c86feb1182f209825b` (v0.4.20, every running OpenViking container) |

## Baseline

| Field | Before | After |
| --- | --- | --- |
| Server `/health` | `v0.4.20`, `auth_mode: trusted` | |
| Images (`v0.4.20`, 7 homelab lines) | `openviking-deployment.yaml:126,315`, `ov-vectordb-deployment.yaml:26`, `ovlock-janitor-cronjob.yaml:63`, `ov-worker-statefulset.yaml:133`, `test/openviking-test-deployment.yaml:106`, `test/ov-test-vectordb-deployment.yaml:26` | |
| Images (dotfiles) | `claude/ov-pilot/pending-canary.yaml:35` `v0.4.20` | |
| Runtime patches `EXPECTED_VERSION` | `v0.4.20`, 12 modules | |
| SDK pins | `openviking==0.4.10 openviking-sdk==0.1.16` in `compendium-sync-cronjob.yaml:138`, `compendium-sync-job.template.yaml:101`, `compendium-backfill-job.template.yaml:56`; vault `pyproject.toml:11` | |
| CLI | `ov --version` → `openviking 0.4.20` on pop; dotfiles `auto-install.sh:363` `OPENVIKING_VERSION="0.4.20"` | |
| ovpilot plugin pin | `ov-pilot-plugin-check.sh:38` `PIN_VERSION="0.4.7-bug1259.1"` | |
| `ovlock-janitor` suspend | `false` | |
| `ov-lock-watchdog` suspend | `false` | |
| `compendium-sync` suspend | `false` | |

The work MacBook's `ov --version` is read there before Phase 5.
