# worktrees-hives Skill

Installable agent skill for the worktrees-hives hybrid orchestrator.

## When to use

Use this skill when:
- Discovering work from GitHub or Linear issues
- Spawning worker subagents for code changes
- Running [Safe Issue → Verified Commit](docs/workflows/safe-issue-verified-commit.md) then [Safe Verified Commit → PR](docs/workflows/safe-verified-commit-to-pr.md)
- Babysitting pull requests through CI
- Reporting results back to the operator

## Safety Guardrails

**These rules are NON-NEGOTIABLE. No agent, orchestrator, or platform override may relax them.**

### Deny-list (never execute)

| Command / Operation | Reason |
| --- | --- |
| `gh pr merge` | Agents never merge PRs. A human must merge. |
| `gh pr merge --auto` | Same prohibition; auto-merge is a merge. |
| `git push --force` (bare) | Destructive; loses history. Use `--force-with-lease` only. |
| `git push -f` (bare) | Short form of the same destructive push. |
| GraphQL `mergePullRequest` | Merge via API is still a merge. |
| REST `PUT /repos/.../merge` | Merge via REST is still a merge. |
| Any merge commit creation | `git merge` in working tree to combine PR branches. |

### Allow-list for force-with-lease

`git push --force-with-lease` is permitted **only** when:
1. The agent is rebasing its own feature branch onto an updated base.
2. The agent is fixing a force-push that failed due to a stale remote ref.
3. The operator explicitly instructs a force-push.

Before using `--force-with-lease`, the agent MUST:
- Verify the current branch is the assigned worktree branch (not `main`, `master`, or another agent's branch).
- Confirm the remote ref is what the agent expects (no unexpected pushes from others).

### Fix-cap semantics

**Rule:** Each PR gets a maximum of **3 code-fix commits** per babysit cycle.

- **What counts:** Commits that change source code, tests, configuration, or documentation that affects behavior.
- **What does not count:** Merge commits from rebasing, CI-triggered commits (e.g., lock file updates), reply comments on the PR.
- **When the cap is hit:** The agent MUST stop committing and report residual issues as PR comments. The agent continues to reply to review comments and monitor CI, but does not push new code changes.
- **Residual reporting:** When the cap is reached, the agent posts a comment listing: remaining CI failures, unresolved review threads, and recommended next steps for a human or next cycle.
- **Reset:** The cap resets when the operator starts a new babysit cycle (explicit restart, not automatic).

### Branch/worktree pre-edit checklist

Before making any code change, the agent MUST verify:

1. **Worktree isolation:** `pwd` is inside the assigned worktree path (`{worktree_root}/{owner}/{repo}/{job_id}`).
2. **Branch correctness:** `git branch --show-current` matches the assigned feature branch.
3. **Clean state:** `git status` shows no uncommitted changes from other work.
4. **Remote alignment:** `git fetch && git status` confirms the branch tracks the expected remote.
5. **No cross-boundary edits:** No file outside the worktree is modified (no `../` paths, no absolute paths outside the worktree root).

If any check fails, the agent MUST abort and report the mismatch.

### Final status guidance

When a babysit cycle ends (successfully or at cap), the agent reports:

- **PR status:** Open / Ready for review / Blocked
- **Fix count:** Number of code-fix commits pushed in this cycle (e.g., "2/3")
- **Residual issues:** List of unresolved CI failures, review comments, or blockers
- **Agent attribution:** Every PR comment and commit message includes agent identification

The agent MUST NOT claim it merged the PR. If the PR appears merged, the agent reports "PR was merged by a human" and stops.

### Platform-neutral worker prompt template

When spawning a worker subagent, include these safety instructions in the prompt:

```
SAFETY RULES (non-negotiable):
- NEVER merge a PR or invoke any merge API/CLI
- NEVER use bare `git push --force` or `git push -f`
- `git push --force-with-lease` is allowed only for rebasing your own branch
- NEVER edit files outside your assigned worktree
- NEVER commit more than 3 code-fix commits per babysit cycle
- Before editing, verify: worktree path, branch name, clean state
- After pushing, reply with SHA and agent attribution
- When at cap, report residual issues; do not push more code
```

### Enforcement layers

These guardrails are enforced at multiple layers:

1. **Agent skill (this file):** Portable documentation and prompt templates. Not a security boundary — agents may bypass if not constrained by the platform.
2. **Python orchestrator:** Policy enforcement via the subprocess bridge. Counts fix commits, validates paths, and blocks deny-listed commands before they reach Rust.
3. **Rust core (`wh-core`):** Hard enforcement. Rejects unsafe git/GitHub operations at the process boundary. This is the authoritative safety layer.

Defense in depth: all three layers enforce the same rules. A failure in one layer is caught by another.
