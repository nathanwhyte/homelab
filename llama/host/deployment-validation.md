# Host Ollama deployment validation

Reviewed and deployed from PR #77 on 2026-09-09. Ollama 0.33.3 now runs once,
under systemd on timmy. The old Kubernetes Deployment and startup ConfigMap are
removed; the Longhorn model PVC is retained for rollback.

## Findings addressed

| Severity | Finding                                                                                                                             | Correction                                                                                                                                      |
| -------- | ----------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| P1       | Starting host warmup before stopping the pod puts two runners on a GPU already holding about 13 GB.                                 | Copy first, stop the pod and wait for deletion, then start the host. Installer refuses a remaining Ollama pod.                                  |
| P1       | `ss` does not expose ServiceLB's hostPort DNAT. The original guard passed with the old route still active.                          | Check Service, ServiceLB pods, EndpointSlices, and NAT before any install/restart. Copy mode exits without service changes.                     |
| P1       | Removing the Service selector leaves the legacy Endpoints and controller slices; a mirroring controller recreates stale pod routes. | Delete legacy Endpoints and both old slice types. Require exactly the host slice before activation.                                             |
| P1       | Copying models omits the daemon's cloud sign-in identity. The live host returned cloud 401s.                                        | Add a separate identity copy with the daemon stopped and a private backup of the existing host identity. Authenticated cloud calls then passed. |
| P2       | `PartOf` alone does not start an inactive warm unit; warm failures were swallowed.                                                  | Add a daemon `Wants` dependency and propagate preparation failure to the warm unit. API readiness stays independent.                            |
| P2       | The scheduled warm job could evict another resident model, and pull transport failures could be hidden by a pipeline.               | Skip warm when any model is resident; check non-streaming pull completion and transport status.                                                 |
| P2       | Host migration dropped the pod's memory/CPU limits and did not reconcile an existing exporter unit.                                 | Retain 16 GiB / 8 CPU for Ollama and 128 MiB / 0.2 CPU for the exporter; reconcile exporter as user `ollama`.                                   |

## Evidence

| Check             | Result                                                                                                                                                                                                                   |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Local validation  | Shellcheck, shell syntax, diff whitespace, repository pre-commit checks, and server-side manifest dry-run passed. Installed units passed `systemd-analyze verify`.                                                       |
| Unsafe-state test | Installer exited 1 while the Service was still LoadBalancer, before changing units or restarting.                                                                                                                        |
| Model copy        | 64,377,387,436 bytes copied. All 19 source model names and digests matched the host inventory. Host service PID and unit hashes stayed unchanged during copying.                                                         |
| GPU/backend       | Startup journal names `library=Vulkan`, RX 9070 XT / RADV GFX1201 at PCI 03:00.0, and 28/28 layers offloaded.                                                                                                            |
| Residency         | `deepseek-coder-v2:fim`, digest prefix `ed9a6ec49200`, 100% GPU, 16,384 context, indefinite residency. Agentpair tags remain unwarmed.                                                                                   |
| FIM behavior      | A suffix completion for `def add(a, b):` returned `a + b` (4 generated tokens). This is a functional smoke check, not a throughput benchmark.                                                                           |
| Routing           | Five version requests each passed through cluster DNS, LAN, and Tailscale from the consumer pod. The ClusterIP answered from all three nodes, and LAN/Tailscale answered from the Mac. Only `ollama-timmy-host` remains. |
| Cloud consumer    | The production chat proxy, using the existing API credential, returned `OK` from `gemma4:31b-cloud` after identity migration.                                                                                            |
| Model Jobs        | A one-off warm Job succeeded without evicting the resident model. A one-off pull of the existing base model returned `status: success`. Temporary Jobs were removed.                                                     |
| Restart behavior  | Re-running the installer restarted the daemon and ran the warm unit successfully. One serving daemon remains.                                                                                                            |
| Monitoring        | Prometheus target `http://ollama.llama.svc:9111/metrics` is up with no scrape error; `ollama_up=1`, `ollama_models_loaded=1`, context metric 16384.                                                                      |
| Resource limits   | Kernel cgroup values confirmed `memory.max=17179869184` and `cpu.max=800000 100000` for Ollama.                                                                                                                          |

The initial pre-cutover loopback cloud probe was not valid evidence of host
authentication: ServiceLB DNAT could redirect it to the pod. Cloud authentication
was verified again after removing the old routing and migrating the identity.

Rollback snapshots are private on timmy at `/root/ollama-pr77-backup-20260909`.
The Deployment snapshot pins both previously running image digests and starts at
zero replicas. The old host identity is preserved under
`/var/backups/ollama-identity.WTKbvP`. Follow the ordered rollback in the README;
the PVC has not been deleted. No node reboot, k3s restart, sustained workload
benchmark, or destructive rollback drill was performed.
