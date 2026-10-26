#!/usr/bin/env bash
# Compatibility entrypoint; model facts and all acceptance live in the module.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
[[ $# -le 1 ]] || { echo 'usage: ollama-warm.sh [--reconcile-only | --ov-pair-only]' >&2; exit 2; }
case ${1-} in
"") command=warm ;;
--reconcile-only) command=prepare ;;
--ov-pair-only) command=pair ;;
-h | --help)
	echo 'usage: ollama-warm.sh [--reconcile-only | --ov-pair-only]'
	exit 0
	;;
*) echo "unknown argument: $1" >&2; exit 2 ;;
esac
exec python3 "$HERE/ollama_prepare.py" "$command"
