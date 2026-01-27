# AGENTS

This repo contains Kubernetes manifests, Helm values, and small deploy scripts for a personal homelab cluster.

## Ground rules (safety first)

- **Do not apply changes to a live cluster unless explicitly asked.** Avoid running `kubectl apply`, `helm install/upgrade`, or anything that mutates cluster state unless the user requests it.
- **Do not introduce or commit secrets.**
  - Prefer referencing existing `Secret` resources (by name) rather than inlining secret material.
  - If a manifest must reference a secret key/token/cert, add a placeholder and document what’s required.
  - The repo ignores common sensitive patterns (see `.gitignore`), but still treat all credentials as out-of-scope.
- **Don’t change global tooling config** (git config, shell config, etc.) unless asked.

## Repo layout

Top-level directories generally map to deployed services:

- `dashboard/`: Kubernetes Dashboard manifests and tunnel/ingress config.
- `grafana/`: Observability stack (Prometheus/Grafana/Loki/Alloy), with Helm values under `grafana/helm/` and manifests under `grafana/manifests/`.
- `harbor/`: Harbor registry, Helm values and supporting manifests.
- `longhorn/`: Longhorn storage, Helm values and supporting manifests.
- `garage/`: Garage (S3-compatible object storage), values and a `manager/` subfolder for the garage manager manifests/config.

Each service folder typically includes:

- `deploy-*.sh`: an opinionated deploy script for that service
- `*-values.yaml`: Helm values (when deployed via Helm)
- `namespace.yaml`, `rbac.yaml`, `ingress.yaml`, `cloudflared.yaml`, etc.: supporting Kubernetes manifests

## Editing conventions

- **YAML style**
  - Keep indentation consistent (2 spaces).
  - Prefer explicit names/namespaces and stable labels/selectors.
  - Avoid large refactors/renames unless necessary; keep diffs reviewable.
- **Helm values**
  - Keep values files focused on overrides; prefer chart defaults where reasonable.
  - When adding configuration, include a short comment explaining *why* (especially for security, persistence, ingress, or resource limits).
- **Scripts**
  - Keep deploy scripts idempotent where possible.
  - Avoid embedding tokens/credentials in scripts; require env vars or pre-created secrets instead.

## Minimal validation (when making changes)

When editing files, prefer quick, local checks:

- `bash -n <script>` for shell scripts (syntax check)
- If available: `shellcheck <script>` and/or `yamllint <file>`
- Keep manifests syntactically valid YAML (don’t rely on cluster-side validation)

## Change hygiene

- Keep changes scoped to the service you’re touching.
- Update `README.md` only when adding/removing services or materially changing how deployments work.

