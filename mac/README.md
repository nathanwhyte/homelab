# MacBook → Cluster telemetry

Ships Ollama metrics, Apple Silicon GPU telemetry, general MacBook hardware
metrics, and the `ollama serve` log from the M5 MacBook into the in-cluster
Prometheus + Loki, where they appear on the **Ollama & GPU Metrics** and **Mac
Metrics** Grafana dashboards tagged `node="macbook-m5"`.

> **This pipeline is NOT currently running** (checked 2026-07-21): pop has no
> `alloy`/`grafana-agent` process, no launchd plist, and Prometheus holds no
> `node="macbook-m5"` series. This document describes how to run it, not
> something that is running. It read as a description of live state, which is
> how the gap went unnoticed.

The cluster receives via NodePorts exposed on every node:

| Purpose                 | URL                                          |
| ----------------------- | -------------------------------------------- |
| Prometheus remote-write | `http://192.168.1.19:30909/api/v1/write`     |
| Loki push               | `http://192.168.1.19:31080/loki/api/v1/push` |

> **These NodePorts are allowlisted to pop only** (IMPR-1020,
> `grafana/nodeport-networkpolicy.yaml`). Until 2026-07-21 they were reachable
> unauthenticated by any tailnet peer, which could read all metrics, read the
> Prometheus scrape config, and inject arbitrary log lines. A NetworkPolicy now
> permits only pop's three addresses — `100.113.28.62` (tailnet),
> `192.168.1.6` (LAN wired), `192.168.1.8` (LAN wifi).
>
> Consequences if you change machines: **reviving this pipeline from any other
> host will silently fail** until that host's addresses are added to the policy.
> Both Services also carry `externalTrafficPolicy: Local` (the policy cannot see
> real client IPs otherwise) and `loki-gateway` is pinned to timmy so that the
> `192.168.1.19` endpoint above keeps working — do not unpin it without
> repointing these URLs.

## Architecture

```text
┌─ macOS host ──────────────────────────────────────────┐
│                                                       │
│  ollama serve (native, :11434)                        │
│  ~/.ollama/logs/server.log                            │
│                                                       │
│  macbook-backup-exporter.py (user LaunchAgent, :9114) │
│  └─ age of the macbook-backup last-success marker     │
│     (no root required)                               │
│                                                       │
│  macmon serve (user LaunchAgent, :9113)              │
│  └─ CPU/GPU/ANE power, temps, freq, RAM/swap         │
│     (no root required)                               │
│                                                       │
│  ┌─ Docker (compose project: mac) ─────────────────┐  │
│  │                                                 │  │
│  │  ollama-exporter                                │  │
│  │    └─ scrapes host.docker.internal:11434        │  │
│  │                                                 │  │
│  │  alloy                                          │  │
│  │    ├─ scrape ollama-exporter:9111               │  │
│  │    ├─ scrape host.docker.internal:9113 (macmon) │  │
│  │    ├─ scrape host.docker.internal:9114 (backup) │  │
│  │    ├─ tail  /var/log/ollama/server.log          │  │
│  │    │        (bind: ~/.ollama/logs)              │  │
│  │    ├─ remote_write 192.168.1.19:30909           │  │
│  │    └─ loki.write   192.168.1.19:31080           │  │
│  │                                                 │  │
│  └─────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────┘
```

**Why the host exporters are native:** macmon reads Apple Silicon sensors via
IOReport/SMC, and the backup exporter stats a file in the user's home. macOS
containers run inside a Linux VM with no view of either, so both run on the
host — and neither needs root.

`mac_gpu_*` (the old `powermetrics` LaunchDaemon on `:9112`) was **retired**:
it was dead — pointing at a pre-migration path, 10781 failed runs — nothing
graphed it, and macmon covered the same ground more cheaply. See BUG-1212.

## Layout

| Path                                             | What                                                                                                                                                                       |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `docker-compose.yml`                             | Defines `ollama-exporter` (image: `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`, runs via `uv run --no-project`) and `alloy` (image: `grafana/alloy:latest`) containers. |
| `alloy/config.alloy`                             | Alloy: scrape all exporters, tail Ollama log, ship to cluster. Bind-mounted into the alloy container.                                                                      |
| `exporters/ollama-exporter.py`                   | Pure-Python exporter, bind-mounted into the ollama-exporter container. Identical to `llama/ollama/ollama-exporter.py`.                                                     |
| `exporters/macbook-backup-exporter.py`           | Exports the age of the macbook-backup last-success marker (BUG-1212). User LaunchAgent, no root.                                                                             |
| `launchd/com.nathanwhyte.macbook-backup-exporter.plist` | User LaunchAgent for the backup freshness exporter on `:9114`.                                                                                                      |
| `launchd/com.nathanwhyte.macmon.plist`           | User LaunchAgent that runs `macmon serve` on `:9113` (no root).                                                                                                            |
| `grafana/dashboards/mac-metrics.json`            | New Grafana dashboard for MacBook hardware metrics.                                                                                                                        |
| `grafana/manifests/mac-metrics-dashboard.yaml`   | ConfigMap that provisions the Mac Metrics dashboard.                                                                                                                       |

