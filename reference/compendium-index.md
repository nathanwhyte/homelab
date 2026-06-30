# Compendium + OV index reference

OV sync targets and compendium vault lookups. Lives in `reference/` (not `CLAUDE.md`) so
the SessionStart hook stays under the 40k char cap.

## Indexed compendia

| Vault                | Source              | OV target                        | Sync tool                         |
| -------------------- | ------------------- | -------------------------------- | --------------------------------- |
| Compendium (unified) | `~/code/compendium` | `viking://resources/compendium/` | `viking/tools/compendium-sync.py` |

Legacy `personal-compendium` was merged into `~/code/compendium` (June 2026, IDEA-034,
+1000 ID offset); old repo archived at `~/code/archive/personal-compendium/`. Personal-band
entries (IDs ≥ 1000) sync with
`COMPENDIUM_ROOT=~/code/compendium OV_TARGET_BASE=viking://resources/personal/`.
