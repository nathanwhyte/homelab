# Hermes Agent on K3s

Hermes runs in the `hermes` namespace as two Deployments:

- `hermes-agent` — Hermes Agent runtime, currently running `hermes gateway run`
- `hermes-jump` — constrained SSH terminal backend used by Hermes for shell/tool execution

The agent API is **cluster-internal only**. Do not expose it through Cloudflare or any public ingress until the PROJ-006 safety review is complete.

## Operator access model

The supported operator path is:

1. `kubectl port-forward` to the internal `hermes-agent` Service.
2. Bearer-authenticated calls to the built-in Hermes API server on port `8642`.
3. `kubectl logs` / `kubectl exec` only for admin/debug actions such as logs, session listing, config inspection, rollout restart, or checking the jump backend.

This avoids adding an SSH daemon, LoadBalancer, Ingress, or Cloudflare route for operator access.

## Helper script

Use `hermes/operator.sh` from this repository:

```bash
# health check through a temporary port-forward
hermes/operator.sh health

# list API models
hermes/operator.sh models

# one-shot OpenAI-compatible chat completion
hermes/operator.sh ask "Reply exactly: ok"

# async run + Server-Sent Events stream
hermes/operator.sh run "Reply exactly: ok"

# foreground port-forward for manual curl/OpenAI clients
hermes/operator.sh port-forward
```

The script reads the API key from the live Kubernetes Secret `hermes-api-server-key` unless `HERMES_API_KEY` is already set.

## Manual API usage

If you do not want to use the helper script:

```bash
kubectl -n hermes port-forward svc/hermes-agent 8642:8642
```

In another shell:

```bash
HERMES_API_KEY=$(kubectl get secret hermes-api-server-key -n hermes \
  -o jsonpath='{.data.api-key}' | base64 --decode)

curl -fsS http://127.0.0.1:8642/health

curl -fsS http://127.0.0.1:8642/v1/chat/completions \
  -H "Authorization: Bearer ${HERMES_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.1:cloud",
    "messages": [{"role": "user", "content": "Reply exactly: ok"}],
    "stream": false
  }'
```

## Admin/debug commands

```bash
# Gateway logs
hermes/operator.sh logs
hermes/operator.sh logs --follow --tail=50

# Hermes session store
hermes/operator.sh sessions list
hermes/operator.sh sessions stats

# Runtime status and config
hermes/operator.sh status
hermes/operator.sh config

# Rollout/restart
hermes/operator.sh rollout-status
hermes/operator.sh restart
```

`hermes/operator.sh config` runs `hermes config show` inside the pod. Treat its output as operationally sensitive and do not paste secrets into committed files.

## Terminal backend check

Hermes should run terminal commands through the `hermes-jump` pod, not directly in the agent container.

```bash
hermes/operator.sh jump-check
```

The expected command output should show the jump pod hostname and `/home/hermes` working directory. If the agent asks for approval, approve only benign read-only commands while TASK-030 is incomplete.

The Kubernetes RBAC boundary can be checked directly:

```bash
kubectl exec -n hermes deploy/hermes-jump -- kubectl auth can-i get pods -n llama
kubectl exec -n hermes deploy/hermes-jump -- kubectl auth can-i delete pods -n llama
```

Expected: `yes` for read-only pod access, `no` for destructive pod deletion.

## Rollback / disable

This access path has no external exposure. To disable local operator access, stop the port-forward process.

To disable the API server entirely, remove or set `API_SERVER_ENABLED=false` in `hermes/hermes-deployment.yaml`, remove the API port/probes or switch probes back to an exec/readiness marker, then apply the deployment. That is a manifest rollback and should be committed separately.

To rotate the API key:

```bash
kubectl create secret generic hermes-api-server-key \
  -n hermes \
  --from-literal=api-key="$(openssl rand -hex 32)" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl rollout restart deployment/hermes-agent -n hermes
kubectl rollout status deployment/hermes-agent -n hermes
```

Do not commit the generated key.
