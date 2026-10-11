# mac/ — pop's Ollama serving config

What remains here is how Ollama **serves** from pop, not how it is observed. The
telemetry pipeline that used to sit alongside it — exporters, an Alloy
container, dashboards — was retired on 2026-10-11, after it turned out never to
have delivered anything to the cluster. Evidence, reasoning, and the full list
of what was removed: [RETIRED.md](RETIRED.md).

| Path | What |
| --- | --- |
| `launchd/com.user.ollama-serve.plist` | User LaunchAgent that runs `Ollama.app`'s `ollama serve` |
| `pf/com.user.ollama-serve.anchor` | `pf` anchor letting the cluster reach pop's Ollama port |
| `docs/ollama-pop-fw.md` | The `socketfilterfw` / `pf` steps that anchor needs |

## Install

```bash
cd /Users/noot/code/homelab/main

# Ollama serving (user LaunchAgent — no root)
cp mac/launchd/com.user.ollama-serve.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.ollama-serve.plist

# Firewall, so the cluster can reach it
sudo cp mac/pf/com.user.ollama-serve.anchor /etc/pf.anchors/com.user.ollama-serve
# then follow docs/ollama-pop-fw.md
```

## Verify

```bash
launchctl list | grep ollama-serve
curl -fsS localhost:11434/api/version
```

## Elsewhere

Machine-local Ollama work — the hand-run `ollama-serve`, log monitoring, and the
`clw` prefix prewarm with its parity gate — lives in
[`ollama/OPERATIONS.md`](../ollama/OPERATIONS.md), not here. The cluster-side
Ollama stack (chat proxy, auth proxy, cloud counter) is under
[`llama/`](../llama/).
