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

`pihole-exporter.env` (wemby, timmy) also lives only on the hosts. Upstream's
schema includes a Pi-hole password, so it is a secret too — `.gitignore` now
matches it by name, and the live copies are mode `600`.

## ⚠️ This directory is not a complete, self-contained deployment

A clean checkout **cannot** stand these stacks up on its own. `docker compose
config -q` fails from the repo alone. Each host additionally needs, present only
on the host:

| Missing from the repo                     | Needed by    | Why not mirrored                                    |
| ----------------------------------------- | ------------ | --------------------------------------------------- |
| `.env`                                    | all three    | Contains the web UI password                        |
| `pihole-exporter.env`                     | wemby, timmy | Contains a Pi-hole password                         |
| `./unbound/` config dir                   | all three    | Includes `root.key` and per-host tuning             |
| external network `logs`                   | wemby, timmy | Created by another compose project                  |
| external volume `deployments_pihole-data` | wemby, manu  | Pre-existing; compose will not create it (BUG-1079) |

What version control gives you here is **the record set and the pins**, not a
turnkey rebuild. Treat this as configuration-of-record, not a disaster-recovery
artifact, until those gaps are closed.

## Deploying a change

Exercised end to end on 2026-08-24: all three hosts took this rollout and now
serve the records locally. One host at a time, so two resolvers always remain up:

```bash
# from the repo
scp network/pihole/<host>/docker-compose.yml <host>:<compose-dir>/docker-compose.yml
ssh <host> 'cd <compose-dir> && docker compose up -d'
```

ℹ️ **manu needed a one-time fix first.** `noot` was not in its `docker` group
(the group had zero members), so compose could not run. Resolved 2026-08-24 with
`sudo usermod -aG docker noot` plus re-login. Note the `docker` group is
root-equivalent — that is a deliberate posture change, recorded in INFO-1124.

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

Volume-rebuild reproducibility — the criterion Phase 0 exists to satisfy.

🛑 **Run this on timmy and nowhere else.**

`timmy` owns its volume through compose (`pihole_pihole-data`), so compose
recreates it. **wemby and manu declare `pihole-data` as `external: true`**
(`deployments_pihole-data`) — Docker will not create an external volume, so
`docker compose up` **fails** after you remove it and that host stays down until
you recreate the volume by hand and restore its contents. Removing it also
destroys query history and gravity on a host that cannot self-heal.

```bash
# timmy ONLY — during a quiet window, with the other two resolvers serving
ssh timmy 'cd /home/noot/deployments/pihole \
  && docker compose down \
  && docker volume rm pihole_pihole-data \
  && docker compose up -d'
```

Then re-run the invariant check. If the records come back without any manual
step, Phase 0 is done. Budget ~1 hour of resolver cache before judging any
record change from a LAN client — see PROJ-1018 on the cache floor.

To exercise reproducibility on wemby or manu without the trap, back the volume up
first and be ready to restore it:

```bash
ssh <host> 'docker run --rm -v deployments_pihole-data:/src -v /tmp:/dst alpine \
  tar czf /dst/pihole-data-backup.tgz -C /src .'
```

## Related

- `PROJ-1018` — hostname resolution migration; Phase 0 is this directory
- `TASK-1159` — Phases 0/3/4 execution and acceptance criteria
- `INFO-1124` — the resolver trio, versions, and the unversioned-config risk
- `BUG-1078` — pop's public-name queries go to the router's unfiltered IPv6 resolver (ad filtering ~8% effective). Does **not** affect internal names and gates nothing here.
- `pihole-failover-drill.sh` in `../` — failover drill, run from timmy or manu. Its `--stop` path is **untested**; the 2026-08-24 drill was run inline over SSH, not through the script.
- `BUG-1079` — wemby/manu cannot recover from a `pihole-data` volume loss
