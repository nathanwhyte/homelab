#!/usr/bin/env bash
# Moved (BUG-1034 phase 2): the in-cluster compendium→OV sync runner now lives
# in the compendium namespace. This stub keeps old invocations working.
exec "$(dirname "$0")/../../compendium/cluster-sync.sh" "$@"
