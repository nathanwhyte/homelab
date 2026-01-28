# Harbor Container Registry

_Generated with help from Cursor._

Harbor is an open-source container registry with enterprise features including:
- **Web UI** for managing images, projects, and users
- **Vulnerability Scanning** with Trivy integration
- **RBAC** for fine-grained access control
- **Image Replication** for multi-registry setups
- **Content Trust** for image signing (Notary, optional)
- **Helm Chart Repository** support
- **Metrics** endpoints for Prometheus monitoring

## Architecture

Harbor uses:
- **Storage Backend**: Longhorn persistent volumes (filesystem-based)
- **Database**: PostgreSQL (internal, deployed by Helm chart, v2.11.1)
- **Cache**: Redis (internal, deployed by Helm chart, v2.11.1)
- **TLS**: Let's Encrypt certificates via cert-manager (DNS-01 challenge with Cloudflare)
- **Ingress**: Traefik with unlimited upload size via custom middleware
- **Version**: Harbor v2.11.1 (components pinned to this version)

## Deployment Steps

### Prerequisites

1. **Namespace**: The `harbor` namespace should exist (created by `harbor/namespace.yaml`)
2. **cert-manager**: Must be installed with the `letsencrypt-prod` ClusterIssuer configured
3. **Traefik Middleware**: Apply the Harbor middleware for unlimited upload sizes:
   ```bash
   kubectl apply -f harbor/harbor-middleware.yaml
   ```

### 1. Install Harbor via Helm

```bash
# Add Harbor Helm repository
helm repo add harbor https://helm.goharbor.io
helm repo update

# Install Harbor
helm install harbor harbor/harbor \
  -f ~/code/homelab/harbor/helm/harbor-values.yaml \
  -n harbor \
  --create-namespace

# Wait for all pods to be ready (this may take several minutes)
kubectl wait --for=condition=ready pod -l app=harbor -n harbor --timeout=10m
```

### 2. Verify Installation

```bash
# Check all Harbor components
kubectl get pods -n harbor -l app=harbor

# Check ingress
kubectl get ingress -n harbor

# Check TLS certificate
kubectl get certificate -n harbor
```

### 3. Access Harbor

**Web UI**: https://registry.nathanwhyte.dev

**Docker Login**:
```bash
docker login registry.nathanwhyte.dev
# Enter username: <username>
# Enter password: <CHANGE_ME>
```

## Configuration Details

### Storage Configuration

Harbor stores image layers and metadata using Longhorn persistent volumes:
- **Registry Storage**: 20Gi PVC using `longhorn-db` storage class (image layers and charts)
- **Database**: 5Gi PVC using `longhorn-ssd` storage class (PostgreSQL data)
- **Redis**: 5Gi PVC using `longhorn-ssd` storage class (cache data)
- **Trivy**: 5Gi PVC using `longhorn-ssd` storage class (vulnerability database)
- **Job Service**: 
  - Job logs: 1Gi PVC (default storage class)
  - Scan data exports: 1Gi PVC (default storage class)

**Storage Policy**: `resourcePolicy: "keep"` - PVCs are retained on Helm uninstall to prevent data loss.

### Upload Size Limits

No size limits are enforced for image pushes:
- **Traefik Middleware**: `harbor-no-limit` middleware configured with:
  - `maxRequestBodyBytes: 0` (unlimited request body)
  - `maxResponseBodyBytes: 0` (unlimited response body)
  - `memRequestBodyBytes: 10485760` (10MB in-memory buffer)
  - `memResponseBodyBytes: 10485760` (10MB in-memory buffer)
- **Harbor's internal nginx**: Handles chunked uploads automatically
- **Supports**: Multi-gigabyte image pushes without size restrictions

**Note**: The middleware is defined in `harbor/harbor-middleware.yaml` and should be applied before or after Harbor installation.

### Security Features

1. **HTTPS Only**: Let's Encrypt certificates with automatic renewal via cert-manager
   - Certificate issuer: `letsencrypt-prod` (ClusterIssuer)
   - Validation method: DNS-01 challenge with Cloudflare
   - Certificate secret: `harbor-tls` (auto-managed by cert-manager)
2. **RBAC**: Project-based access control with roles (admin, developer, guest)
   - Project creation: Allowed for everyone (`projectCreationRestriction: "everyone"`)
   - Public projects: Allow anonymous pull access
   - Private projects: Require authentication for all operations
3. **Vulnerability Scanning**: Automatic scanning with Trivy
   - Enabled: `true`
   - Offline mode: `false` (updates database on startup)
   - Replicas: 1
   - Resources: 512Mi-2Gi memory, 200m-1000m CPU
4. **Content Trust**: Optional image signing with Notary (disabled by default)
   - Status: `enabled: false`
   - Can be enabled for image signing and verification
5. **Audit Logs**: All actions are logged and accessible via web UI
6. **Auto Certificate Generation**: Enabled for internal components (`enableAutoGenCert: true`)

### Component Configuration

Harbor components are configured with single replicas to avoid volume conflicts (RWO volumes):

- **Core** (API server): 1 replica
  - Resources: 256Mi-1Gi memory, 100m-1000m CPU
