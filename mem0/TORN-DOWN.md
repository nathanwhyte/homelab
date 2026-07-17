# Torn down 2026-07-02

The `mem0` namespace was deleted (server, dashboard, exporter, Postgres) and
the `mem0`/`api-mem0.nathanwhyte.dev` tunnel routes removed. A final
`pg_dumpall` (58 MB) is archived at `backups/2026-07-02-mem0-teardown/`
(gitignored, local only). Hermes — the sole mem0 consumer — was already not
deployed live (see IMPR-1025). Manifests retained for reference; restoring
requires re-applying the stack and loading the dump. Orphaned DNS records
(`mem0`, `api-mem0`) still need deleting in the Cloudflare dashboard.

Hermes itself was retired 2026-07-16 (see [`../hermes/RETIRED.md`](../hermes/RETIRED.md))
rather than migrated to a new memory provider — OpenViking's knowledge-base
tooling has since covered the persistent-memory use case both systems targeted.
