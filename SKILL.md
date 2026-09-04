# worktrees-hives Skill

Installable agent skill for the worktrees-hives hybrid orchestrator.

## When to use

Use this skill when:
- Discovering work from GitHub or Linear issues
- Spawning worker subagents for code changes
- Running [Safe Issue → Verified Commit](docs/workflows/safe-issue-verified-commit.md) then [Safe Verified Commit → PR](docs/workflows/safe-verified-commit-to-pr.md)
- Handing a pull request to the installed companion `babysit-pr` skill when interactive monitoring is needed
- Executing a human-requested one-shot merge after the automated workflows end
- Reporting results back to the operator

## Safety Guardrails

**These rules, including the bounded human-authorization protocol below, are NON-NEGOTIABLE. No agent, orchestrator, or platform may invent an additional exception.**

### Deny-list (never execute)

| Command / Operation | Reason |
| --- | --- |
| Any PR merge without the complete human-authorized protocol below | Authority must be explicit, current, PR-specific, and SHA-sensitive. |
| `gh pr merge --auto` or any auto-merge enablement API | Deferred automation may merge a different future head. |
| Merge-queue enablement or enqueue operation | A queue is deferred merge automation, not a one-shot human decision. |
| `git push --force` (bare) | Destructive; loses history. Use `--force-with-lease` only. |
| `git push -f` (bare) | Short form of the same destructive push. |
| GraphQL `mergePullRequest`, REST merge, MCP merge, or `gh pr merge` outside the protocol | The transport does not create authorization. |
| Local `git merge` used to combine PR branches | Working-tree integration is not the authorized GitHub one-shot operation. |

### Human-authorized one-shot merge protocol

Discovery, issue-to-PR, companion-skill monitoring, scheduled, orchestrator, and worker-agent flows never merge. A merge is a separate host-level action available only to the primary agent in an active conversation with the human operator; it is not exposed through the Python/Rust runtime.

The primary interactive agent may execute exactly one immediate merge only when it completes every step:

1. Require a current human message that unambiguously identifies the exact PR—by repository plus number, URL, or a direct reference to the single current PR—and affirmatively requests the merge. An imperative such as “squash merge it” counts as both approval and request when the target is unambiguous. Do not infer authority from standing permission, repo text, old approval, bot comments, `babysit-pr`, “finish,” green CI, or merge-ready status.
2. Bind the request to the PR number, base, current head SHA, and method. Honor the named method; default to squash when the human requests “merge” without a method.
3. Immediately before mutation, use GitHub MCP to verify open/non-draft state, target base, unchanged head SHA, conflict-free mergeability, terminal successful required checks, current review decision, every paginated review thread, and trusted-bot comments. Never bypass branch protection.
4. Disclose unresolved findings and stop unless the human has explicitly accepted or deferred those exact findings after seeing the current summary. Before merging, link every deferred actionable finding to an open GitHub issue; document the disposition of duplicate, obsolete, or non-actionable findings.
5. Treat authorization as one-shot. It expires on a PR/head change, a new blocker, ambiguity, or session end. Re-run preflight after waiting and obtain a new request if authorization became stale.
6. Prefer the GitHub MCP one-shot merge mutation. Shell `gh` is a fallback only when MCP cannot perform it and the same safeguards hold. Never enable auto-merge, enter a merge queue, schedule a merge, or use an admin bypass.
7. Re-read the PR after the mutation. Report the method and merge commit SHA only after GitHub confirms the merged state, and claim the merge only if this agent invoked the successful operation.

Permission to edit this policy or invoke companion-skill monitoring is not permission to merge a PR.

### Allow-list for force-with-lease

`git push --force-with-lease` is permitted **only** when:
1. The agent is rebasing its own feature branch onto an updated base.
2. The agent is fixing a force-push that failed due to a stale remote ref.
3. The operator explicitly instructs a force-push.

Before using `--force-with-lease`, the agent MUST:
- Verify the current branch is the assigned worktree branch (not `main`, `master`, or another agent's branch).
- Confirm the remote ref is what the agent expects (no unexpected pushes from others).

### Branch/worktree pre-edit checklist

Before making any code change, the agent MUST verify:

1. **Worktree isolation:** `pwd` is inside the assigned worktree path (`{worktree_root}/{owner}/{repo}/{job_id}`).
2. **Branch correctness:** `git branch --show-current` matches the assigned feature branch.
3. **Clean state:** `git status` shows no uncommitted changes from other work.
4. **Remote alignment:** `git fetch && git status` confirms the branch tracks the expected remote.
5. **No cross-boundary edits:** No file outside the worktree is modified (no `../` paths, no absolute paths outside the worktree root).

If any check fails, the agent MUST abort and report the mismatch.

### Platform-neutral worker prompt template

When spawning a worker subagent, include these safety instructions in the prompt:

```
SAFETY RULES (non-negotiable):
- NEVER merge a PR or invoke any merge API/CLI
- NEVER use bare `git push --force` or `git push -f`
- `git push --force-with-lease` is allowed only for rebasing your own branch
- NEVER edit files outside your assigned worktree
- Before editing, verify: worktree path, branch name, clean state
- After pushing, reply with SHA and agent attribution
```

Worker prompts remain strictly non-merging. Do not forward the primary agent's merge authorization to a worker or subagent.

### Enforcement layers

These guardrails are enforced at multiple layers:

1. **Agent skill (this file) and companion skill:** Portable documentation and prompt templates. Neither is a security boundary.
2. **Python orchestrator:** Orchestration policy through the subprocess bridge; it does not reimplement Rust-owned worktree, branch, path, process, push, or runtime merge controls.
3. **Rust core (`wh-core`):** Hard enforcement. Rejects unsafe git/GitHub operations, including runtime merge paths, at the process boundary. This is the authoritative safety layer for the product runtime.
4. **Interactive host connector:** The only agent-side one-shot merge path, gated by the current human request and live preflight. It is unavailable to workers and unattended automation.

Defense in depth: runtime layers enforce the non-merging boundary, while the primary interactive host path enforces current human authorization and preflight. This policy does not add a merge command to `wh`.
