#!/usr/bin/env bash
# check-alert-routing.sh — prove every loaded alert rule can actually be delivered.
#
# BUG-1129: alert delivery was an allowlist keyed on `alertgroup`, a label only
# locally-authored rules set. 169 of 194 live label sets — 40 of them critical —
# routed to the null receiver and were discarded without a trace. Nothing failed,
# nothing logged; the rules simply never reached anyone. manu crashed twice on
# 2026-09-14 and KubeNodeNotReady, KubeletDown and KubeNodeUnreachable were all
# among the 169.
#
# Adding a rule and adding a route were separate acts with no linkage. This
# closes that: it evaluates every rule's real label set against the live merged
# route tree (base config + operator-merged AlertmanagerConfig CRs) and fails
# when any rule reaches no receiver, or reaches two.
#
#   ./grafana/check-alert-routing.sh            # check the live cluster
#   ./grafana/check-alert-routing.sh --verbose  # list every rule and its receiver
#
# Exit 0 = every rule deliverable exactly once. Exit 1 = a gap.
set -euo pipefail

NS=${ALERTMANAGER_NAMESPACE:-grafana}
POD=${ALERTMANAGER_POD:-alertmanager-prom-alertmanager-0}
CFG=/etc/alertmanager/config_out/alertmanager.env.yaml
VERBOSE=${1:-}

# Deliberately silent. Watchdog fires forever to prove Alertmanager is alive;
# InfoInhibitor exists only to suppress other alerts; `info` is dashboard-only.
# Anything else reaching no receiver is a bug, not a decision.
is_expected_silent() {
	case "$1" in
	*'alertname=Watchdog'*) return 0 ;;
	*'alertname=InfoInhibitor'*) return 0 ;;
	*'severity=info'*) return 0 ;;
	*) return 1 ;;
	esac
}

command -v kubectl >/dev/null || {
	echo "check-alert-routing: kubectl not found" >&2
	exit 2
}

# Every distinct label set across every loaded PrometheusRule. Each label must be
# its own argument to amtool: it joins positional args into ONE matcher, so
# "a=b c=d" in a single word silently matches nothing and prints `null` — which
# reads exactly like an undelivered alert (BUG-1129's amtool invocation trap).
labelsets=$(kubectl get prometheusrule -A -o json | python3 -c "
import sys, json
d = json.load(sys.stdin)
seen = []
for r in d['items']:
    for g in r['spec']['groups']:
        for ru in g['rules']:
            a = ru.get('alert')
            if not a:
                continue
            l = dict(ru.get('labels') or {})
            args = ['alertname=' + a]
            for k in ('severity', 'alertgroup'):
                if k in l:
                    args.append(k + '=' + str(l[k]))
            line = ' '.join(args)
            if line not in seen:
                seen.append(line)
print('\n'.join(seen))
")

total=$(printf '%s\n' "$labelsets" | grep -c . || true)
echo "check-alert-routing: $total distinct label sets from $(kubectl get prometheusrule -A --no-headers | wc -l | tr -d ' ') PrometheusRule objects"

# One exec for the whole sweep — a kubectl exec per rule would take minutes.
results=$(printf '%s\n' "$labelsets" | kubectl -n "$NS" exec -i "$POD" -c alertmanager -- sh -c "
while IFS= read -r line; do
  [ -z \"\$line\" ] && continue
  echo \"\$(amtool config routes test --config.file=$CFG \$line 2>/dev/null | tr '\n' ',')|\$line\"
done
")

undelivered=0
duplicated=0
while IFS='|' read -r receivers labels; do
	[ -z "${labels:-}" ] && continue
	real=$(printf '%s' "$receivers" | tr ',' '\n' | grep -v '^null$' | grep -c . || true)
	if [ "$real" -eq 0 ]; then
		if is_expected_silent "$labels"; then
			[ "$VERBOSE" = "--verbose" ] && echo "  silent (expected)  $labels"
		else
			echo "  UNDELIVERABLE      $labels"
			undelivered=$((undelivered + 1))
		fi
	elif [ "$real" -gt 1 ]; then
		echo "  DOUBLE-ROUTED      $labels -> $receivers"
		duplicated=$((duplicated + 1))
	else
		[ "$VERBOSE" = "--verbose" ] && echo "  ok                 $labels -> $(printf '%s' "$receivers" | tr -d ',')"
	fi
done <<EOF
$results
EOF

echo
if [ "$undelivered" -eq 0 ] && [ "$duplicated" -eq 0 ]; then
	echo "check-alert-routing: PASS — every rule reaches exactly one receiver"
	exit 0
fi
[ "$undelivered" -gt 0 ] && echo "check-alert-routing: FAIL — $undelivered rule(s) reach no receiver"
[ "$duplicated" -gt 0 ] && echo "check-alert-routing: FAIL — $duplicated rule(s) would page twice"
exit 1
