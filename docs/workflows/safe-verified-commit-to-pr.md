# Safe Verified Commit → PR

Portable worker contract for any coding agent. Runs **after** [Safe Issue → Verified Commit](safe-issue-verified-commit.md). Opens or updates a human-reviewable pull request. Never merges.

Safety rules live in [`AGENTS.md`](../../AGENTS.md), [`SKILL.md`](../../SKILL.md), and [`REVIEW.md`](../../REVIEW.md). This file does not relax them.

Contract: Issue → PR [#8](https://github.com/rmems/worktrees-hives/issues/8) / Linear [RM-123](https://linear.app/rpd-34/issue/RM-123/issue-pr-workflow-never-auto-merge). Isolation prerequisite [#6](https://github.com/rmems/worktrees-hives/issues/6). Babysit is **not** this workflow ([#9](https://github.com/rmems/worktrees-hives/issues/9) / RM-124).

## Inputs

| Input | Required | Notes |
| --- | --- | --- |
| Verified push | yes | Branch already pushed; local gates already passed or residuals already reported |
| `issue` | yes | GitHub issue this PR implements |
| `owner` / `repo` | no | Resolve from `git remote` if omitted |
| `dry_run` | no | Describe the PR you would open. Do not create or update it |

One issue → one PR unless the issue explicitly groups work.

## Hard stops

- No verified push yet — run the commit workflow first.
- Shared `main`/`master` checkout — work only in the job worktree/branch.
- Owner outside the configured allowlist unless the operator named this job.
- Any merge command, merge API, auto-merge, or merge-queue enablement.
- Bare `git push --force` / `git push -f`.
- Opening a no-op “kick CI” PR.

## Stages

### 8. Open or update the PR

GitHub **MCP first**. Shell `gh` only if MCP is unavailable.

1. Confirm you are still on the job branch inside the job worktree (`git rev-parse --show-toplevel`, `git branch --show-current`).
2. `git fetch`. Re-read `HEAD` after any last rebase/push: `git rev-parse HEAD`.
3. Base = repository default branch, or the stack parent if this is a stacked PR. Process stacks **bottom-up**.
4. If a PR already exists for this branch, update it. Do not open a second PR for the same issue.
5. Otherwise create the PR:

   - title reflects the issue
   - body links the issue (`Fixes #<n>` only when the issue is fully done; otherwise `Refs #<n>`)
   - no merge flags
   - do not enable auto-merge

Suggested body:

```markdown
## Summary

<what changed>

## Issue
Fixes #<n>   <!-- or Refs #<n> if partial -->

## Test plan
- [ ] Local gates from README.md
- [ ] CI on PR

## Notes for babysit
- Known residuals: ...
```

### Partial / blocked

- Code exists but the issue is incomplete: open a **draft** or clearly partial PR with a Remaining section.
- Zero commits: do **not** open an empty PR. Comment residuals on the issue instead.

### 9. Handoff

After the create/update call, record at least:

| Field | Rule |
| --- | --- |
| `repo` | `owner/repo` |
| `issue` | issue number |
| `pr` | PR number |
| `url` | PR URL |
| `branch` | job branch |
| `head_sha` | `git rev-parse HEAD` **after** the last push |
| `status` | open / draft / blocked |
| `notes` | residuals |

Comment on the GitHub issue with PR URL, SHA, residuals, and agent name. Cross-link a Linear twin only if it already exists.

If an orchestrator is present, this handoff is what enqueueing babysit (#9) consumes. This workflow does not start babysit.

### 10. Never merge

Success is **PR opened (or updated) and handoff ready**, not “landed on main.”

Never:

- `gh pr merge` (including `--auto`)
- GraphQL `mergePullRequest`
- REST `PUT /repos/.../merge`
- merge-queue / auto-merge enablement
- claiming the agent merged the PR

If the PR is already merged, report that a human merged it and stop.

## Done when

An open (or draft) PR links the issue, `head_sha` matches the last push, the issue comment includes URL + SHA + agent, and no merge path was invoked.
