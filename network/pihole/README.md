# Pi-hole resolver trio — version-controlled compose

PROJ-1018 Phase 0. These are the three Pi-hole + unbound stacks that serve LAN
DNS. Until 2026-08-24 the compose files existed only on the hosts, alongside
`.bak-20260725` copies, and nothing reproduced them.

## Host map

| Host  | Resolver IP    | Compose path on host                   | Notes                                           |
| ----- | -------------- | -------------------------------------- | ----------------------------------------------- |
| wemby | `192.168.1.9`  | `/home/natew/code/deployments/pihole/` | Different user (`natew`), not `noot`            |
| manu  | `192.168.1.10` | `/home/noot/deployments/pihole/`       | Docker socket unreachable to `noot` — see below |
| timmy | `192.168.1.19` | `/home/noot/deployments/pihole/`       | Control plane; also the target of every record  |

Per-host differences are real, not drift to be normalised away:

- **manu has no `pihole-exporter`**; wemby and timmy do.
- **timmy's `pihole-data` volume is compose-owned** (`pihole_pihole-data`).
  wemby and manu attach a pre-existing external volume,
  `deployments_pihole-data`.

## The private record set

`FTLCONF_dns_hosts` in each `docker-compose.yml`. It is the only mechanism that
survives a deliberate `pihole-data` volume rebuild — the value lives in the
compose file on the host rather than in the volume's `pihole.toml`. Records
added through the web UI or the v6 HTTP API land in the volume and are lost on
rebuild.

**The value must be byte-identical on all three hosts.** Clients pick a resolver
arbitrarily, so a two-of-three rollout produces intermittent failures rather
than an obvious one (INFO-1124's answer-identically invariant).

⚠️ Setting `dns.hosts` via env var makes it **read-only in the web UI**. That is
the intended trade for reproducible-from-git, but a one-off record added through
the UI will silently fail to stick. Edit the compose file instead.

## Secrets

`FTLCONF_webserver_api_password` is **not** in the repo. Each host needs an
untracked `.env` next to its compose file — see `.env.example`. `docker compose`
will refuse to start with a clear error if it is missing.

`pihole-exporter.env` (wemby, timmy) also lives only on the hosts and is not
mirrored here.

## Deploying a change

Not yet exercised end to end — the records were added on 2026-08-24 and have not
been rolled out. Intended procedure, one host at a time so two resolvers always
remain up:

```bash
# from the repo
scp network/pihole/<host>/docker-compose.yml <host>:<compose-dir>/docker-compose.yml
ssh <host> 'cd <compose-dir> && docker compose up -d'
```

⚠️ **manu may not work this way.** Its Docker socket is unreachable to `noot`
(INFO-1124), so `docker compose` there may need `sudo` or a different account.
The Pi-hole v6 HTTP API is the read-only fallback for verification.

## Verifying

Two checks, both required.

Answer-identically invariant — every resolver must agree:

```bash
for ip in 192.168.1.9 192.168.1.10 192.168.1.19; do
  for name in registry k8s longhorn; do
    printf '%s %-9s -> %s\n' "$ip" "$name" \
      "$(dig +short @"$ip" "$name".nathanwhyte.dev | head -1)"
  done
done
```

Volume-rebuild reproducibility — the criterion Phase 0 exists to satisfy. On
**one** host only, during a quiet window, with the other two serving:

```bash
ssh <host> 'cd <compose-dir> && docker compose down && docker volume rm <volume> && docker compose up -d'
```

Then re-run the invariant check. If the records come back without any manual
step, Phase 0 is done. Budget ~1 hour of resolver cache before judging any
record change from a LAN client — see PROJ-1018 on the cache floor.

## Related

- `PROJ-1018` — hostname resolution migration; Phase 0 is this directory
- `TASK-1159` — Phases 0/3/4 execution and acceptance criteria
- `INFO-1124` — the resolver trio, versions, and the unversioned-config risk
- `BUG-1078` — pop is served by the router's IPv6 resolver, not these; gates Phases 3–5
- `pihole-failover-drill.sh` in `../` — failover drill, run from timmy or manu