## Install

Requires Docker Desktop, OrbStack, Colima, or Rancher Desktop. Anything that
provides `docker compose` and a `host-gateway` for `host.docker.internal`.

```bash
cd /Users/noot/code/homelab/main

# 1. macmon (user LaunchAgent — no root)
mkdir -p /Users/noot/.local/share/macmon
cp mac/launchd/com.nathanwhyte.macmon.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nathanwhyte.macmon.plist

# 2. macbook-backup freshness exporter (user LaunchAgent — no root)
mkdir -p /Users/noot/.local/share/macbook-backup-exporter
cp mac/launchd/com.nathanwhyte.macbook-backup-exporter.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nathanwhyte.macbook-backup-exporter.plist

# 3. Containers
docker compose -f mac/docker-compose.yml up -d
```

**Retiring the old GPU exporter.** If `com.nathanwhyte.mac-gpu-exporter` was
ever installed as a system daemon, remove it — nothing scrapes it now:

```bash
sudo launchctl bootout system/com.nathanwhyte.mac-gpu-exporter
sudo rm /Library/LaunchDaemons/com.nathanwhyte.mac-gpu-exporter.plist
```

## Verify

```bash
# Containers running
docker compose -f mac/docker-compose.yml ps

# Local exporters
curl -fsS localhost:9111/metrics | grep ^ollama_up           # via compose port-forward
curl -fsS localhost:9113/metrics | grep ^macmon_sys_power_watts # user LaunchAgent
curl -fsS localhost:9114/metrics | grep ^macbook_backup      # user LaunchAgent

# Alloy
curl -fsS localhost:12345/-/ready                            # OK

# LaunchAgents / LaunchDaemons
launchctl list | grep macmon
launchctl list | grep macbook-backup-exporter

# End-to-end (from anywhere with cluster Grafana MCP access)
# ollama_up{node="macbook-m5"}      → 1
# macmon_sys_power_watts{node="macbook-m5"} → live value
# macbook_backup_last_success_timestamp_seconds{node="macbook-m5"} → recent mtime
# {job="ollama", host="macbook-m5"} in Loki → recent log lines
```

## Ollama log path

The Alloy container bind-mounts `~/.ollama/logs` (Ollama.app's default
location) read-only at `/var/log/ollama` and tails `server.log` inside it.

If you run `ollama serve` from a shell instead of Ollama.app, Ollama writes
to stdout. The simplest fix is to launch it under launchd with stdout
redirected into the same directory so Alloy picks it up unchanged:

```xml
<key>StandardOutPath</key>
<string>/Users/noot/.ollama/logs/server.log</string>
<key>StandardErrorPath</key>
<string>/Users/noot/.ollama/logs/server.log</string>
```

Otherwise update the bind mount in `docker-compose.yml` and the
`__path__` in `alloy/config.alloy` to point at the alternative directory.

## Update

```bash
docker compose -f mac/docker-compose.yml pull
docker compose -f mac/docker-compose.yml up -d
```

## Uninstall / rollback

```bash
docker compose -f mac/docker-compose.yml down -v

# Host agents (both user-level, no sudo)
launchctl bootout gui/$(id -u)/com.nathanwhyte.macmon
launchctl bootout gui/$(id -u)/com.nathanwhyte.macbook-backup-exporter
rm ~/Library/LaunchAgents/com.nathanwhyte.macmon.plist
rm ~/Library/LaunchAgents/com.nathanwhyte.macbook-backup-exporter.plist
```

No persistent state on the Mac beyond `/Users/noot/.local/share/macmon/macmon.log`,
`/Users/noot/.local/share/macbook-backup-exporter/exporter.log`, and the named
Docker volume `mac_alloy-data` (removed by `down -v`).

## Dashboards

Metrics from this pipeline are used on two Grafana dashboards:

- **Ollama & GPU Metrics** (`grafana/dashboards/ollama-and-gpu-metrics.json`) —
  cluster AMD GPU plus a MacBook row with `macmon_*` GPU/power/temperature panels.
- **Mac Metrics** (`grafana/dashboards/mac-metrics.json`) — dedicated Apple
  Silicon CPU/GPU/ANE power, frequency, temperature, and memory overview.

`macbook_backup_last_success_timestamp_seconds` is deliberately not dashboarded:
it exists for the freshness alert in
`grafana/manifests/macbook-backup-alerts.yaml`, which fires when the mirror has
not completed a clean run in 24h.

## Notes

- The compose project name defaults to `mac` (directory name), so the
  named volume is `mac_alloy-data` and containers are reachable by their
  short names within the compose network.
- `host.docker.internal:host-gateway` works on Docker Desktop, OrbStack,
  Rancher Desktop, and Colima ≥ 0.5.6.
- Alloy and the Ollama exporter restart automatically with `restart:
unless-stopped`. The native GPU LaunchDaemon restarts via launchd.
- If Docker isn't running at boot, the containers won't start — enable
  "Start on login" / "Launch at startup" in Docker Desktop / OrbStack.
