# Safe Issue → Verified Commit

Portable worker contract for any coding agent (Codex, Claude, Grok, Hermes, Devin, or later). Stops at a verified push and an issue comment. Does not open a PR — that is [Safe Verified Commit → PR](safe-verified-commit-to-pr.md).

Safety rules live in [`AGENTS.md`](../../AGENTS.md) and [`SKILL.md`](../../SKILL.md). This file does not relax them.

Contracts: isolation [#6](https://github.com/rmems/worktrees-hives/issues/6), skill/procedure [#84](https://github.com/rmems/worktrees-hives/issues/84).

## Inputs

| Input | Required | Notes |
| --- | --- | --- |
| `issue` | yes | GitHub issue URL or number |
| `owner` / `repo` | no | If omitted, resolve from `git remote` in the current repository |
| `dry_run` | no | Intake + isolate + plan only. No commit, push, or issue comment |

Do not hard-code an owner. Multi-repo discovery and scheduling still use `WH_ALLOWED_OWNERS` / explicit API args.

## Hard stops

Abort and report if any of these fail:

- Issue is closed, is a pull request, or has no actionable acceptance criteria
- Owner is outside the configured allowlist (unless the operator named this repo/job explicitly)
- Worktree path, branch, remote, upstream, or cleanliness check fails
- `wh` is missing and no enforcing wrapper is available (mutating runs)
- Any required quality gate fails or times out
- A deny-listed command would be required (merge, bare `--force` / `-f`)
- `git push` exits non-zero or the remote rejects the push
- This is already a babysit cycle and the 3 code-fix commit cap is exhausted

## Stages

### 1. Intake (read-only)

1. Read the GitHub issue with **GitHub MCP first**. Shell `gh` only if MCP is unavailable.
2. Read `AGENTS.md` or `CLAUDE.md`, `README.md`, and [`REVIEW.md`](../../REVIEW.md).
3. Extract acceptance criteria. Preserve them. Do not invent extra scope.
4. If a Linear twin is already linked, note its id. Do not create a second twin.

### 2. Isolate

1. If this repo uses Beads, run `bd prime`, inspect `bd ready`, and claim the relevant bead.
2. Start from an up-to-date base. Never edit `main` or `master`.
3. Create or reuse a dedicated branch and isolated worktree:
   - Required: `wh --json worktree create` (`WH_BIN` or `PATH`).
   - If `wh` is missing, a platform-specific wrapper may call Git only after it has enforced: worktree root under the configured base, no path traversal, expected job branch, expected remote, owner allowlist from `WH_ALLOWED_OWNERS` or explicit API args, and assigned-worktree identity.
   - Raw `git worktree add` is forbidden on mutating runs.
4. Suggested issue branch: `hive/issue-<n>-<short-slug>` (document any local override).
5. Run the pre-edit checklist in `AGENTS.md` / `SKILL.md`: worktree path, `git branch --show-current`, clean tree, expected remote, no path escape.
6. One writable worktree per job. Do not share it with another agent.

### 3. Implement

- Change only that worktree and branch.
- Stay inside Rust / Python / skill ownership (`AGENTS.md`).
- No drive-by refactors. No files outside the worktree.

### 4. Validate (fail-closed)

Run the narrowest relevant checks first, then the gates in `README.md`.

For this repository, unless the issue names a smaller subset:

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

If `python/` changed, also run the Python test extra documented in `README.md`.

Run every `cargo` and Python check under an explicit process timeout supplied by the host (orchestrator supervisor, CI job timeout, or equivalent). Do not use a Linux-only timeout command as the contract.

If a required gate fails or the process is killed for time, **do not commit**. Report the failure or timeout as a residual on the issue. A hang or timeout is not a license to skip the gate and commit anyway.

### 5. Commit

- One or few focused commits. Scoped `git add` (no secrets, no unrelated dirt).
- Message names the agent and links the GitHub issue (and Linear id if already known).
- Beads status matches reality.

### 6. Push

Immediately before `git pull --rebase` or `git push`, fail closed if any of these differ from the assigned job: worktree path (`git rev-parse --show-toplevel`), job branch (`git branch --show-current`), remote, or upstream. Abort and report. Do not rebase or push on a mismatch.

Then:

1. `git pull --rebase`. Require a **successful, conflict-free** rebase before anything else. If rebase fails (non-zero exit) or leaves conflicts, **stop**: record the rebase issue as a residual, do **not** run validation gates, and do **not** `git push`.
2. Re-run **all** required validation gates from stage 4 (same process timeouts) on the rebased tree. The commit about to be pushed must be covered. If any gate fails or times out, **do not push**. Report residuals.
3. `git push`. If the command exits non-zero or the remote rejects the update, **stop before Stage 7**: record the push failure as a residual. Do **not** report `git rev-parse HEAD` as the pushed SHA.

- Never merge.
- Never `git push --force` or `git push -f`.
- `--force-with-lease` only on **this** job branch after a rebase you own, after the identity check above succeeds.

### 7. Report

Run this stage **only** after Stage 6 step 3 succeeded (push accepted by the remote).

Comment on the GitHub issue (MCP first) with:

- branch
- pushed SHA (`git rev-parse HEAD` **after** that successful push)
- what landed
- residual blockers
- agent name

Do not claim a merge. Do not open a PR here. A local HEAD SHA after a failed or rejected push is not a completion report.

## Dry run

Stop after isolate + a written implementation plan. No commits, push, or issue comment.

## Done when

Branch is pushed (remote accepted), required gates passed (or residuals are explicit and nothing was committed or reported as pushed over a failed gate, timeout, rebase failure, or rejected push), and the issue has a SHA-bearing comment only after that successful push.
