#!/usr/bin/env bash
set -euo pipefail

RESOLVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="grafana"
# Never an empty array: macOS /usr/bin/env bash is 3.2, where "${a[@]}" of an
# empty array under `set -u` aborts with "unbound variable".
dry_run=(--dry-run=none)
live=1
case "${1:-}" in
--dry-run)
	dry_run=(--dry-run=server)
	live=0
	;;
"") ;;
*)
	echo "Usage: $0 [--dry-run]" >&2
	exit 2
	;;
esac
if (($# > 1)); then
	echo "Usage: $0 [--dry-run]" >&2
	exit 2
fi

for file in resolver.py policy.json manifests.yaml; do
	if [[ ! -f "$RESOLVER_DIR/$file" ]]; then
		echo "Missing required file: $RESOLVER_DIR/$file" >&2
		exit 1
	fi
done

python3 -m json.tool "$RESOLVER_DIR/policy.json" >/dev/null

if ((live)); then
	for secret in capacity-auto-resolver-token alertmanager-slack-bot-token; do
		if ! kubectl -n "$NAMESPACE" get secret "$secret" >/dev/null 2>&1; then
			echo "Required Secret $NAMESPACE/$secret does not exist." >&2
			if [[ "$secret" == "capacity-auto-resolver-token" ]]; then
				echo "Create it with:" >&2
				echo "  kubectl -n $NAMESPACE create secret generic $secret --from-literal=token=\"\$(openssl rand -hex 32)\"" >&2
			fi
			exit 1
		fi
	done
fi

apply_configmap() {
	local name="$1"
	local key="$2"
	local source="$3"
	kubectl -n "$NAMESPACE" create configmap "$name" \
		"--from-file=$key=$source" \
		--dry-run=client -o yaml | kubectl apply "${dry_run[@]}" -f -
}

apply_configmap capacity-auto-resolver-source resolver.py "$RESOLVER_DIR/resolver.py"
apply_configmap capacity-auto-resolver-policy policy.json "$RESOLVER_DIR/policy.json"
kubectl apply "${dry_run[@]}" -f "$RESOLVER_DIR/manifests.yaml"

if ((live)); then
	# ConfigMap updates do not change the pod template, so restart explicitly.
	kubectl -n "$NAMESPACE" rollout restart deployment/capacity-auto-resolver
	kubectl -n "$NAMESPACE" rollout status deployment/capacity-auto-resolver \
		--timeout=120s
fi
