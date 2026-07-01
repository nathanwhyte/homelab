# MacBook → Cluster telemetry

Ships Ollama metrics, Apple Silicon GPU telemetry, general MacBook hardware
metrics, and the `ollama serve` log from the M5 MacBook into the in-cluster
Prometheus + Loki, where they appear on the **Ollama & GPU Metrics** and **Mac
Metrics** Grafana dashboards tagged `node="macbook-m5"`.

The cluster receives via NodePorts already exposed on every node:

| Purpose                 | URL                                          |
| ----------------------- | -------------------------------------------- |
| Prometheus remote-write | `http://192.168.1.19:30909/api/v1/write`     |
| Loki push               | `http://192.168.1.19:31080/loki/api/v1/push` |

## Architecture

```text
┌─ macOS host ──────────────────────────────────────────┐
│                                                       │
│  ollama serve (native, :11434)                        │
│  ~/.ollama/logs/server.log                            │
│                                                       │
│  mac-gpu-exporter.py (native LaunchDaemon, :9112)     │
│  └─ powermetrics (requires hardware access)           │
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
│  │    ├─ scrape host.docker.internal:9112          │  │
│  │    ├─ tail  /var/log/ollama/server.log          │  │
│  │    │        (bind: ~/.ollama/logs)              │  │
│  │    ├─ remote_write 192.168.1.19:30909           │  │
│  │    └─ loki.write   192.168.1.19:31080           │  │
│  │                                                 │  │
│  └─────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────┘
```

**Why GPU exporter is native:** `powermetrics` reads hardware sensors via
IOKit. macOS containers run inside a Linux VM that has no view of those
sensors, so the GPU exporter must run on the host.

## Layout

| Path                                             | What                                                                                                                                                                       |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `docker-compose.yml`                             | Defines `ollama-exporter` (image: `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`, runs via `uv run --no-project`) and `alloy` (image: `grafana/alloy:latest`) containers. |
| `alloy/config.alloy`                             | Alloy: scrape all exporters, tail Ollama log, ship to cluster. Bind-mounted into the alloy container.                                                                      |
| `exporters/ollama-exporter.py`                   | Pure-Python exporter, bind-mounted into the ollama-exporter container. Identical to `llama/ollama/ollama-exporter.py`.                                                     |
| `exporters/mac-gpu-exporter.py`                  | Native Apple Silicon GPU exporter (wraps `powermetrics`). Runs as a LaunchDaemon on the host.                                                                              |
| `exporters/test_mac_gpu_exporter.py`             | Unit tests for the `powermetrics` parser.                                                                                                                                  |
| `launchd/com.nathanwhyte.mac-gpu-exporter.plist` | System LaunchDaemon (runs as root for `powermetrics`).                                                                                                                     |
| `launchd/com.nathanwhyte.macmon.plist`           | User LaunchAgent that runs `macmon serve` on `:9113` (no root).                                                                                                            |
| `grafana/dashboards/mac-metrics.json`            | New Grafana dashboard for MacBook hardware metrics.                                                                                                                        |
| `grafana/manifests/mac-metrics-dashboard.yaml`   | ConfigMap that provisions the Mac Metrics dashboard.                                                                                                                       |

## Install

Requires Docker Desktop, OrbStack, Colima, or Rancher Desktop. Anything that
provides `docker compose` and a `host-gateway` for `host.docker.internal`.

```bash
cd /Users/noot/code/homelab

# 1. Native GPU exporter (system LaunchDaemon — needs root)
sudo cp mac/launchd/com.nathanwhyte.mac-gpu-exporter.plist /Library/LaunchDaemons/
sudo chown root:wheel /Library/LaunchDaemons/com.nathanwhyte.mac-gpu-exporter.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/com.nathanwhyte.mac-gpu-exporter.plist

# 2. User LaunchAgent for macmon (no root)
mkdir -p /Users/noot/.local/share/macmon
cp mac/launchd/com.nathanwhyte.macmon.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nathanwhyte.macmon.plist

# 3. Containers
docker compose -f mac/docker-compose.yml up -d
```

## Verify

```bash
# Containers running
docker compose -f mac/docker-compose.yml ps

# Local exporters
curl -fsS localhost:9111/metrics | grep ^ollama_up           # via compose port-forward
curl -fsS localhost:9112/metrics | grep ^mac_gpu_power_watts # native LaunchDaemon
curl -fsS localhost:9113/metrics | grep ^macmon_sys_power_watts # user LaunchAgent

# Alloy
curl -fsS localhost:12345/-/ready                            # OK

# LaunchAgents / LaunchDaemons
sudo launchctl list | grep mac-gpu-exporter
launchctl list | grep macmon

# End-to-end (from anywhere with cluster Grafana MCP access)
# ollama_up{node="macbook-m5"}      → 1
# mac_gpu_power_watts                → live value
# macmon_sys_power_watts{node="macbook-m5"} → live value
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

## Tests

```bash
python3 mac/exporters/test_mac_gpu_exporter.py -v
```

## Update

```bash
docker compose -f mac/docker-compose.yml pull
docker compose -f mac/docker-compose.yml up -d
```

## Uninstall / rollback

```bash
docker compose -f mac/docker-compose.yml down -v
sudo launchctl unload /Library/LaunchDaemons/com.nathanwhyte.mac-gpu-exporter.plist
sudo rm /Library/LaunchDaemons/com.nathanwhyte.mac-gpu-exporter.plist
```

No persistent state on the Mac beyond `/var/log/mac-gpu-exporter.log`,
`/Users/noot/.local/share/macmon/macmon.log`, and the named Docker volume
`mac_alloy-data` (removed by `down -v`).

## Dashboards

Metrics from this pipeline are used on two Grafana dashboards:

- **Ollama & GPU Metrics** (`grafana/dashboards/ollama-and-gpu-metrics.json`) —
  cluster AMD GPU plus a MacBook row with `macmon_*` GPU/power/temperature panels.
- **Mac Metrics** (`grafana/dashboards/mac-metrics.json`) — dedicated Apple
  Silicon CPU/GPU/ANE power, frequency, temperature, and memory overview.

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
