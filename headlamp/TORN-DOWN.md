# Torn down 2026-07-02

The `headlamp` namespace was deleted (with its `headlamp-admin*`
ClusterRoleBindings) and the `lamp.nathanwhyte.dev` tunnel route removed.
Manifests retained for reference — do not re-apply without recreating the
namespace tunnel token and reviewing the cluster-admin RBAC it grants.
The orphaned `lamp.nathanwhyte.dev` DNS record still needs deleting in the
Cloudflare dashboard.
