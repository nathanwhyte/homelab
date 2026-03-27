# Ollama K8s Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy Ollama on timmy's RX 9070 XT as a K8s Deployment with Prometheus exporter sidecar, ConfigMap-driven model management, and Traefik ingress with API key auth on `robots.nathanwhyte.dev`.

**Architecture:** Single Deployment in the `llama` namespace pinned to timmy via nodeSelector. Two containers: Ollama (ROCm) + exporter sidecar (python:3.12-slim). Models persist on existing 20Gi PVC. Startup script pulls base models and creates custom models from Modelfiles stored in ConfigMaps. Traefik BasicAuth middleware protects external ingress.

**Tech Stack:** Ollama (ROCm), Traefik Ingress, Prometheus, K8s ConfigMaps, cert-manager

**Spec:** `docs/superpowers/specs/2026-03-27-ollama-k8s-deployment-design.md`

---

## File Structure

| File | Responsibility |
|------|---------------|
| `llama/ollama-configmap.yaml` | Startup script + Modelfiles (two ConfigMaps) |
| `llama/ollama-deployment.yaml` | Deployment (ollama + exporter sidecar) + ClusterIP Service |
| `llama/ollama-ingress.yaml` | Traefik Ingress + BasicAuth middleware + Secret + TLS |
| `grafana/helm/kube-prometheus-stack-values.yaml` | Update Prometheus scrape target (existing file) |

Reuses existing: `llama/namespace.yaml`, `llama/pvc.yaml` (20Gi `llama-model-cache`)

---

### Task 1: ConfigMaps (startup script + Modelfiles)

**Files:**
- Create: `llama/ollama-configmap.yaml`

- [ ] **Step 1: Create the Modelfiles ConfigMap**

```yaml
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: ollama-modelfiles
  namespace: llama
data:
  qwen35-claude: |
    FROM qwen3.5:9b-q4_K_M
    PARAMETER num_ctx 65536
    SYSTEM "/no_think"
```

- [ ] **Step 2: Create the startup script ConfigMap**

```yaml
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: ollama-startup
  namespace: llama
data:
  startup.sh: |
    #!/bin/bash
    set -eu

    echo "Waiting for Ollama to be ready..."
    until curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; do
      sleep 2
    done
    echo "Ollama is ready."

    # Process each Modelfile in /modelfiles/
    for modelfile in /modelfiles/*; do
      name=$(basename "$modelfile")
      echo "Processing model: $name"

      # Extract base model from FROM line
      base=$(grep -i '^FROM ' "$modelfile" | head -1 | awk '{print $2}')

      # Pull base model if not already present
      if ! ollama list | grep -q "^${base}"; then
        echo "Pulling base model: $base"
        ollama pull "$base"
      else
        echo "Base model already present: $base"
      fi

      # Create custom model from Modelfile
      echo "Creating custom model: $name"
      ollama create "$name" -f "$modelfile"
    done

    echo "All models ready."
    ollama list

  ollama-exporter.py: |
    [PASTE FULL CONTENTS OF llama/ollama-exporter.py HERE — 226 lines]
    [Read the file with: cat llama/ollama-exporter.py]
    [Indent every line by 4 spaces for YAML block scalar formatting]
```

**Important:** The `ollama-exporter.py` value MUST contain the full contents of `llama/ollama-exporter.py` (226 lines), indented by 4 spaces for YAML formatting. Read the file and paste it verbatim — do not summarize or truncate.

- [ ] **Step 3: Write the combined file**

Write both ConfigMaps into `llama/ollama-configmap.yaml` as a single multi-document YAML file (separated by `---`). The `ollama-modelfiles` ConfigMap first, then the `ollama-startup` ConfigMap.

- [ ] **Step 4: Verify YAML syntax**

Run: `kubectl apply --dry-run=client -f llama/ollama-configmap.yaml`
Expected: `configmap/ollama-modelfiles created (dry run)` and `configmap/ollama-startup created (dry run)`

- [ ] **Step 5: Commit**

```bash
git add llama/ollama-configmap.yaml
git commit -m "feat: add Ollama ConfigMaps for startup script and Modelfiles"
```

---

### Task 2: Deployment + Service

**Files:**
- Create: `llama/ollama-deployment.yaml`

- [ ] **Step 1: Create the Deployment manifest**

