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

**Verified 2026-06-10** (results now in `HARBOR.md`'s "External consumers" row). Each pull namespace maps to a project, but only some have their source repos on this machine.

| Harbor image | Cluster ns | Project / source repo | Local path |
|---|---|---|---|
| `build-hook/api` | `build` | build-hook (CI/CD webhook for buildkit builds) | **not on this machine** — only the deployment manifest in `~/code/dotfiles/hermes/k8s/manifests.yaml` |
| `coach/coach` + `coach/scrub` | `coach` | credit-coach (Rails + Bun frontend + scrubber sidecar) | **not on this machine** — deployment manifests only |
| `equal-risk/rails` + `equal-risk/math` | `equal-risk` | equal-risk-portfolio (Rails + Python FastAPI) | `~/code/equal-risk-portfolio/` (own `build-docker.sh` + `k8s/`; pushes via `docker buildx`) |
| `glossary/glossary` | `glossary` | glossary (likely a Phoenix/Elixir app per Hermes memory note) | **not on this machine** — deployment manifests only |
| `portfolio/portfolio` | `portfolio` | portfolio (likely a Phoenix/Elixir app per Hermes memory note) | **not on this machine** — deployment manifests only |

**Push scripts in this repo:**

| Script | Target | Live consumer? |
|---|---|---|
| `viking/deploy-openviking-parallel.sh` | `registry.nathanwhyte.dev/homelab` | **No** — `ov-coordinator`, `ov-merge`, `ov-worker` all scaled to 0; current OV deploy pulls `ghcr.io/volcengine/openviking:v0.3.14` from upstream |
| `backlog/nanochat/build.sh` | `registry.nathanwhyte.dev/library/nanochat:*` | **No** — `backlog/nanochat/train-rocm-job.yaml` references `nanochat:rocm-v4` but the job itself is in `backlog/`, not deployed on the cluster |

**Notably absent:** no pulls from `harbor`, `viking`, `llama`, `grafana`, `hermes`, or any system namespace. The `viking/ov-coordinator`, `ov-merged`, `ov-vectordb`, and related services all pull from `ghcr.io/volcengine/openviking:v0.3.14` and `ghcr.io/ggml-org/llama.cpp:server-cuda` — not from local Harbor. The hermes-agent cluster deployment pulls `nousresearch/hermes-agent:latest` from Docker Hub.

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
