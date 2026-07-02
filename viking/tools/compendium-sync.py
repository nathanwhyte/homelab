#!/usr/bin/env python3
"""Stub — the canonical compendium-sync client lives in the vault.

This copy diverged from ~/code/compendium/_scripts/compendium-sync.py (different
retry counts, default endpoint, health auth) and incident hardening was landing
in one copy but not the other (IMPR-1026). The vault copy is canonical — it is
what every skill, workflow, and log.md re-run command invokes. This stub execs
it so existing docs and invocations of this path keep working.

Plan: ~/code/compendium/docs/plans/2026-07-02-IMPR-1026-ov-agent-side-reliability.md
"""

import os
import sys
from pathlib import Path

CANONICAL = Path("~/code/compendium/_scripts/compendium-sync.py").expanduser()

if not CANONICAL.is_file():
    sys.stderr.write(f"error: canonical sync script not found at {CANONICAL}\n")
    sys.exit(1)

os.execv(sys.executable, [sys.executable, str(CANONICAL), *sys.argv[1:]])
