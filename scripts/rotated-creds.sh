#!/usr/bin/env bash
# rotated-creds.sh — print rotated cluster credentials to a local file.
#
# This script does NOT touch the cluster. It just formats a fixed
# summary of the credentials that were rotated during the
# TASK-050 follow-up (Grafana admin + Harbor secretKey) into a
# human-readable text file the operator can upload to a password
# manager and then delete.
#
# IMPORTANT: do not commit the resulting output file. It contains
# plaintext secrets. The script writes to ./rotated-creds-<date>.txt
# in the current working directory; you decide where to run it.
#
# Usage:
#   ./scripts/rotated-creds.sh                 # writes to cwd
#   OUT=/secure/path/creds.txt ./scripts/rotated-creds.sh
#
# Required environment (export before running):
#   HARBOR_ADMIN_PASSWORD     active Harbor admin password
#   HARBOR_SECRETKEY          active Harbor core signing key
#   GRAFANA_ADMIN_PASSWORD    active Grafana admin password
#
# If you have the 1Password CLI installed, you can source values
# from your vault with `op read`, e.g.:
#   export HARBOR_ADMIN_PASSWORD="$(op read op://Homelab/harbor-admin/password)"
#   export HARBOR_SECRETKEY="$(op read op://Homelab/harbor-secretkey/password)"
#   export GRAFANA_ADMIN_PASSWORD="$(op read op://Homelab/grafana-admin/password)"
#   ./scripts/rotated-creds.sh
#
# After you upload:
#   shred -u rotated-creds-*.txt    # if you have shred(1)
#   # or just: rm rotated-creds-*.txt
#
# The script refuses to overwrite an existing file unless
# FORCE=1 is set in the environment. It also refuses to run if
# any of the three required values are unset.

set -euo pipefail

missing=()
[ -n "${HARBOR_ADMIN_PASSWORD:-}" ] || missing+=("HARBOR_ADMIN_PASSWORD")
[ -n "${HARBOR_SECRETKEY:-}" ] || missing+=("HARBOR_SECRETKEY")
[ -n "${GRAFANA_ADMIN_PASSWORD:-}" ] || missing+=("GRAFANA_ADMIN_PASSWORD")
if [ "${#missing[@]}" -gt 0 ]; then
	printf 'ERROR: missing required env vars: %s\n' "${missing[*]}" >&2
	printf '       set them (or `op read ...`) and re-run. See header for examples.\n' >&2
	exit 2
fi

DATE="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${OUT:-./rotated-creds-${DATE}.txt}"

if [ -e "$OUT" ] && [ "${FORCE:-0}" != "1" ]; then
	printf 'ERROR: %s already exists. Set FORCE=1 to overwrite.\n' "$OUT" >&2
	exit 2
fi

umask 077

{
	printf 'Rotated cluster credentials — generated %s\n\n' "$DATE"
	printf 'Source: TASK-050 follow-up sweep (homelab/harbor-runbook.md,\n'
	printf '        homelab/grafana/reset-admin-password.sh)\n\n'

	printf '== Harbor ==\n'
	printf 'Secret:    harbor/harbor-core\n'
	printf 'Key:       secretKey (Harbor core signing key — CSRF/session token signing)\n'
	printf 'Namespace: harbor\n'
	printf 'Cluster:   manu (single Harbor install)\n'
	printf 'Value:     %s\n' "$HARBOR_SECRETKEY"
	printf 'Where to use:\n'
	printf '  - This is consumed by the cluster, not by you directly.\n'
	printf '  - You only need it if you want to verify the rotation:\n'
	printf '      kubectl get secret harbor-core -n harbor \\\n'
	printf '        -o jsonpath='"'"'{.data.secretKey}'"'"' | base64 -d\n'
	printf '    should print the value above, NOT the literal "not-a-secure-key".\n'
	printf '  - Keep it so you can compare after future cluster rebuilds.\n\n'

	printf '== Grafana ==\n'
	printf 'Secret:    kube-prometheus-stack-grafana (admin)\n'
	printf 'Key:       GF_SECURITY_ADMIN_PASSWORD (env-injected; mirrored in Grafana DB after reset)\n'
	printf 'Namespace: grafana\n'
	printf 'Cluster:   wemby (kube-prometheus-stack chart, app=grafana)\n'
	printf 'URL:       https://grafana.nathanwhyte.dev  (LAN-only, IP-allowlisted)\n'
	printf 'Username:  admin\n'
	printf 'Password:  %s\n' "$GRAFANA_ADMIN_PASSWORD"
	printf 'Where to use:\n'
	printf '  - Log into the Grafana web UI as admin with this password.\n'
	printf '  - Used by any local automation that calls /api/org or /api/dashboards.\n\n'

	printf '== Companion credentials (rotated in the same session) ==\n\n'

	printf '== Harbor admin ==\n'
	printf 'Source:    TASK-050 (Harbor admin rotation)\n'
	printf 'Rotated:   2026-06-11 (re-rotated at the end of the TASK-050 sweep)\n'
	printf 'Username:  admin\n'
	printf 'Password:  %s\n' "$HARBOR_ADMIN_PASSWORD"
	printf 'URL:       https://registry.nathanwhyte.dev\n'
	printf 'Notes:     The in-cluster K8s Secret (harbor-core/HARBOR_ADMIN_PASSWORD)\n'
	printf '           and the harbor_user DB row hold this value. Both are kept\n'
	printf '           in sync by the gate in harbor/deploy-harbor.sh: any future\n'
	printf '           helm upgrade that omits --admin-password will abort with\n'
	printf '           a clear error rather than silently re-apply the chart'\''s\n'
	printf '           "<CHANGE_ME>" placeholder.\n\n'

	printf '== Notes ==\n'
	printf -- '- The Harbor DB row (harbor_user.password) and the\n'
	printf '  harbor-core K8s Secret (HARBOR_ADMIN_PASSWORD) are kept in\n'
	printf '  sync by the gate in harbor/deploy-harbor.sh: any future\n'
	printf '  helm upgrade that omits --admin-password will abort with\n'
	printf '  a clear error rather than silently re-apply the placeholder.\n'
	printf -- '- The Grafana K8s Secret kube-prometheus-stack-grafana still\n'
	printf '  holds "Homelab@123" (the chart never re-syncs admin pw on\n'
	printf '  upgrade; only the DB row holds the new value). That is fine\n'
	printf '  in practice — the pod only reads the env var at first start\n'
	printf '  to seed the admin user, and the user already exists. If you\n'
	printf '  ever want the Secret to match, run:\n'
	echo '      kubectl patch secret kube-prometheus-stack-grafana \'
	echo '        -n grafana --type=json -p='\''[{"op":"replace",'
	echo '        "path":"/data/admin-password",'
	echo '        "value":"<base64-of-new-pw>"}]'\'''
	echo '  and restart the deployment.'
	printf -- '- secretKey is safe to rotate again at any time (signing-key\n'
	printf '  invalidation only). Procedure is in harbor/harbor-runbook.md\n'
	printf '  § "Auth: secretKey".\n\n'

	printf 'Delete this file after uploading:\n'
	printf '    shred -u %s\n' "$OUT"
	printf '    # or: rm %s\n' "$OUT"
} >"$OUT"

# Lock down perms and report
chmod 600 "$OUT"
printf 'Wrote %s (mode 0600, %s bytes)\n' "$OUT" "$(wc -c <"$OUT" | tr -d ' ')"
printf 'Upload to your password manager, then shred or rm the file.\n'
