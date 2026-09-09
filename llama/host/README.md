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
   sudo systemctl stop ollama                            # stops only the old host daemon
   sudo llama/host/install-host-ollama.sh --sync-identity  # preserve the pod's cloud sign-in identity
   ```

   `--sync-models` only copies models and exits successfully. It never installs a binary,
   writes units, or restarts anything. Pause model pulls/creates during the copy; partial
   downloads are excluded. Compare `/api/tags` names and digests before retiring the pod.
   The daemon's cloud identity (`.ollama/id_ed25519`) is separate from the model store.
   `--sync-identity` requires the host daemon to be stopped, backs up its previous identity
   under a private `/var/backups/ollama-identity.*` directory, and copies the pod identity
   with mode 0600. Do this while the PVC is still mounted. A loopback cloud probe before
   removing ServiceLB DNAT can actually reach the pod; verify host authentication only
   after cutover. Model copying alone caused cloud 401s during the first live deployment.

   Save the live Deployment, ConfigMap, and Service for rollback in a private directory.
   Use those snapshots to preserve the deployed image/config, rather than pulling `latest`.

2. **Swap the Service** so the cluster name points at the host and svclb releases the port:

   ```bash
   kubectl apply -f llama/ollama-service.yaml
   # The legacy Endpoints object otherwise recreates mirrored pod slices.
   kubectl -n llama delete endpoints ollama --ignore-not-found
   kubectl -n llama delete endpointslice -l 'kubernetes.io/service-name=ollama,endpointslice.kubernetes.io/managed-by=endpointslice-controller.k8s.io'
   kubectl -n llama delete endpointslice -l 'kubernetes.io/service-name=ollama,endpointslice.kubernetes.io/managed-by=endpointslicemirroring-controller.k8s.io'
   kubectl -n llama get endpointslices -l kubernetes.io/service-name=ollama   # ollama-timmy-host -> 192.168.1.19
   kubectl -n kube-system get pods | grep svclb-ollama                        # should be gone
   ```

   This starts a maintenance gap. Verify the Service has no selector or external IPs.
   Only `ollama-timmy-host` should remain. Host ports use DNAT rules,
   which are invisible to `ss`; the installer checks both Kubernetes state and host NAT.

3. **Release the pod's GPU, then bring the host daemon up**:

   ```bash
   kubectl -n llama scale deployment ollama --replicas=0
   kubectl -n llama wait --for=delete pod -l app=ollama --timeout=120s
   sudo llama/host/install-host-ollama.sh        # restarts ollama.service with the drop-in
   journalctl -fu ollama-warm                      # watch the FIM warm
   curl -s http://192.168.1.19:11434/api/tags | head -c 300
   ```

   Never warm the host while the pod runner still holds the GPU. The installer fails
   before mutations if the old pod, ServiceLB pods, or old external DNAT rules remain.
   Warmup runs asynchronously; API readiness does not prove model readiness. Check
   `systemctl status ollama-warm`, `/api/ps`, a bounded FIM completion, and journal
   evidence naming Vulkan and the RX 9070 XT. Verify LAN, Tailscale, cluster DNS, the
   chat proxy's cloud route, and the Prometheus scrape before continuing.

4. **Retire the Deployment** once consumers are verified:

   ```bash
   kubectl -n llama run -it --rm probe --image=curlimages/curl:8.11.1 --restart=Never -- -fsS http://ollama.llama.svc:11434/api/ps
   kubectl -n llama delete deployment ollama
   kubectl -n llama delete configmap ollama-startup
   kubectl apply -f llama/ollama-jobs.yaml -f llama/ollama-gpu-hold.yaml
   ```

   Keep `llama-model-cache` (PVC) for a week as rollback, then delete it and `llama/pvc.yaml`.

5. **Update the docs that name the pod** — `reference/llm-config.md`, `reference/service-routing.md`,
   `llama/README.md` (done in the same PR), and compendium `INFO-1023`.

## Day-to-day

| Task                          | Command (on timmy unless noted)                                                                                        |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Restart the daemon            | `sudo systemctl restart ollama` (warm re-runs via `PartOf`)                                                            |
| Change an env value           | edit `homelab.conf` in the repo, `sudo llama/host/install-host-ollama.sh`                                              |
| Upgrade Ollama                | bump `OLLAMA_VERSION` in the install script, run it (that is the pin)                                                  |
| Pull a model from the cluster | `kubectl -n llama create job --from=cronjob/ollama-pull pull-$(date +%s)` after setting `MODEL`                        |
| Is the FIM model resident?    | `curl -s 192.168.1.19:11434/api/ps`; `/run/ollama/models-ready` records successful startup warm, not current residency |
| Metrics                       | `curl -s 192.168.1.19:9111/metrics`; Grafana scrapes `ollama.llama.svc:9111`                                           |

Node maintenance: `scripts/node-maintenance.sh --spin-down` no longer scales `llama/ollama`
(there is no Deployment); the daemon rides through drains and stops with the host on reboot.
Its RAM is still counted: the preflight subtracts `HOST_MEMORY_RESERVATIONS` (default
`timmy=16Gi`, the drop-in's `MemoryMax`) from timmy's headroom, so keep the two in sync. To
free that memory for a tight drain, `ssh timmy sudo systemctl stop ollama` by hand first —
the nodes' non-interactive sudo is scoped to apt/reboot, so the script cannot do it for you.

GPU exclusion: the retired pod's `amd.com/gpu: "2"` request was the only thing keeping other
AMD-GPU pods (`gpu/amd/rocm-test-pod.yaml`, the nanochat training Job, the embedding
benchmark) off the card. `llama/ollama-gpu-hold.yaml` restores that guard with a pause
container holding both device-plugin slots; apply it with the Service and jobs. To lend the
GPU to a cluster workload on purpose: `sudo systemctl stop ollama`, scale `ollama-gpu-hold`
to 0, run the workload, reverse both.

## Rollback

Suspend/delete the model CronJobs and finish/delete any active model Jobs first so they
cannot warm the rollback pod unexpectedly. Stop all host units, then restore the saved
ConfigMap and Deployment (initially at zero replicas) and their LoadBalancer Service:

```bash
kubectl -n llama delete cronjob ollama-warm ollama-pull --ignore-not-found
sudo systemctl disable --now ollama-warm ollama-exporter ollama
# Remove the dependency that would otherwise start warmup on a later manual start.
sudo rm /etc/systemd/system/ollama.service.d/homelab.conf
sudo systemctl daemon-reload
kubectl apply -f <snapshot-dir>/configmap.json -f <snapshot-dir>/deployment.json -f <snapshot-dir>/service.json
kubectl -n llama delete endpointslice ollama-timmy-host
kubectl -n llama scale deployment ollama --replicas=1
kubectl -n llama rollout status deployment/ollama --timeout=300s
```

The order is the mirror of the cutover: the svclb bind fails while the host daemon still holds
`0.0.0.0:11434`.
Verify the pod's local FIM, cloud route, metrics, and LoadBalancer access after rollback.