```yaml
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ollama
  namespace: llama
  labels:
    app: ollama
spec:
  replicas: 1
  revisionHistoryLimit: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: ollama
  template:
    metadata:
      labels:
        app: ollama
    spec:
      nodeSelector:
        kubernetes.io/hostname: timmy
        gpu.vendor: amd
      securityContext:
        fsGroup: 0
      containers:
        - name: ollama
          image: ollama/ollama:rocm
          command: ["/bin/bash", "-c"]
          args:
            - |
              ollama serve &
              OLLAMA_PID=$!
              /scripts/startup.sh
              wait $OLLAMA_PID
          env:
            - name: OLLAMA_HOST
              value: "0.0.0.0"
            - name: OLLAMA_FLASH_ATTENTION
              value: "1"
            - name: OLLAMA_KV_CACHE_TYPE
              value: "q8_0"
            - name: OLLAMA_KEEP_ALIVE
              value: "-1"
            - name: OLLAMA_NUM_PARALLEL
              value: "2"
            - name: OLLAMA_MAX_LOADED_MODELS
              value: "1"
            - name: HIP_VISIBLE_DEVICES
              value: "0"
          ports:
            - containerPort: 11434
              name: http
          resources:
            requests:
              cpu: "500m"
              memory: 2Gi
              amd.com/gpu: "1"
            limits:
              cpu: "8"
              memory: 20Gi
              amd.com/gpu: "1"
          securityContext:
            privileged: true
          startupProbe:
            tcpSocket:
              port: 11434
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 120
          readinessProbe:
            tcpSocket:
              port: 11434
            initialDelaySeconds: 5
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 12
          volumeMounts:
            - name: model-cache
              mountPath: /root/.ollama
            - name: shm
              mountPath: /dev/shm
            - name: startup
              mountPath: /scripts
            - name: modelfiles
              mountPath: /modelfiles
        - name: ollama-exporter
          image: python:3.12-slim
          command: ["python3", "/scripts/ollama-exporter.py"]
          args:
            - "--ollama"
            - "http://localhost:11434"
            - "--port"
            - "9111"
          ports:
            - containerPort: 9111
              name: metrics
          resources:
            requests:
              cpu: "50m"
              memory: 64Mi
            limits:
              cpu: "200m"
              memory: 128Mi
          volumeMounts:
            - name: startup
              mountPath: /scripts
      volumes:
        - name: model-cache
          persistentVolumeClaim:
            claimName: llama-model-cache
        - name: shm
          emptyDir:
            medium: Memory
            sizeLimit: 4Gi
        - name: startup
          configMap:
            name: ollama-startup
            defaultMode: 0755
        - name: modelfiles
          configMap:
            name: ollama-modelfiles
```

- [ ] **Step 2: Add the Service**

Append to `llama/ollama-deployment.yaml`:

```yaml
---
apiVersion: v1
kind: Service
metadata:
  name: ollama
  namespace: llama
  labels:
    app: ollama
spec:
  type: ClusterIP
  ports:
    - name: http
      port: 80
      protocol: TCP
      targetPort: 11434
    - name: metrics
      port: 9111
      protocol: TCP
      targetPort: 9111
  selector:
    app: ollama
```

- [ ] **Step 3: Verify YAML syntax**

Run: `kubectl apply --dry-run=client -f llama/ollama-deployment.yaml`
Expected: `deployment.apps/ollama created (dry run)` and `service/ollama created (dry run)`

- [ ] **Step 4: Commit**

```bash
git add llama/ollama-deployment.yaml
git commit -m "feat: add Ollama Deployment with exporter sidecar and ClusterIP Service"
```

---

### Task 3: Ingress with API Key Auth

**Files:**
- Create: `llama/ollama-ingress.yaml`

- [ ] **Step 1: Generate htpasswd secret**

Generate credentials for the API key. Use `ollama` as the username and a generated password as the API key:

```bash
# Install htpasswd if needed
which htpasswd || sudo apt-get install -y apache2-utils

# Generate credentials (user will substitute their own password)
htpasswd -nb ollama YOUR_API_KEY_HERE | base64 -w 0
```

- [ ] **Step 2: Create the Secret, Middleware, and Ingress manifests**

```yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: ollama-api-key
  namespace: llama
type: Opaque
data:
  # base64-encoded htpasswd entry: ollama:YOUR_API_KEY_HERE
  # Generate with: htpasswd -nb ollama <password> | base64 -w 0
  users: REPLACE_WITH_BASE64_HTPASSWD
---
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: ollama-auth
  namespace: llama
spec:
  basicAuth:
    secret: ollama-api-key
---
# HTTP → HTTPS redirect
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: ollama-https-redirect
  namespace: llama
spec:
  redirectScheme:
    scheme: https
    permanent: true
---
# HTTP entrypoint — redirects to HTTPS
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ollama-redirect
  namespace: llama
  annotations:
    traefik.ingress.kubernetes.io/router.entrypoints: web
    traefik.ingress.kubernetes.io/router.middlewares: llama-ollama-https-redirect@kubernetescrd
spec:
  ingressClassName: traefik
  rules:
    - host: robots.nathanwhyte.dev
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: ollama
                port:
                  number: 80
---
# HTTPS entrypoint — with BasicAuth
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ollama-ingress
  namespace: llama
  annotations:
    traefik.ingress.kubernetes.io/router.entrypoints: websecure
    traefik.ingress.kubernetes.io/router.tls: "true"
    traefik.ingress.kubernetes.io/router.middlewares: llama-ollama-auth@kubernetescrd
    cert-manager.io/cluster-issuer: letsencrypt-prod
spec:
  ingressClassName: traefik
  tls:
    - hosts:
        - robots.nathanwhyte.dev
      secretName: ollama-tls
  rules:
    - host: robots.nathanwhyte.dev
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: ollama
                port:
                  number: 80
```

