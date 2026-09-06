# Migrating the NVIDIA driver from the GPU Operator to the host (BUG-1102)

Closes the cause behind [BUG-1102]: the GPU Operator rebuilds the NVIDIA kernel
module **from the network on every driver container start**. There is no host
driver and no dkms to fall back on, so a node that reboots while Canonical's
archive is unreachable comes back with `nvidia.com/gpu: 0`. On 2026-09-05 manu
took 20 restarts before one build got through, and the GPU was absent for an
hour while `embedder-qwen-cuda` — pinned to manu by nodeSelector — sat Pending
and every `ov find` returned `[INTERNAL]`.

Installing the driver on the host with dkms removes the boot-time network
dependency entirely: dkms rebuilds the module locally on kernel upgrade, and a
plain reboot loads an already-built module with no network at all.

## Preconditions — all verified 2026-09-05

| Check                                | manu                          | wemby                         |
| ------------------------------------ | ----------------------------- | ----------------------------- |
| OS                                   | noble                         | noble                         |
| Running kernel                       | `6.17.0-35-generic`           | `6.8.0-139-generic`           |
| Kernels in `/boot`                   | only `6.17.0-35`              | `6.8.0-138`, `6.8.0-139`      |
| **Secure Boot**                      | **disabled**                  | **disabled**                  |
| Headers for running kernel           | installed                     | installed                     |
| `dkms`                               | not installed                 | not installed                 |
| `nvidia-driver-580-server` candidate | `580.173.02-0ubuntu0.24.04.1` | `580.173.02-0ubuntu0.24.04.1` |

Two things this table settles:

- **Secure Boot is off on both**, so dkms-built modules load without MOK
  enrollment. If Secure Boot is ever turned on, this procedure needs a signing
  step and the modules will silently fail to load until it is added.
- **The same driver version is available on both nodes** despite different kernel
  series, so the fleet ends up consistent.

It also corrects an earlier claim in BUG-1102 that manu had `7.0.0-28-generic`
installed and would boot into a kernel the driver had never seen. `/boot` on manu
contains only `6.17.0-35-generic`. That hazard does not exist.

**The nodes use different kernel series** (6.17 vs 6.8), which is exactly why
dkms is the right mechanism rather than a precompiled image — dkms builds against
whatever kernel each node is actually running.

## Order of operations, and why

**One node at a time, manu first.** Never both at once: each node's driver
container is unloaded before its host install, so doing both simultaneously drops
the cluster to zero GPUs. manu goes first because `embedder-qwen-cuda` is pinned
there and OpenViking retrieval depends on it.

Per node:

1. **Stop the operator's driver on that node only.**

   ```bash
   kubectl label node <NODE> nvidia.com/gpu.deploy.driver=false --overwrite
   kubectl -n gpu-operator wait --for=delete pod -l app=nvidia-driver-daemonset \
     --field-selector spec.nodeName=<NODE> --timeout=300s
   ```

   The container unloads its modules on exit. The node drops to
   `nvidia.com/gpu: 0` and GPU workloads there go Pending — expected, and the
   window this whole procedure exists to make permanent-proof.

2. **Install on the host** (needs sudo; run it on the node):

   ```bash
   sudo apt-get update
   sudo apt-get install -y dkms linux-headers-$(uname -r) nvidia-driver-580-server
   ```

   `nvidia-driver-580-server` pulls `nvidia-dkms-580-server`, which registers the
   module with dkms and builds it for the running kernel.

3. **Verify before moving on.** All four must pass:

   ```bash
   nvidia-smi                      # lists the GPU
   dkms status                     # nvidia/580.173.02: ... installed
   lsmod | grep nvidia             # nvidia, nvidia_uvm, nvidia_modeset present
   modinfo nvidia | head -3        # resolves from /lib/modules, not /run/nvidia
   ```

   `modinfo` resolving outside `/run/nvidia/driver` is the one that proves the
   host module — not the container's — is in play.

4. **Confirm the cluster sees it**: `kubectl get node <NODE> -o jsonpath=
'{.status.capacity.nvidia\.com/gpu}'` returns `1`. The device plugin keeps
   running throughout; it only needed a module to find.

5. Repeat 1–4 for the second node.

## Final step — take the operator out of the driver business

Only after **both** nodes pass step 3. In `gpu/nvidia/values.yaml`:

```yaml
driver:
  enabled: false # BUG-1102: driver is host-installed via dkms
```

Then `./gpu/nvidia/deploy-nvidia-gpu.sh`, and confirm
`nvidia-driver-daemonset` is gone while both nodes still report
`nvidia.com/gpu: 1`.

Clear the per-node labels afterwards so they do not shadow the global setting:

```bash
kubectl label node manu wemby nvidia.com/gpu.deploy.driver- --overwrite
```

## Verifying the thing this actually fixes

The point is reboot survival, so test it rather than assume it. On one node,
after the migration:

```bash
sudo reboot
# then, from pop:
kubectl get node <NODE> -o jsonpath='{.status.capacity.nvidia\.com/gpu}{"\n"}'
```

It should read `1` as soon as the kubelet registers, with no driver container and
no network fetch involved. That is the difference from today, where the same
reboot started a 20-restart lottery.

The `NvidiaGpuCapacityLost` alert from `gpu-alerts.yaml` stays useful afterwards —
it is keyed on capacity, not on the operator, so it still fires if a host driver
fails to load after a kernel upgrade.

## Rollback

Reinstate the operator-managed driver:

```bash
kubectl label node <NODE> nvidia.com/gpu.deploy.driver=true --overwrite
# and revert driver.enabled to true in values.yaml, then re-run deploy-nvidia-gpu.sh
```

Optionally remove the host packages (`sudo apt-get remove --purge
'nvidia-driver-580-server' 'nvidia-dkms-580-server'`). Rolling back returns you to
the fragility this migration exists to remove, so prefer fixing forward.

## Known drift to reconcile

`gpu/nvidia/values.yaml` pins `driver.version: "580.105.08"` and the live
ClusterPolicy agrees, but the running container image is
`nvcr.io/nvidia/driver:580.126.20-ubuntu24.04`. Once `driver.enabled: false`
lands, that stanza stops being live and the drift becomes moot — but if this
migration is ever rolled back, reconcile it first so the rollback lands on a
known version.

[BUG-1102]: manu's GPU does not survive a reboot — driver container cannot reach archive.ubuntu.com to rebuild the kernel module
