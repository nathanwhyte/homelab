#!/usr/bin/env bash
set -euo pipefail

# Label all existing Longhorn resources for Helm adoption.
# Run this ONCE before the first `helm upgrade --install` to transition
# from kubectl-apply managed resources to Helm-managed.

LABELS="app.kubernetes.io/managed-by=Helm"
ANNOTATIONS="meta.helm.sh/release-name=longhorn meta.helm.sh/release-namespace=longhorn-system"
NS="longhorn-system"

label_resource() {
  local kind="$1" name="$2" ns_flag="${3:-}"
  if kubectl get "$kind" "$name" $ns_flag &>/dev/null; then
    kubectl label "$kind" "$name" $ns_flag $LABELS --overwrite 2>/dev/null && \
    kubectl annotate "$kind" "$name" $ns_flag $ANNOTATIONS --overwrite 2>/dev/null && \
    echo "  [ok] $kind/$name" || echo "  [FAIL] $kind/$name"
  else
    echo "  [skip] $kind/$name (not found)"
  fi
}

echo "Labeling cluster-scoped resources..."
label_resource PriorityClass longhorn-critical
label_resource ClusterRole longhorn-role
label_resource ClusterRoleBinding longhorn-service-account
label_resource ClusterRoleBinding longhorn-support-bundle

echo "Labeling CRDs..."
for crd in backingimagedatasources backingimagemanagers backingimages backupbackingimages \
  backups backuptargets backupvolumes engineimages engines instancemanagers nodes orphans \
  recurringjobs replicas settings sharemanagers snapshots supportbundles systembackups \
  systemrestores volumeattachments volumes; do
  label_resource CustomResourceDefinition "${crd}.longhorn.io"
done

echo "Labeling namespaced resources..."
for sa in longhorn-service-account longhorn-ui-service-account longhorn-support-bundle; do
  label_resource ServiceAccount "$sa" "-n $NS"
done
for cm in longhorn-default-resource longhorn-default-setting longhorn-storageclass; do
  label_resource ConfigMap "$cm" "-n $NS"
done
for svc in longhorn-admission-webhook longhorn-backend longhorn-frontend longhorn-recovery-backend; do
  label_resource Service "$svc" "-n $NS"
done
label_resource DaemonSet longhorn-manager "-n $NS"
label_resource Deployment longhorn-driver-deployer "-n $NS"
label_resource Deployment longhorn-ui "-n $NS"
label_resource Role longhorn "-n $NS"
label_resource RoleBinding longhorn-service-account "-n $NS"
label_resource Ingress longhorn-ingress "-n $NS" 2>/dev/null || true

echo -e "\nDone! All resources labeled for Helm adoption."
echo "You can now run: ./deploy-longhorn.sh"