- [ ] **Step 3: Verify YAML syntax**

Run: `kubectl apply --dry-run=client -f llama/ollama-ingress.yaml`
Expected: All resources created (dry run). The Secret will need real credentials before actual apply.

- [ ] **Step 4: Commit**

```bash
git add llama/ollama-ingress.yaml
git commit -m "feat: add Traefik Ingress for Ollama with BasicAuth on robots.nathanwhyte.dev"
```

---

### Task 4: Update Prometheus Scrape Config

**Files:**
- Modify: `grafana/helm/kube-prometheus-stack-values.yaml:114-121`

- [ ] **Step 1: Update the scrape target**

Replace the bare-metal Ollama scrape target with the K8s service endpoint:

```yaml
      # Before (bare-metal):
      - job_name: "ollama-metrics"
        scrape_interval: 30s
        static_configs:
          - targets:
              - "192.168.1.19:9111"
            labels:
              node: "timmy"

      # After (K8s service):
      - job_name: "ollama-metrics"
        scrape_interval: 30s
        static_configs:
          - targets:
              - "ollama.llama.svc:9111"
            labels:
              node: "timmy"
```

- [ ] **Step 2: Verify the change**

Run: `grep -A5 'ollama-metrics' grafana/helm/kube-prometheus-stack-values.yaml`
Expected: Target should show `ollama.llama.svc:9111`

- [ ] **Step 3: Commit**

```bash
git add grafana/helm/kube-prometheus-stack-values.yaml
git commit -m "chore: update Prometheus Ollama scrape target from bare-metal to K8s service"
```

---

### Task 5: Deploy and Validate

- [ ] **Step 1: Apply ConfigMaps**

```bash
kubectl apply -f llama/ollama-configmap.yaml
```

Expected: Both ConfigMaps created in the `llama` namespace.

- [ ] **Step 2: Apply Deployment and Service**

```bash
kubectl apply -f llama/ollama-deployment.yaml
```

Expected: Deployment and Service created. Pod will start pulling the Ollama image.

- [ ] **Step 3: Watch pod startup**

```bash
kubectl -n llama get pods -l app=ollama -w
```

Expected: Pod goes through `ContainerCreating` → `Running`. The startup probe gives up to 20 minutes for model loading.

- [ ] **Step 4: Verify model loading**

```bash
kubectl -n llama exec deploy/ollama -c ollama -- ollama list
```

Expected: Shows `qwen35-claude` and its base model `qwen3.5:9b-q4_K_M`.

- [ ] **Step 5: Test internal API access**

```bash
kubectl run test --rm -i --restart=Never --image=curlimages/curl -n llama \
  -- curl -s http://ollama.llama.svc:80/api/tags
```

Expected: JSON response listing loaded models.

- [ ] **Step 6: Verify exporter sidecar**

```bash
kubectl run test --rm -i --restart=Never --image=curlimages/curl -n llama \
  -- curl -s http://ollama.llama.svc:9111/metrics
```

Expected: Prometheus metrics output including `ollama_up 1` and `ollama_models_loaded`.

- [ ] **Step 7: Set up ingress credentials and apply**

Generate real htpasswd credentials, base64-encode them, update the Secret in `llama/ollama-ingress.yaml`, then apply:

```bash
kubectl apply -f llama/ollama-ingress.yaml
```

- [ ] **Step 8: Test external access**

Verify `robots.nathanwhyte.dev` resolves and the auth challenge works (after DNS is configured):

```bash
# Should return 401 without credentials
curl -s -o /dev/null -w "%{http_code}" https://robots.nathanwhyte.dev/api/tags

# Should return 200 with credentials
curl -s -u ollama:YOUR_API_KEY https://robots.nathanwhyte.dev/api/tags
```

- [ ] **Step 9: Stop bare-metal Ollama on timmy**

Provide this command for the user to run on timmy (SSH doesn't work from Claude's shell):

```bash
ssh -t timmy 'sudo systemctl disable --now ollama ollama-exporter'
```

- [ ] **Step 10: Apply Prometheus config**

```bash
helm upgrade kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n monitoring -f grafana/helm/kube-prometheus-stack-values.yaml
```

Verify scrape target in Grafana's Prometheus targets page shows `ollama.llama.svc:9111` as UP.

- [ ] **Step 11: Commit any credential/DNS changes**

```bash
git add -A
git commit -m "chore: finalize Ollama K8s deployment with credentials and DNS"
```
