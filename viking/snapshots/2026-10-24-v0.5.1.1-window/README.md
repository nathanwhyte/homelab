# IMPR-1412 window — v0.4.20 → v0.5.1.1 (2026-10-24)

Smoke-check outputs from the Phase 3 window (GUIDE-1085 4.1), captured against production after the cutover. The pre-window captures they compare against are in `../2026-10-22-pre-v0.5.1.1/` (`*-window*` files and `read-baseline-plain.uri`).

| Time (CDT) | Event | Evidence |
| --- | --- | --- |
| 11:15:59 | `openviking` at 0 replicas | `window-log.txt` line 135 |
| 11:16:17, 11:16:24 | Snapshots `ov-data-window-20261024161608`, `ov-vectordb-window-20261024161608` ready | `window-log.txt` lines 136–137 |
| 11:19:49 | homelab#263 merged (`c30e995`) | the merge commit on `main` |
| 11:26:11 | window sealed | `window-log.txt` line 145 |
| 11:26:33 | v0.5.1.1 pod started | `c1.txt` `START` 16:26:33Z |
| 11:26:52 | v0.5.1.1 Ready: first start 19 s (11:26:33 → 11:26:52), outage 10 min 53 s (11:15:59 → 11:26:52) | `first-start-log.txt` line 85, `Application startup complete` at 16:26:52Z |

`window-log.txt` is the window harness log (freeze to seal), and `first-start-log.txt` is the v0.5.1.1 server's first startup log. Both are copies of the window's `window.log` and `first-start.log`, renamed because `.log` is gitignored; the trailing-whitespace hook stripped line-end spaces in `first-start-log.txt`.

| # | File | Check | Result |
| --- | --- | --- | --- |
| 1 | `c1.txt` | Pod stable | 2/2 Ready, 0 restarts; the file does not record when it was captured |
| 2 | `c2.txt` | Version | the three `openviking` image references and `/health` on v0.5.1.1 |
| 3 | `c3.txt` | Digest | `sha256:551b1ae0…` on `openviking` and `ov-exporter`, the digest commented beside the image in `../../manifests/openviking-deployment.yaml` |
| 5 | `c5.txt` | `ov status --verbose` | `[queue]` healthy, every queue row 0; `[vikingdb]` unhealthy from the `aggregate` 404 (BUG-1268, since fixed by homelab#265) |
| 6 | `c6.txt` | Identity | `trusted`, `default`/`noot`; both keys masked |
| 7 | `c7.txt`, `search-window-v0.5.1.1.json` | Search parity against `../2026-10-22-pre-v0.5.1.1/search-baseline-window-v0.4.20.json` | `--mapping half`: 38 queries, 374 shared hits, max deviation 0.0000, top-3 differs on 0 queries |
| 9 | `c9.txt` | AGFS bucket | 72,632 objects, the same count as the window's AGFS export (`window-log.txt` line 138) |
| 10 | `c10.txt` | `usage_audit` | schema `5`, 2001 `request_audit` rows |

Not committed here; recorded on IMPR-1412 in the compendium instead:

- Check 4 (all nine runtime patches `applied`) was read from the live server log.
- Check 8 (read parity). The URI pointer `read-baseline-plain.uri` (64 bytes) is committed. The baseline body it names (2365 bytes) is not, because the trailing-whitespace hook would rewrite it. The pre-window and post-cutover reads both hashed to sha256 `40a044dd1066edf01e80756e6c47a86d54d60fbe461e2f3ebb0b6f7c8b755f3e`.