- **Portal** (Web UI): 1 replica
  - Resources: 64Mi-128Mi memory, 50m-200m CPU
- **Registry** (Docker registry): 1 replica
  - Resources: 256Mi-1Gi memory, 100m-1000m CPU
  - Controller: 128Mi-256Mi memory, 50m-200m CPU
- **Jobservice** (background jobs): 1 replica
  - Resources: 256Mi-1Gi memory, 100m-1000m CPU
- **Nginx** (internal routing): 1 replica
  - Resources: 128Mi-256Mi memory, 100m-500m CPU
- **Trivy** (vulnerability scanner): 1 replica
  - Resources: 512Mi-2Gi memory, 200m-1000m CPU
- **Database** (PostgreSQL): 1 replica (internal)
  - Resources: 256Mi-512Mi memory, 100m-500m CPU
- **Redis** (cache): 1 replica (internal)
  - Resources: 128Mi-256Mi memory, 100m-200m CPU

**Note**: Single replicas are used to avoid Multi-Attach errors with ReadWriteOnce (RWO) volumes. Longhorn provides replication at the storage layer.

## Usage Examples

### Push an Image

```bash
# Tag your image (project name is required, e.g., 'library' for default project)
docker tag myapp:latest registry.nathanwhyte.dev/library/myapp:latest

# Login first (if not already logged in)
docker login registry.nathanwhyte.dev

# Push to Harbor
docker push registry.nathanwhyte.dev/library/myapp:latest
```

### Pull an Image

```bash
# Public projects can be pulled without authentication
docker pull registry.nathanwhyte.dev/library/myapp:latest

# Private projects require login
docker login registry.nathanwhyte.dev
docker pull registry.nathanwhyte.dev/private-project/myapp:latest
```

### Create a Project (via Web UI)

1. Log in to https://registry.nathanwhyte.dev
2. Click "Projects" → "New Project"
3. Set project name and access level:
   - **Public**: Anyone can pull images (pushes still require auth)
   - **Private**: All operations require authentication
4. Assign users/roles as needed:
   - **Admin**: Full control over project
   - **Developer**: Can push/pull images
   - **Guest**: Read-only access

### Scan for Vulnerabilities

Harbor automatically scans images on push (if enabled). View results in the web UI:
1. Navigate to your project
2. Click on a repository
3. View the "Vulnerabilities" column for scan results
4. Click on an image tag to see detailed vulnerability information

**Manual Scan**: You can also trigger scans manually from the web UI by clicking "Scan" on an image.

### Access Metrics

Harbor exposes Prometheus metrics on all components:
- **Core**: `http://harbor-core.harbor.svc.cluster.local:8001/metrics`
- **Registry**: `http://harbor-registry.harbor.svc.cluster.local:8001/metrics`
- **Jobservice**: `http://harbor-jobservice.harbor.svc.cluster.local:8001/metrics`
- **Exporter**: `http://harbor-exporter.harbor.svc.cluster.local:8001/metrics`

These endpoints can be scraped by Prometheus using a ServiceMonitor or PodMonitor.

## Maintenance

### Update Harbor

```bash
# Update Helm repository
helm repo update

# Upgrade Harbor (review changelog first for breaking changes)
helm upgrade harbor harbor/harbor \
  -f ~/code/homelab/harbor/harbor-values.yaml \
  -n harbor

# Wait for rollout to complete
kubectl rollout status deployment/harbor-core -n harbor
kubectl rollout status deployment/harbor-portal -n harbor
kubectl rollout status deployment/harbor-jobservice -n harbor
kubectl rollout status deployment/harbor-registry -n harbor
```

**Note**: Always review the Harbor release notes before upgrading, especially for major version changes.

### Backup

Harbor data is stored in Longhorn persistent volumes. Back up these PVCs for disaster recovery:

- **Image Layers**: Registry PVC (`harbor-registry`, 20Gi, `longhorn-db`)
- **Database**: PostgreSQL PVC (`harbor-database`, 5Gi, `longhorn-ssd`)
- **Redis**: Redis PVC (`harbor-redis`, 5Gi, `longhorn-ssd`)
- **Trivy**: Vulnerability database PVC (`harbor-trivy`, 5Gi, `longhorn-ssd`)
- **Job Service**: 
  - Job logs PVC (`harbor-jobservice-jobservice-joblog`, 1Gi)
  - Scan data exports PVC (`harbor-jobservice-jobservice-scan-data-exports`, 1Gi)

**Backup Methods**:
1. Use Longhorn's built-in backup functionality
2. Create snapshots of PVCs
3. Export database using `pg_dump` from the database pod
4. Copy registry storage directory

### Garbage Collection

Run garbage collection to remove unused blobs and free up storage:
1. Log in to Harbor web UI
2. Go to "Administration" → "Garbage Collection"
3. Click "Run Now" or schedule automatic runs
4. Monitor the job status in "Administration" → "Job Service Logs"

**Note**: Garbage collection requires the registry to be in read-only mode during execution. Plan accordingly.

### Database Maintenance

