# k3s datastore restore drill — 2026-09-05

First restore ever attempted against the `k3s-datastore-backup` CronJob shipped by IMPR-1021 on
2026-07-02. Run under IMPR-1126. Executed entirely on **pop** (macOS, Docker Desktop 29.7.2,
aarch64). The live cluster was touched read-only: a `kubectl port-forward` to Garage, `kubectl get`,
and one `kubectl apply --dry-run=server`. Nothing was applied, restarted, or written.

## Verdict

**The snapshots restore.** A k3s server built from `state-2026-09-05.db` + `token-2026-09-05` came
up and served the full cluster state in **8 seconds**.

**But the restore procedure that was written down does not work.** The comment block in
`k3s-datastore-backup.yaml` said "restore state.db to `/var/lib/rancher/k3s/server/db/`, restore
token + `tls/`, start k3s". Following it verbatim produces a k3s that starts the apiserver and then
dies. The `tls/` tarball is the thing that breaks it. That is now corrected in the manifest.

## What was restored

| Item                 | Value                                                                               |
| -------------------- | ----------------------------------------------------------------------------------- |
| Object               | `garage:k3s-backups/timmy/state-2026-09-05.db`                                      |
| Object timestamp     | 2026-09-05 03:30:01.090 CDT                                                         |
| Size                 | 129,470,464 bytes (123.5 MiB)                                                       |
| MD5                  | `981ed195f1fecc6cda031555ae3f473d` — matched after download                         |
| Companions           | `token-2026-09-05` (109 B), `tls-2026-09-05.tar.gz` (15,714 B)                      |
| Bucket at drill time | 39 objects, 1.517 GiB; Garage 99.7% free on all three nodes                         |
| Download             | 2 s (~59 MiB/s) via `kubectl -n garage port-forward svc/garage 13900:3900` + rclone |
| Restore target       | `rancher/k3s:v1.36.2-k3s1` (arm64) in Docker, matching cluster `v1.36.2+k3s1`       |

## Offline integrity, before any restore

```text
PRAGMA integrity_check          -> ok   (<0.1 s)
kine rows                       -> 4641
max revision                    -> 67081768
distinct keys / live / tombstone-> 2562 / 2562 / 0
newest revision written         -> /registry/pods/kube-system/k3s-datastore-backup-29809950-pqkp4
```

The newest revision in the snapshot is the backup job's own pod — the snapshot captures the instant
the job ran, as designed.

## Run 1 — the documented procedure. FAILS.

Staged `state.db` + `token` + the extracted `tls/` into a disposable volume, started k3s.

The apiserver came up on the restored data — it logged `Kube API server is now running` and
`k3s is up and running` about 6 s in — and then, in the same second:

```text
level=fatal msg="failed to start controllers: failed to create new server context:
stat /var/lib/rancher/k3s/server/cred/supervisor.kubeconfig: no such file or directory"
```

Cause, confirmed by inspecting the volume afterwards: the backup covers `db/`, `token` and `tls/`
but **not** `server/cred/`. That would be survivable on its own, because k3s regenerates the
kubeconfigs — except it only regenerates them _as a side effect of regenerating the matching leaf
client certificate_. Restoring a complete `tls/` makes every leaf cert already-present, so nothing
is regenerated and no kubeconfig is ever written.

The evidence is in the `cred/` directory after the failed start:

```text
-rw------- 1 root root  97 Dec 11  2025 ipsec.psk     <- rehydrated from state.db bootstrap blob
-rw------- 1 root root 111 Dec 11  2025 passwd        <- rehydrated from state.db bootstrap blob
(no *.kubeconfig)
```

`ipsec.psk` and `passwd` arrived with their original December 2025 mtimes, so bootstrap
rehydration from the datastore worked. The kubeconfigs are not part of that blob and were never
generated. This failure mode is nasty because the apiserver comes up first — it looks like a
datastore problem when it is a certificate-side-effect problem.

## Run 2 — `state.db` + `token` only. WORKS.

Fresh volume, only `db/state.db` and `token`, no `tls/`. k3s reconciled the bootstrap blob out of
the datastore, regenerated the leaf certs (and therefore the kubeconfigs), and served.

```text
container start   2026-09-05T15:25:50Z
kubectl get ns    2026-09-05T15:25:58Z   -> 8 seconds
```

Verified against the restored apiserver:

