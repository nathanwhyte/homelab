# Homelab — agent instructions (pi, Codex)

3-node K3s cluster (manu, timmy, wemby) running AI/RAG workloads. The full
manual is [`CLAUDE.md`](CLAUDE.md): topology, secrets, OpenViking config,
network, failover, and per-service notes. Read it before any cluster-facing
change. This file is the slim port: the rules that apply to every edit. Keep
the two in step.

## Ground rules

- **Never mutate the live cluster unless explicitly asked.** No
  `kubectl apply`, `helm install/upgrade`, `kubectl scale`, or deletes on your
  own initiative. Reads (`get`, `describe`, `logs`, `diff`) are fine.
- **Apply the object you changed, never the whole file.** Run
  `kubectl diff -f <target>` first and read it: every `+` you did not intend
  is an object you are about to create. A multi-document manifest often holds
  objects the cluster deliberately does not run; `created` in the output
  instead of `configured` is the giveaway. Narrow with a single-object file or
  `-l <selector>`.
- **Never introduce or commit secrets.** Reference existing `Secret` objects
  by name; use a placeholder and document what is required. Scripts take
  credentials from env vars or pre-created secrets, never inline.
- **Do not change global tooling config** (git, shell) unless asked.

## Repo layout and git

- **`~/code/homelab` is a bare repo.** Run status, tests, formatting, commits,
  and rebases from a worktree; use the bare root only for worktree
  administration (`git -C ~/code/homelab worktree …`). `main` lives at
  `~/code/homelab/main/`; every other branch is a sibling worktree. Run
  `git worktree list` first and work in the worktree that owns your branch.
  Repo paths are `~/code/homelab/main/<dir>`.
- **Never switch the `main/` worktree to another branch.** New work:
  `git -C ~/code/homelab worktree add <slug> -b <type>/<desc> origin/main`,
  then `cd ~/code/homelab/<slug>`. Branch types: `feat/`, `fix/`, `docs/`,
  `scan/`.
- **PRs target `main` and are squash-merged** (the only strategy the repo
  allows). `git fetch origin main && git rebase origin/main` before pushing;
  never force-push `main`.
- **Clean up after merge:** `git -C ~/code/homelab worktree remove <slug>` and
  delete the local branch. If a worktree directory was moved, use
  `git worktree repair <path>`, **never** `git worktree prune`.
- Preserve uncommitted changes in every worktree; touch only your own. Stage
  named files, commit only when asked.

## Editing and validation

- YAML: 2-space indent, explicit names and namespaces, stable labels and
  selectors. Keep diffs reviewable; no drive-by renames.
- Helm values files hold overrides only; comment the _why_ on security,
  persistence, ingress, and resource-limit settings.
- Nested markdown filenames are lowercase-kebab; `README.md`, `CLAUDE.md`,
  `AGENTS.md`, `SKILL.md`, `HARDWARE.md`, `TORN-DOWN.md`, `RETIRED.md` keep
  their casing.
- Before committing: `bash -n <script>`, and `shellcheck` / `yamllint` when
  available. Manifests must be valid YAML on their own; do not rely on the
  cluster to validate them.
- Keep changes scoped to the service you are touching. Update `README.md`
  only when adding or removing a service or changing how deployment works.