```bash
# Access PostgreSQL pod
kubectl exec -it -n harbor deployment/harbor-database -- bash

# Connect to database
psql -U postgres -d registry

# Common maintenance queries
# Check database size
SELECT pg_size_pretty(pg_database_size('registry'));

# List tables
\dt

# Vacuum database (run periodically)
VACUUM ANALYZE;
```

## Migrating from Old Registry

If you have images in the old Docker Registry (registry:2), you can migrate them:

### Option 1: Re-tag and Push (Recommended)

```bash
# Pull from old registry
docker pull registry.nathanwhyte.dev/image:tag

# Re-tag for Harbor (note: Harbor requires a project name, e.g., 'library')
docker tag registry.nathanwhyte.dev/image:tag registry.nathanwhyte.dev/library/image:tag

# Login to Harbor
docker login registry.nathanwhyte.dev

# Push to Harbor
docker push registry.nathanwhyte.dev/library/image:tag
```

**Batch Migration Script**:
```bash
# List all images in old registry (if accessible)
# Then loop through and migrate each one
for image in $(cat images.txt); do
  docker pull registry.nathanwhyte.dev/$image
  docker tag registry.nathanwhyte.dev/$image registry.nathanwhyte.dev/library/$image
  docker push registry.nathanwhyte.dev/library/$image
done
```

### Option 2: Use Harbor Replication

1. Keep the old registry running temporarily
2. Log in to Harbor web UI
3. Go to "Administration" → "Replications" → "New Replication Rule"
4. Configure source registry (old registry endpoint)
5. Select projects/repositories to replicate
6. Harbor will automatically replicate images in the background

**Note**: Replication requires the old registry to be accessible from Harbor's jobservice pods.

## Troubleshooting

### Check Pod Status

```bash
# Check all Harbor pods
kubectl get pods -n harbor -l app=harbor

# Check specific component
kubectl get pods -n harbor -l component=core
kubectl get pods -n harbor -l component=registry
kubectl get pods -n harbor -l component=jobservice
kubectl get pods -n harbor -l component=portal
kubectl get pods -n harbor -l component=database
kubectl get pods -n harbor -l component=redis
kubectl get pods -n harbor -l component=trivy

# Describe a failing pod
kubectl describe pod <pod-name> -n harbor
```

### View Logs

```bash
# Core logs
kubectl logs -n harbor -l component=core --tail=100 -f

# Registry logs
kubectl logs -n harbor -l component=registry --tail=100 -f

# Jobservice logs (for background jobs)
kubectl logs -n harbor -l component=jobservice --tail=100 -f

# All Harbor logs
kubectl logs -n harbor -l app=harbor --all-containers --tail=50

# Database logs
kubectl logs -n harbor -l component=database --tail=100

# Redis logs
kubectl logs -n harbor -l component=redis --tail=100

# Trivy logs
kubectl logs -n harbor -l component=trivy --tail=100
```

### Database Connection Issues

```bash
# Check database pod status
kubectl get pods -n harbor -l component=database
kubectl logs -n harbor -l component=database

# Check database connectivity from core
kubectl exec -it -n harbor deployment/harbor-core -- nc -zv harbor-database 5432

# Test database connection directly
kubectl exec -it -n harbor deployment/harbor-database -- psql -U postgres -d registry -c "SELECT version();"
```

### Ingress and TLS Issues

```bash
# Check ingress
kubectl get ingress -n harbor
kubectl describe ingress -n harbor

# Check certificate
kubectl get certificate -n harbor
kubectl describe certificate harbor-tls -n harbor

# Check cert-manager logs
kubectl logs -n cert-manager -l app=cert-manager --tail=100

# Test TLS connection
curl -v https://registry.nathanwhyte.dev/api/v2.0/health
```

### Storage Issues

```bash
# Check PVCs
kubectl get pvc -n harbor

# Check PVC details
kubectl describe pvc harbor-registry -n harbor
kubectl describe pvc harbor-database -n harbor

# Check storage usage (if Longhorn UI available)
# Or check from within pod
kubectl exec -it -n harbor deployment/harbor-core -- df -h /storage
```

### Common Issues

**Issue**: Pods stuck in `Pending` state
- **Solution**: Check PVC status and ensure storage classes are available
- Check: `kubectl get pvc -n harbor`

**Issue**: Certificate not issued
- **Solution**: Verify cert-manager is running and ClusterIssuer is configured
- Check: `kubectl get clusterissuer letsencrypt-prod`
- Check: `kubectl get secret cloudflare-api-token -n cert-manager`

**Issue**: Cannot push large images
- **Solution**: Verify Traefik middleware is applied: `kubectl get middleware -n harbor`
- Check: `kubectl describe middleware harbor-no-limit -n harbor`

**Issue**: Vulnerability scans failing
- **Solution**: Check Trivy pod logs and ensure it has internet access for database updates
- Check: `kubectl logs -n harbor -l component=trivy`

**Issue**: High memory usage
- **Solution**: Review resource limits in `harbor-values.yaml` and adjust if needed
- Monitor: `kubectl top pods -n harbor`
```

## Resources

- [Harbor Documentation](https://goharbor.io/docs/)
- [Harbor Helm Chart](https://github.com/goharbor/harbor-helm)
