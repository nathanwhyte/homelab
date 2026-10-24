# IMPR-1412 window — v0.4.20 → v0.5.1.1 (2026-10-24)

Smoke-check outputs from the Phase 3 window (GUIDE-1085 4.1), captured against production after the cutover. The pre-window captures they compare against are in `../2026-10-22-pre-v0.5.1.1/` (`*-window*` files and `read-baseline-plain.*`).

| Time (CDT) | Event |
| --- | --- |
| 11:15:59 | `openviking` at 0 replicas |
| 11:16:24 | Snapshots `ov-data-window-20261024161608`, `ov-vectordb-window-20261024161608` ready |
| 11:19:49 | homelab#263 merged (`c30e995`) |
| 11:26:11 | window sealed |
| 11:26:52 | v0.5.1.1 Ready (first start 19 s, outage 10 min 53 s) |

| # | File | Check | Result |
| --- | --- | --- | --- |
| 1 | `c1.txt` | Pod stable | 2/2 Ready, 0 restarts at T+12 min |
| 2 | `c2.txt` | Version | every OpenViking container and `/health` on v0.5.1.1 |
| 3 | `c3.txt` | Digest | `sha256:551b1ae0…`, the manifest pin |
| 5 | `c5.txt` | `ov status --verbose` | queues healthy and 0; `[vikingdb]`/`[system]` unhealthy from the `aggregate` 404, as expected |
| 6 | `c6.txt` | Identity | `trusted`, `default`/`noot` |
| 7 | `c7.txt`, `search-window-v0.5.1.1.json` | Search parity against `search-baseline-window-v0.4.20.json` | `--mapping half`: max deviation 0.0000, top-3 identical on 38/38 |
| 9 | `c9.txt` | AGFS bucket | 72,632 objects, as the window export |
| 10 | `c10.txt` | `usage_audit` | schema `5`, 2001 `request_audit` rows |

Check 4 (all nine runtime patches `applied`) and check 8 (read parity, byte-identical) were read from the live log and `ov read`; their results are recorded on IMPR-1412. The check 8 baseline (`read-baseline-plain.uri`, 2365 bytes) is not committed, because the trailing-whitespace hook would rewrite it; the pre-window and post-cutover reads both hash to sha256 `40a044dd1066edf01e80756e6c47a86d54d60fbe461e2f3ebb0b6f7c8b755f3e`.
