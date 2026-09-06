# Harbor CLI (`harbor`) Reference

`harbor` (package / source repo: `harbor-cli`) is the official command-line companion to Harbor. The binary installs as `harbor`, not `harbor-cli` — a common gotcha. It covers ~30 top-level commands / 167 subcommands — most workflows that today are done via the web UI, `kubectl exec` into the core/database pods, or ad-hoc `curl` to `/api/v2.0/...`. **Optional, recommended for any operator who touches Harbor more than once a month.** Verified on Harbor 2.15.1; binary v0.0.22 (via `brew install harbor-cli`).

## Install

```bash
# MacBook (Homebrew — recommended)
brew install harbor-cli

# Or grab a release binary
# https://github.com/goharbor/harbor-cli/releases

# Verify
harbor --version    # expect v0.0.22 or later
```

## Auth

```bash
# Username + password (binary is `harbor`, not `harbor-cli`)
harbor login https://registry.nathanwhyte.dev \
    --username admin \
    --password "$HARBOR_ADMIN_PASSWORD"

# Or use --password-stdin
echo "$HARBOR_ADMIN_PASSWORD" | harbor login https://registry.nathanwhyte.dev \
    --username admin --password-stdin

# The credentials are stored in ~/.config/harbor-cli/config.yaml.
# Or use a robot account for automation (see below).
```

## Top workflows (operator essentials)

| Workflow | Command |
|---|---|
| List projects | `harbor project list` |
| Create a project | `harbor project create --name myproject --public` |
| Toggle scan-on-push | `harbor project update --name myproject --scan-on-push=true` |
| List repos in a project | `harbor repo list --project myproject` |
| Show tags | `harbor tag list --project myproject --repo myapp` |
| Trigger a scan | `harbor scan start --project myproject --repo myapp --tag latest` |
| List vulnerabilities for an artifact | `harbor vuln list --project myproject --repo myapp --tag latest` |
| Robot accounts (CI push/pull) | `harbor robot create --name ci-deployer --project myproject --duration 90` |
| List robot accounts | `harbor robot list` |
| Retention policy | `harbor retention create --project myproject --template retention-template.json` |
| Garbage collection jobs | `harbor gc list` ; `harbor gc create --schedule 0 2 * * *` |
| Audit log (last 24h) | `harbor auditlog list --query 'Begin=2026-06-09T00:00:00 End=2026-06-10T00:00:00'` |
| Job service status | `harbor jobservice list` |
| Show Harbor version + components | `harbor version` ; `harbor health` |

