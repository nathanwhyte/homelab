# PVC capacity auto-resolver

The capacity auto-resolver is an authenticated Alertmanager webhook for genuine
filesystem pressure. It accepts only `KubePersistentVolumeFillingUp`; it never
expands from `LonghornVolumeSpaceHigh`, because Longhorn allocated bytes include
deleted-but-undiscarded blocks and do not represent live filesystem usage.

The initial rollout is deliberately incapable of mutation:

- `policy.json` is in `dry-run` mode.
- `CAPACITY_RESOLVER_MUTATION_ENABLED` is `false` in `manifests.yaml`.
- The exact allowlist contains only `viking/llama-cuda-model-cache`.
- A required deny-list (Prometheus, Loki, media, databases, object storage,
  OpenViking data) is checked first, and a policy that allowlists a denied PVC
  refuses to load.
- That PVC can grow by 4Gi at a time and never beyond 20Gi.

Both the policy mode and the independent Deployment switch must be changed in a
reviewed commit before a PVC patch can occur.

## Decision gates

For each firing alert, the resolver evaluates these gates in order:

1. The alert name is exactly `KubePersistentVolumeFillingUp`.
2. The namespace/PVC tuple matches no deny-list rule, and then appears in the
   static allowlist.
3. A fresh Prometheus query reports at least 85% live filesystem use.
4. The PVC is Bound and no earlier resize is still converging.
5. The cooldown annotation is absent or at least 24 hours old.
6. The expected Longhorn StorageClass supports expansion.
7. The Longhorn volume is attached and healthy, and its size matches the PVC.
8. Every expected replica and its current node/disk placement can be resolved.
9. Every replica disk is Ready and Schedulable.
10. Projected free space remains above both 20Gi and 25% of disk capacity.
11. Projected scheduled capacity remains at or below 80% of disk capacity.
12. The proposed target does not exceed the per-PVC hard cap.

Outcomes are `ignore`, `refuse`, `would-expand`, and `expand`. Relevant refusal,
dry-run, proposed, and completed decisions are sent to `#cron-homelab` with the
PVC, Longhorn volume, measured use, old/new/cap sizes, and disk-headroom result.
Active mode fails closed if the pre-mutation Slack notification cannot be sent.

Successful writes add persistent annotations to the PVC with the UTC action
time, previous and target sizes, Alertmanager fingerprint and `startsAt` episode
identity, and completion-audit state. RFC 6902 `test` operations verify both the
current PVC `resourceVersion` and old storage request immediately before
`replace`, so a concurrent update fails rather than racing. If the PVC patch
succeeds but the completion Slack request fails, the PVC keeps a `pending`
marker. Two paths can finish that audit notification without authorizing a
second expansion: a replay of the same alert, and a background reconciler that
checks the allowlisted PVCs at startup and every
`CAPACITY_RESOLVER_AUDIT_RECONCILE_SECONDS` (default 300). The reconciler is
needed because an expansion usually resolves its alert, so no replay may ever
arrive. Completing the audit is itself a PVC patch, so, like an expansion, it
requires both `mode: active` and the mutation switch. A `Recreate` Deployment
prevents overlapping webhook consumers during rollout.

## Secrets

Audit messages go through the existing `grafana/alertmanager-slack-bot-token`
Secret (`api-url`, `bot-token`): the same `chat.postMessage` transport and
`#cron-homelab` channel as the `slack-homelab` receiver (IMPR-1173). Create a
separate random bearer token shared by Alertmanager and the resolver:

```bash
kubectl -n grafana create secret generic capacity-auto-resolver-token \
  --from-literal=token="$(openssl rand -hex 32)"
```

The token value must not be committed.

Create this Secret **before** the next real run of
`longhorn/deploy-storage-alerts.sh`: that script now deploys the resolver first
and stops if the token is missing. That is deliberate. The storage-alert route
reads the same token, and without it the AlertmanagerConfig passes server
dry-run but its receiver is never built (the same reason IMPR-1173's Slack
guard fails fast). A `--dry-run` run does not check Secrets.

## Validate and deploy

Run unit and webhook tests:

```bash
cd grafana/capacity-auto-resolver
python3 -m unittest -v test_resolver.py
```

Validate all objects through the live API without changing cluster state:

```bash
grafana/capacity-auto-resolver/deploy.sh --dry-run
longhorn/deploy-storage-alerts.sh --dry-run
```

`longhorn/deploy-storage-alerts.sh` deploys the resolver before applying the
Alertmanager route, so Alertmanager is never pointed at an absent Service. A
normal deployment requires both Secrets and remains dry-run-only under the
committed policy and Deployment switch.

## RBAC boundary

The ServiceAccount can:

- get and patch only `viking/llama-cuda-model-cache`;
- get only the `longhorn-ssd` StorageClass;
- read Longhorn volumes, replica placements, and nodes in `longhorn-system`.

It cannot create or delete PVCs, mutate Longhorn resources, read Secrets, or
patch any other PVC. Verify the boundary after deployment:

```bash
kubectl auth can-i --as=system:serviceaccount:grafana:capacity-auto-resolver \
  get pvc/llama-cuda-model-cache -n viking
kubectl auth can-i --as=system:serviceaccount:grafana:capacity-auto-resolver \
  patch pvc/llama-cuda-model-cache -n viking
kubectl auth can-i --as=system:serviceaccount:grafana:capacity-auto-resolver \
  patch pvc/embedder-cuda-model-cache -n viking
kubectl auth can-i --as=system:serviceaccount:grafana:capacity-auto-resolver \
  patch volumes.longhorn.io -n longhorn-system
kubectl auth can-i --as=system:serviceaccount:grafana:capacity-auto-resolver \
  get secrets -n grafana
```

Expected results are `yes`, `yes`, `no`, `no`, and `no`.

## Enabling mutation later

Do not enable mutation until dry-run decisions have been observed and reviewed.
A separate reviewed change must set `mode` to `active` in `policy.json` and set
`CAPACITY_RESOLVER_MUTATION_ENABLED` to `"true"` in `manifests.yaml`. Expand the
allowlist only together with an equally narrow PVC Role rule and a documented
hard cap.

Rollback is immediate and non-destructive: remove the resolver child route from
`storage-alert-routing.yaml` or set either activation gate back to its safe
value. Already-expanded PVCs cannot be shrunk.
