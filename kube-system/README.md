# kube-system

Cluster-level k3s pieces that are not owned by an application namespace.

| File | What |
| --- | --- |
| `crictl-image-prune.yaml` | DaemonSet that prunes unused container images on each node |
| `k3s-datastore-backup.yaml` | CronJob that backs the k3s datastore up to S3 |
| `k3s-backup-s3-credentials.secret.yaml.example` | Template for the backup job's credentials |
| `k3s-datastore-restore-drill-2026-09-05.md` | Restore drill record |
| `pin-k3s-node-ip.sh` | Host script: pin a node's `node-ip` to its LAN IPv4 address (BUG-1152) |

## Pinning `node-ip` (BUG-1152)

The nodes were installed by hand, so k3s startup flags live in each host's
`/etc/rancher/k3s/config.yaml` rather than anywhere in this repo. With no
`node-ip` set, k3s detects the node's addresses **once at startup** and
registers what it finds, including the DHCPv6 address from the ISP's delegated
prefix. When that prefix rotates, the host drops the old address while k3s keeps
announcing it, and kubelet logs this every ~10 seconds until the unit restarts:

```text
"Failed to set some node status fields" err="failed to validate secondaryNodeIP:
 node IP: \"2605:a601:9c2d:ee00::2\" not found in the host's network interfaces"
```

Nothing consumes the IPv6 address — the pod network is IPv4-only
(`podCIDR 10.42.0.0/24`) — so pinning IPv4 costs nothing and makes prefix
rotations a non-event.

Run the script **on each node**, agents first and the server last, because
restarting the server briefly interrupts the API:

```bash
# on wemby, then manu (agents)
./kube-system/pin-k3s-node-ip.sh --dry-run
./kube-system/pin-k3s-node-ip.sh

# on timmy (server) last
./kube-system/pin-k3s-node-ip.sh
```

The script detects the unit (`k3s` or `k3s-agent`), derives the `192.168.1.x`
address and refuses anything else, backs up `config.yaml`, restarts the unit,
then prints the registered addresses and the `secondaryNodeIP` error count from
the following minute. Re-running with the same address reports `already pinned`
and does not restart k3s.

Afterwards each node should show one `InternalIP` and no IPv6:

```bash
kubectl get node <node> -o jsonpath='{.status.addresses}'
kubectl get node <node> -o jsonpath='{.metadata.annotations.k3s\.io/internal-ip}'
```

manu registers no IPv6 address today (it has no IPv6 default route, see
BUG-1101), so pinning there is for uniformity rather than a fix.