For the full surface, see `harbor <command> --help` and the upstream [CLI docs](https://goharbor.io/cli-docs/).

## Robot accounts for CI / agents

A robot account is a non-human Harbor identity scoped to one project. Use it for hermes-agent, build pipelines, or any non-interactive push/pull.

```bash
# Create (90-day expiry; cap permissions to the project)
harbor robot create \
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

**Verified 2026-06-10** (results now in `HARBOR.md`'s "External consumers" row). Each pull namespace maps to a project, but only some have their source repos on this machine.

| Harbor image | Cluster ns | Source repo (GitHub) | Stack | Local path |
|---|---|---|---|---|
| `build-hook/api` | `build` | [`nathanwhyte/build-hook`](https://github.com/nathanwhyte/build-hook) (public, Rust) | Static binary; `Cmd: ['build-hook']`, WorkingDir `/app` | **not on this machine** — only the deployment manifest in `~/code/dotfiles/hermes/k8s/manifests.yaml` |
| `coach/coach` | `coach` | [`nathanwhyte/credit-coach`](https://github.com/nathanwhyte/credit-coach) (private, Python — actually Bun+TS) | Bun frontend; `Cmd: ['bun','./server.js']`, `NODE_ENV=production`, user `nextjs` | **not on this machine** — deployment manifests only |
| `coach/scrub` (`release`) | `coach` | `nathanwhyte/credit-coach` (same repo, sidecar) | Python FastAPI; `Cmd: ['/app/.venv/bin/fastapi','run','app/main.py']` | **not on this machine** |
| `equal-risk/rails` (`test`) | `equal-risk` | [`nathanwhyte/equal-risk-portfolio`](https://github.com/nathanwhyte/equal-risk-portfolio) (public, Ruby) | Rails + Thruster; `Cmd: ['./bin/thrust','./bin/rails','server']`, `RAILS_ENV=production` | `~/code/equal-risk-portfolio/` (own `build-docker.sh` + `k8s/`; pushes via `docker buildx`) |
| `equal-risk/math` (`test`) | `equal-risk` | `nathanwhyte/equal-risk-portfolio` (same repo, sibling service) | Python FastAPI (inferred — not probed) | same as above |
| `glossary/glossary` | `glossary` | [`nathanwhyte/glossary`](https://github.com/nathanwhyte/glossary) (public, Elixir) | Phoenix release; `Cmd: ['/app/bin/server']`, `MIX_ENV=prod`, user `nobody` | **not on this machine** — deployment manifests only |
| `portfolio/portfolio` | `portfolio` | **No GitHub source found** (checked `nathanwhyte` and `aclamant` orgs via `gh repo list`/`gh search`) — image created 2026-03-15, last pulled 2026-04-13 | Phoenix release (per image config: `Cmd: ['/app/bin/server']`, `MIX_ENV=prod`, user `nobody`); would need to be extracted from the image if rebuilt | **not on this machine** — deployment manifests only |

**Push scripts in this repo:**

| Script | Target | Live consumer? |
|---|---|---|
| `viking/deploy-openviking-parallel.sh` | `registry.nathanwhyte.dev/homelab` | **No** — `ov-coordinator`, `ov-merge`, `ov-worker` all scaled to 0; current OV deploy pulls `ghcr.io/volcengine/openviking:v0.4.4` from upstream |
| `backlog/nanochat/build.sh` | `registry.nathanwhyte.dev/library/nanochat:*` | **No** — `backlog/nanochat/train-rocm-job.yaml` references `nanochat:rocm-v4` but the job itself is in `backlog/`, not deployed on the cluster |

**Notably absent:** no pulls from `harbor`, `viking`, `llama`, `grafana`, `hermes`, or any system namespace. The `viking/ov-coordinator`, `ov-merged`, `ov-vectordb`, and related services all pull from `ghcr.io/volcengine/openviking:v0.4.4` and `ghcr.io/ggml-org/llama.cpp:server-cuda` — not from local Harbor. The hermes-agent cluster deployment pulls `registry.nathanwhyte.dev/homelab/hermes-agent-mem0:fe57dd3` from the local Harbor.

To re-run the audit:

```bash
# In-cluster: which namespaces pull from registry.nathanwhyte.dev?
for ns in $(kubectl get ns -o jsonpath='{range .items[*]}{.metadata.name}{" "}{end}'); do
  out=$(kubectl get pods -n "$ns" -o jsonpath='{range .items[*]}{.spec.containers[*].image}{" "}{end}' 2>/dev/null | tr ' ' '\n' | grep 'registry\.nathanwhyte\.dev' | sort -u)
  [ -n "$out" ] && { echo "--- $ns ---"; echo "$out"; }
done

# Repo-wide: who references the registry?
grep -rn 'registry\.nathanwhyte\.dev' ~/code/ \
    --include='*.yaml' --include='*.yml' --include='*.sh' \
    2>/dev/null | grep -v 'harbor/\|thoughts/'
```

## See also

- `harbor/HARBOR.md` — one-page index
- `harbor/HARBOR-RUNBOOK.md` — operational procedures
- [goharbor.io/cli-docs](https://goharbor.io/cli-docs/) — full CLI reference
- [goharbor/harbor-cli](https://github.com/goharbor/harbor-cli) — source
