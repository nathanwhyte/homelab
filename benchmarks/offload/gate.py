"""The 90-case batch gate that run_shadow.py runs and score.py scores, kept in one place.

Case files live under benchmarks/results/<case>/cases.json in this repo.
"""

# (case file, --only lane, expected case count); each lane runs on its own because
# `--only triage` also runs every other triage lane and crashes on a case file
# that lacks that lane's cases key.
GATE = [
    ("batch-skill-gate-20260828-postfix2", "summary", 20),
    ("fence-mistag-20260830", "fence", 15),
    ("triage-skills-20260830", "blocker", 23),
    ("triage-skills-20260830", "staleness", 12),
    ("compaction-triage-20260831", "compaction", 20),
]

LANES = [lane for _, lane, _ in GATE]

# Cases homelab#193 marks ambiguous; score.py reports totals with and without them.
AMBIGUOUS = {"IDEA-1027->PROJ-1018#0", "BUG-152"}
