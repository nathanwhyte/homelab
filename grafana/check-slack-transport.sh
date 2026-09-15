#!/usr/bin/env bash
# check-slack-transport.sh — prove every Slack receiver's destination is knowable.
#
# BUG-1135: the config said `channel: "#cron-homelab"` and every alert arrived in
# `#hermes-noise`. A Slack incoming webhook is bound to the channel chosen when it
# was minted, and `slack_configs.channel` is ignored on the POST — so a webhook
# rotation silently redirected delivery while every layer reported success: the
# route resolved, Slack returned 200 ok, and the notification counter incremented.
# Nothing failed and nothing logged.
#
# A counter is not proof of delivery, and neither is a rendered config that merely
# mentions a channel. What makes the destination *knowable* is the transport:
#
#   chat.postMessage + a bot token  -> `channel` is a request parameter, authoritative
#   hooks.slack.com webhook         -> `channel` is decoration; the URL decides
#
# So this check fails when a receiver uses a webhook transport while also carrying a
# `channel` field, because that combination is exactly the state in which the field
# lies. It proves the destination is knowable; it does NOT claim to prove delivery,
# which still needs a human reading the channel (see grafana/alert-routing.md).
#
#   ./grafana/check-slack-transport.sh                 # check the live cluster
#   ./grafana/check-slack-transport.sh --verbose       # list every Slack receiver
#   ./grafana/check-slack-transport.sh --config-file F # check a local YAML file (tests)
#
# Exit 0 = every Slack receiver's destination is knowable. Exit 1 = a lying channel.
set -euo pipefail

NS=${ALERTMANAGER_NAMESPACE:-grafana}
POD=${ALERTMANAGER_POD:-alertmanager-prom-alertmanager-0}
CFG=/etc/alertmanager/config_out/alertmanager.env.yaml
VERBOSE=""
LOCAL_CFG=""

while [ $# -gt 0 ]; do
	case "$1" in
	--verbose)
		VERBOSE="--verbose"
		;;
	--config-file)
		LOCAL_CFG=${2:-}
		if [ -z "$LOCAL_CFG" ]; then
			echo "check-slack-transport: --config-file needs a path" >&2
			exit 2
		fi
		shift
		;;
	*)
		echo "check-slack-transport: unknown argument '$1'" >&2
		exit 2
		;;
	esac
	shift
done

if [ -n "$LOCAL_CFG" ]; then
	if [ ! -r "$LOCAL_CFG" ]; then
		echo "check-slack-transport: cannot read $LOCAL_CFG" >&2
		exit 2
	fi
	CONFIG_TEXT=$(cat "$LOCAL_CFG")
else
	if ! command -v kubectl >/dev/null; then
		echo "check-slack-transport: kubectl not found" >&2
		exit 2
	fi
	# Read the file the operator rendered. `amtool config -o json` would also give
	# resolved output, but its `original` field is the same YAML and the JSON
	# wrapper adds a parse step for no benefit.
	if ! CONFIG_TEXT=$(kubectl -n "$NS" exec "$POD" -c alertmanager -- cat "$CFG"); then
		echo "check-slack-transport: could not read $CFG from $POD" >&2
		exit 2
	fi
fi

# Parse on the HOST with pyyaml via uv: the alertmanager image ships no PyYAML,
# and neither does the host python3 (same constraint check-alert-routing.sh works
# around). Output: one `<receiver>\t<transport>\t<channel>` row per slack config,
# where transport is `webhook`, `api`, or `unknown`. `config_out` is post-operator
# rendering, so global inheritance is already applied — which is exactly what is
# needed to classify the base receiver, whose transport the global token supplies.
rows=$(printf '%s' "$CONFIG_TEXT" | uv run --no-project --with pyyaml python3 -c '
import sys, yaml

cfg = yaml.safe_load(sys.stdin) or {}
glob = cfg.get("global") or {}
# Global app-token presence means a receiver with no api_url/api_url_file of its
# own inherits the bot-token transport (config.go sets api_url = slack_app_url).
global_app_token = bool(
    glob.get("slack_app_token") or glob.get("slack_app_token_file") or glob.get("slack_app_url")
)
for recv in cfg.get("receivers") or []:
    for sc in recv.get("slack_configs") or []:
        api_url = sc.get("api_url") or ""
        api_url_file = sc.get("api_url_file") or ""
        own_token = sc.get("app_token") or sc.get("app_token_file") or sc.get("app_url")
        # A file-backed URL is unresolved from here, and api_url_file in this
        # repo has only ever carried a webhook, so classify it as one. A false
        # positive is the safe direction for a check like this.
        if "hooks.slack.com" in api_url or api_url_file:
            transport = "webhook"
        elif "chat.postMessage" in api_url:
            transport = "api"
        elif own_token:
            # Receiver-level token; the operator renders api_url from app_url,
            # which is chat.postMessage in production.
            transport = "api"
        elif not api_url and not api_url_file and global_app_token:
            # Inherits the global slack_app_token_file -> chat.postMessage.
            transport = "api"
        else:
            transport = "unknown"
        print("%s\t%s\t%s" % (recv.get("name"), transport, sc.get("channel") or ""))
')

lying=0
unknown=0
checked=0
while IFS=$'\t' read -r name transport channel; do
	[ -z "${name:-}" ] && continue
	checked=$((checked + 1))
	case "$transport" in
	api)
		[ "$VERBOSE" = "--verbose" ] && echo "  ok       $name -> channel '$channel' is authoritative"
		;;
	webhook)
		if [ -n "$channel" ]; then
			echo "  LYING    $name uses a hooks.slack.com webhook but carries channel '$channel'"
			echo "           the webhook's bound channel decides delivery; this field is ignored"
			lying=$((lying + 1))
		else
			[ "$VERBOSE" = "--verbose" ] && echo "  ok       $name uses a webhook with no misleading channel"
		fi
		;;
	*)
		echo "  UNKNOWN  $name transport could not be classified"
		unknown=$((unknown + 1))
		;;
	esac
done <<EOF
$rows
EOF

echo
if [ "$checked" -eq 0 ]; then
	echo "check-slack-transport: FAIL — no Slack receivers found; the config did not parse as expected"
	exit 1
fi
if [ "$lying" -eq 0 ] && [ "$unknown" -eq 0 ]; then
	echo "check-slack-transport: PASS — $checked Slack receiver(s), every channel knowable"
	exit 0
fi
[ "$lying" -gt 0 ] && echo "check-slack-transport: FAIL — $lying receiver(s) claim a channel their transport ignores"
[ "$unknown" -gt 0 ] && echo "check-slack-transport: FAIL — $unknown receiver(s) have an unclassifiable transport"
exit 1