| Check                                                    | Result                                                                                             |
| -------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| Namespaces                                               | 27 — identical set to the live cluster                                                             |
| Nodes                                                    | manu, timmy (control-plane), wemby — all `v1.36.2+k3s1`                                            |
| Known Deployment `grafana/k8s-monitoring-alloy-operator` | `1/1`, age 166d, image `ghcr.io/grafana/alloy-operator:1.6.2`                                      |
| Known Secret `kube-system/k3s-backup-s3-credentials`     | decoded `ACCESS_KEY_ID` = `GKdb43099d…`, which is the real Garage key `GKdb43099de4c42883526d99f9` |
| Inventory                                                | 58 deployments, 100 CRDs, 11 statefulsets, 33 PVCs, 154 secrets, 130 configmaps                    |
| `cred/`                                                  | all 6 `*.kubeconfig` written at restore time                                                       |

The Secret check is the strongest single result: a Secret's payload was carried through the
snapshot, the S3 round trip and the restore, and decodes to a credential whose correctness is
independently verifiable against Garage's own key listing.

## Cluster identity survives — the part that makes this a real mitigation

The CA material k3s rehydrated from `state.db` is **byte-identical** to the material in the
`tls-2026-09-05.tar.gz` tarball:

| File                    | backup vs restored |
| ----------------------- | ------------------ |
| `server-ca.crt`         | identical          |
| `server-ca.key`         | identical          |
| `client-ca.crt`         | identical          |
| `client-ca.key`         | identical          |
| `request-header-ca.crt` | identical          |
| `service.key`           | identical          |

14 of 47 files under `tls/` are identical (the CA/key material); the other 33 are leaf certificates
and per-run temporaries that k3s regenerated, all signed by the same CAs. Because `service.key` —
the ServiceAccount token signing key — is unchanged, **existing ServiceAccount tokens stay valid**,
and because the CAs are unchanged, **existing agent nodes can rejoin without re-bootstrapping**.
The node join token's `K108deb04ad1a1…` prefix is the `server-ca.crt` hash, and it matches.

This is what upgrades the backup from "the data is there" to "the cluster comes back".

## Consequence for the manifest

`tls-<date>.tar.gz` is **redundant for restore** — every byte of CA material in it already lives
inside `state.db`'s bootstrap blob, recoverable with the token alone. It stays in the backup as an
independent ~15 KB/night copy for the case where the datastore itself is unreadable, but it is no
longer an input to the normal restore path, and restoring it is now explicitly warned against.

## Workarounds and gotchas hit during the drill

| Gotcha                                            | Handling                                                                                                                                                                                        |
| ------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Restoring `tls/` suppresses kubeconfig generation | Do not restore `tls/`. This is the headline finding.                                                                                                                                            |
| `rancher/k3s` image ships no `openssl`            | Compared certificates by SHA-256 of the files instead of fingerprints.                                                                                                                          |
| Cluster is amd64, pop is arm64                    | Irrelevant — SQLite datastore and PEM material are architecture-neutral.                                                                                                                        |
| Garage S3 is cluster-internal only                | `kubectl port-forward svc/garage 13900:3900`, rclone at `127.0.0.1:13900`.                                                                                                                      |
| k3s in Docker wants an agent                      | `--disable-agent` plus disabling traefik/servicelb/metrics-server/local-storage/cloud-controller/network-policy/helm-controller. Control plane only is enough to prove datastore restorability. |

## Reproducing this drill

Nothing here needs the live cluster beyond read access to Garage.

1. `kubectl -n garage port-forward svc/garage 13900:3900`
2. Point rclone at `http://127.0.0.1:13900` with the `k3s-backup-s3-credentials` key, path style on.
3. `rclone copy garage:k3s-backups/timmy/state-<date>.db .` and the matching `token-<date>`.
4. Stage `state.db` into `<vol>/server/db/state.db` and `token` into `<vol>/server/token` (mode 600).
   **Do not stage `tls/`.**
5. `docker run -d --privileged --tmpfs /run --tmpfs /var/run -v <vol>:/var/lib/rancher/k3s
rancher/k3s:v<version> server --disable-agent --disable=traefik,servicelb,metrics-server,local-storage
--disable-cloud-controller --disable-network-policy --disable-helm-controller`
6. `docker exec <ctr> kubectl get ns` — should list the cluster's namespaces within ~10 s.
7. Delete the container and the volume. The drill leaves no cluster-side state.

Re-run this after any k3s minor upgrade; the failure mode found here was a property of how the
running k3s version reconciles certs, not of the snapshot format.
