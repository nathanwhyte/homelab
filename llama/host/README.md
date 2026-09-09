# Host Ollama on timmy

Since 2026-09 (IMPR-1075) Ollama runs **once**, on timmy's host under systemd, and the k3s
cluster consumes it by name. The pod Deployment is retired. This directory is the whole host
side; the cluster side is `llama/ollama-service.yaml` (selector-less Service + EndpointSlice)
and `llama/ollama-jobs.yaml` (CronJobs that talk to the daemon over HTTP).

| Piece                             | Where it lands on timmy                             | What it does                                                            |
| --------------------------------- | --------------------------------------------------- | ----------------------------------------------------------------------- |
| `ollama.service.d/homelab.conf`   | `/etc/systemd/system/ollama.service.d/`             | The env the pod used to carry (bind, ctx, KV, Vulkan, …)                |
| `ollama-warm.sh` + `.service`     | `/opt/ollama-host/`, `/etc/systemd/system/`         | Post-start: build + load-only warm of the FIM tag, build agentpair tags |
| `install-host-ollama.sh`          | run from a repo checkout, with sudo                 | Pins the binary, installs the above, reconciles the exporter            |
| `llama/ollama/ollama-exporter.py` | `/opt/ollama-exporter/` (`ollama-exporter.service`) | Prometheus metrics on `:9111`, scraped as `ollama.llama.svc:9111`       |

Why host instead of pod: single owner of `11434` (no klipper DNAT arbitration), real VRAM
accounting under Vulkan (no device-plugin injection, so no `CAP_PERFMON` workaround from
IMPR-1022), and the daemon survives k3s restarts and node drains without a spin-down step.
What moved out of Kubernetes: liveness/rollout for the daemon (systemd `Restart=always` covers
crashes) and the image pin, which is now `OLLAMA_VERSION` in the install script.

## Cutover runbook (one-time, ordered)

The order matters because klipper's svclb pod holds host port `11434` for the old
LoadBalancer Service; the host daemon cannot bind `0.0.0.0:11434` until that is gone
(observed as `bind: address already in use` on 2026-09-08).

1. **Copy the model store while the pod still runs** (Longhorn PVC → host, ~60 GB, local disk
   to local disk):

   ```bash
   sudo llama/host/install-host-ollama.sh --check          # see current state
   sudo llama/host/install-host-ollama.sh --sync-models    # rsync PVC models -> /usr/share/ollama/.ollama/models
   ```

   This step will stop at "refusing to restart" if svclb still owns the port — expected.

2. **Swap the Service** so the cluster name points at the host and svclb releases the port:

   ```bash
   kubectl apply -f llama/ollama-service.yaml
   kubectl -n llama get endpointslices -l kubernetes.io/service-name=ollama   # ollama-timmy-host -> 192.168.1.19
   kubectl -n kube-system get pods | grep svclb-ollama                        # should be gone
   ```

   From this moment `ollama.llama.svc` resolves to `192.168.1.19:11434` and nothing answers
   there until step 3 — a gap of the seconds it takes to run the next command. Do it now.

3. **Bring the host daemon up on all interfaces**:

   ```bash
   sudo llama/host/install-host-ollama.sh        # restarts ollama.service with the drop-in
   journalctl -fu ollama-warm                      # watch the FIM warm
   curl -s http://192.168.1.19:11434/api/tags | head -c 300
   ```

4. **Retire the pod** once consumers are verified:

   ```bash
   kubectl -n llama run -it --rm probe --image=curlimages/curl:8.11.1 --restart=Never -- -fsS http://ollama.llama.svc:11434/api/ps
   kubectl -n llama delete deployment ollama
   kubectl -n llama delete configmap ollama-startup
   kubectl apply -f llama/ollama-jobs.yaml
   ```

   Keep `llama-model-cache` (PVC) for a week as rollback, then delete it and `llama/pvc.yaml`.

5. **Update the docs that name the pod** — `reference/llm-config.md`, `reference/service-routing.md`,
   `llama/README.md` (done in the same PR), and compendium `INFO-1023`.

## Day-to-day

| Task                          | Command (on timmy unless noted)                                                                 |
| ----------------------------- | ----------------------------------------------------------------------------------------------- |
| Restart the daemon            | `sudo systemctl restart ollama` (warm re-runs via `PartOf`)                                     |
| Change an env value           | edit `homelab.conf` in the repo, `sudo llama/host/install-host-ollama.sh`                       |
| Upgrade Ollama                | bump `OLLAMA_VERSION` in the install script, run it (that is the pin)                           |
| Pull a model from the cluster | `kubectl -n llama create job --from=cronjob/ollama-pull pull-$(date +%s)` after setting `MODEL` |
| Is the FIM model resident?    | `curl -s 192.168.1.19:11434/api/ps` or `cat /run/ollama/models-ready`                           |
| Metrics                       | `curl -s 192.168.1.19:9111/metrics`; Grafana scrapes `ollama.llama.svc:9111`                    |

Node maintenance: `scripts/node-maintenance.sh --spin-down` no longer scales `llama/ollama`
(there is no Deployment); the daemon rides through drains and stops with the host on reboot.

## Rollback

Stop the host units first (`sudo systemctl disable --now ollama ollama-warm`), then restore the
pod and its LoadBalancer Service from the pre-cutover commit:

```bash
git show <pre-cutover-sha>:llama/ollama-deployment.yaml | kubectl apply -f -
git show <pre-cutover-sha>:llama/ollama-configmap.yaml | kubectl apply -f -
kubectl -n llama delete endpointslice ollama-timmy-host
```

The order is the mirror of the cutover: the svclb bind fails while the host daemon still holds
`0.0.0.0:11434`.
