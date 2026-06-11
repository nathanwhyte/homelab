# Harbor CLI (`harbor-cli`) Reference

`harbor-cli` is the official command-line companion to Harbor. It covers ~30 top-level commands / 167 subcommands — most workflows that today are done via the web UI, `kubectl exec` into the core/database pods, or ad-hoc `curl` to `/api/v2.0/...`. **Optional, recommended for any operator who touches Harbor more than once a month.** Verified on Harbor 2.14.3; should be forward-compatible to 2.15.x.

## Install

```bash
# MacBook (Homebrew — recommended)
brew install harbor-cli

# Or grab a release binary
# https://github.com/goharbor/harbor-cli/releases

# Verify
harbor-cli --version    # expect v0.0.22 or later
```

## Auth

```bash
# Username + password
harbor-cli login https://registry.nathanwhyte.dev \
    --name admin \
    --password "$HARBOR_ADMIN_PASSWORD"

# The credentials are stored in ~/.harbor-cli/harbor-cli.yaml.
# Or use a robot account for automation (see below).
```

## Top workflows (operator essentials)

| Workflow | Command |
|---|---|
| List projects | `harbor-cli project list` |
| Create a project | `harbor-cli project create --name myproject --public` |
| Toggle scan-on-push | `harbor-cli project update --name myproject --scan-on-push=true` |
| List repos in a project | `harbor-cli repo list --project myproject` |
| Show tags | `harbor-cli tag list --project myproject --repo myapp` |
| Trigger a scan | `harbor-cli scan start --project myproject --repo myapp --tag latest` |
| List vulnerabilities for an artifact | `harbor-cli vuln list --project myproject --repo myapp --tag latest` |
| Robot accounts (CI push/pull) | `harbor-cli robot create --name ci-deployer --project myproject --duration 90` |
| List robot accounts | `harbor-cli robot list` |
| Retention policy | `harbor-cli retention create --project myproject --template retention-template.json` |
| Garbage collection jobs | `harbor-cli gc list` ; `harbor-cli gc create --schedule 0 2 * * *` |
| Audit log (last 24h) | `harbor-cli auditlog list --query 'Begin=2026-06-09T00:00:00 End=2026-06-10T00:00:00'` |
| Job service status | `harbor-cli jobservice list` |
| Show Harbor version + components | `harbor-cli version` ; `harbor-cli health` |

For the full surface, see `harbor-cli <command> --help` and the upstream [CLI docs](https://goharbor.io/cli-docs/).

## Robot accounts for CI / agents

A robot account is a non-human Harbor identity scoped to one project. Use it for hermes-agent, build pipelines, or any non-interactive push/pull.

```bash
# Create (90-day expiry; cap permissions to the project)
harbor-cli robot create \
    --name hermes-agent-pusher \
    --project hermes-agent \
    --action push,pull \
    --duration 90

# Returns:
#   name:    robot$hermes-agent+hermes-agent-pusher
#   secret:  <long-token>
#
# Store the secret in a K8s Secret:
kubectl create secret generic harbor-robot-hermes-agent \
    --namespace hermes \
    --from-literal=username='robot$hermes-agent+hermes-agent-pusher' \
    --from-literal=password='<long-token>' \
    --dry-run=client -o yaml | kubectl apply -f -
```

Use it like a regular user:

```bash
docker login registry.nathanwhyte.dev \
    -u 'robot$hermes-agent+hermes-agent-pusher' \
    -p '<long-token>'
```

## Audit: who else pushes to this Harbor?

The in-repo docs claim Harbor is "for hermes-agent, ov-vectordb, and other cluster-built images." Worth verifying with a grep:

```bash
# Searches the homelab repo (and personal-compendium, since IDs are sometimes hardcoded there too)
grep -rn 'registry\.nathanwhyte\.dev' ~/code/ ~/code/personal-compendium/ \
    --include='*.yaml' --include='*.yml' --include='*.sh' --include='*.md' \
    2>/dev/null
```

Likely consumers: hermes-agent (image pull), ov-vectordb (image pull), nothing else by default. Anything else is a real consumer to log in `HARBOR.md`'s "External consumers" row.

## See also

- `harbor/HARBOR.md` — one-page index
- `harbor/HARBOR-RUNBOOK.md` — operational procedures
- [goharbor.io/cli-docs](https://goharbor.io/cli-docs/) — full CLI reference
- [goharbor/harbor-cli](https://github.com/goharbor/harbor-cli) — source
