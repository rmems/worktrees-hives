# AGENTS.md

## Purpose

This file defines how coding agents contribute to `worktrees-hives` and how the future hive runtime divides responsibility. The project is a Python/Rust hybrid designed for multiple agent platforms.

## Non-negotiable safety

**These rules are absolute. No agent, orchestrator, or platform override may relax them.**

### Core prohibitions

- **Never merge** a pull request or invoke a merge API. A human must merge.
- **Never use bare `git push --force`** or `git push -f`. Only `--force-with-lease` is permitted, and only for rebasing your own branch.
- **Never edit outside** a job's assigned worktree or branch.
- **Repository scope** is a **configured owner allowlist** (env `WH_ALLOWED_OWNERS` and/or explicit API args). There is no built-in default org; operators supply the owners they manage. Empty allowlist means deny-by-default for multi-owner discovery/scheduling unless a module documents otherwise (e.g. single-PR babysit with an explicit owner).
- **Limit code-fix commits** to three per PR per babysit cycle. Replies are unlimited.
- **Process stacked PRs** from the bottom of the stack upward.
- **Post review replies** only after pushing, and include the pushed SHA plus agent attribution.
- **GitHub MCP first (non-negotiable for agents):** For PR status, CI check runs, review threads, issue reads, and PR comments, use the **GitHub MCP** (`github__pull_request_read`, list/comment tools, etc.). Do **not** default to shell `gh` for reads. Shell `gh` is allowed only when MCP is unavailable (e.g. 503) or for operations MCP cannot perform. Local `git` remains for branch/rebase/push. Do **not** hardcode org/owner names in product code or agent docs — owners come only from `WH_ALLOWED_OWNERS` / explicit API args.

### Deny-list (never execute)

| Command / Operation | Reason |
| --- | --- |
| `gh pr merge` | Agents never merge PRs. |
| `gh pr merge --auto` | Auto-merge is still a merge. |
| `git push --force` (bare) | Destructive; loses history. |
| `git push -f` (bare) | Short form of same destructive push. |
| GraphQL `mergePullRequest` | Merge via API is still a merge. |
| REST `PUT /repos/.../merge` | Merge via REST is still a merge. |

### Allow-list for force-with-lease

`git push --force-with-lease` is permitted **only** when:
1. Rebasing your own feature branch onto an updated base.
2. Fixing a force-push that failed due to a stale remote ref.
3. The operator explicitly instructs a force-push.

Before using `--force-with-lease`, verify:
- Current branch is the assigned worktree branch (not `main` or another agent's branch).
- Remote ref matches expectations (no unexpected pushes from others).

### Fix-cap semantics

Each PR gets a maximum of **3 code-fix commits** per babysit cycle.

- **Counts:** Commits changing source code, tests, config, or behavior-affecting docs.
- **Does not count:** Merge commits from rebasing, CI-triggered commits, reply comments.
- **At cap:** Stop committing. Report residual issues as PR comments. Continue replying to reviews and monitoring CI.
- **Residual reporting:** Post a comment listing remaining CI failures, unresolved review threads, and recommended next steps.
- **Reset:** Cap resets when the operator starts a new babysit cycle.

### Branch/worktree pre-edit checklist

Before making any code change, verify:

1. **Worktree isolation:** `pwd` is inside the assigned worktree path.
2. **Branch correctness:** `git branch --show-current` matches the assigned feature branch.
3. **Clean state:** `git status` shows no uncommitted changes from other work.
4. **Remote alignment:** `git fetch && git status` confirms the branch tracks the expected remote.
5. **No cross-boundary edits:** No file outside the worktree is modified.

If any check fails, abort and report the mismatch.

### Final status guidance

When a babysit cycle ends, report:

- **PR status:** Open / Ready for review / Blocked
- **Fix count:** Number of code-fix commits pushed (e.g., "2/3")
- **Residual issues:** Unresolved CI failures, review comments, or blockers
- **Agent attribution:** Every PR comment and commit message includes agent identification

**Never claim you merged a PR.** If the PR appears merged, report "PR was merged by a human" and stop.

### Enforcement layers

These guardrails are enforced at multiple layers:

1. **Agent skill (`SKILL.md`):** Portable documentation and prompt templates. Not a security boundary.
2. **Python orchestrator:** Policy enforcement via subprocess bridge. Counts fix commits, validates paths, blocks deny-listed commands.
3. **Rust core (`wh-core`):** Hard enforcement. Rejects unsafe git/GitHub operations at the process boundary. Authoritative safety layer.

Rust must enforce safety-sensitive mutation rules. Skill instructions and Python checks provide defense in depth but are not sufficient on their own.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:7510c1e2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->

## Hybrid architecture

worktrees-hives is a **Python/Rust hybrid** designed so that each layer owns what it does best:

- **Rust** — performance, memory discipline, git worktrees, process supervision/timeouts, job state, and **hard safety enforcement** (never-merge, force-with-lease only, branch verification, path sandboxing).
- **Python** — orchestration policy, discover/partition, issue-to-PR and babysit loops, human reports, and agent glue.
- **Agent skill (`SKILL.md`)** — portable prompts describing when and how agents call the CLI on any platform.

```text
Agent / SKILL.md
       |
       | intent and operator context
       v
Python package: worktrees_hives
       |
       | wh subprocess calls + JSON envelope v1
       v
Rust binary: wh -> wh-core
       |
       | allowlisted subprocess operations
       v
git / gh / operating system
```

| Layer | Responsibilities |
| --- | --- |
| Agent skill | Describe when to discover work, spawn subagents, invoke the orchestrator, babysit PRs, and report results. Prompt content is portable guidance, not a security boundary. |
| Python orchestrator | Discover and partition work, enforce owner and per-cycle policy, order stacks, drive issue-to-PR and babysit loops, and build human-readable reports. |
| Rust core and CLI | Resolve sandboxed paths, create and remove worktrees, persist atomic job state, supervise child processes, verify branches, and reject unsafe git/GitHub operations. |
| External tools | `git` and `gh` perform only operations selected and validated by Rust. The OS supplies filesystem and process primitives. |

**Why this split?** Rust enforces safety-sensitive mutation rules at the binary boundary so a malformed prompt or Python bug cannot bypass them. Python handles orchestration logic that benefits from rapid iteration and rich ecosystem tooling. The agent skill layer remains portable across platforms without coupling to either runtime.

The stable cross-language boundary is a CLI with JSON envelopes. PyO3 is out of scope for v1. The contract is versioned independently so Python and Rust can evolve without sharing an in-process ABI.

## Source ownership

### Rust

Rust code lives in `crates/`:

- `crates/wh-core/` is the reusable library and source of truth for worktrees, state, process execution, paths, and safety policy.
- `crates/wh/` is the `wh` command-line adapter. It parses arguments, calls `wh-core`, emits human or JSON output, and maps policy failures to exit code 2.

Keep security boundaries in `wh-core`, not only in the CLI parser. Git must be invoked as a subprocess rather than through libgit2. New mutating commands require branch verification and path-sandbox tests.

### Python

Python code will live in `python/src/worktrees_hives/`:

- The subprocess bridge locates `wh` through `WH_BIN` or `PATH` and validates JSON responses.
- Discovery, partitioning, issue-to-PR, babysit, and reporting modules own high-level policy.
- Python must not reimplement Rust-owned worktree, state, branch, or git safety checks.
- The three-code-fix-commit budget is a Python orchestration rule; Rust still rejects unsafe individual commands.

### Agent skill

The installable `SKILL.md` will own platform-facing prompts and command guidance. It may adapt spawning instructions to a host platform, but it must preserve the same safety invariants and call the Python/Rust boundary instead of bypassing it.

## Data flow

1. The operator or agent supplies GitHub or Linear issue/PR context.
2. Python discovers eligible work under the owner allowlist and partitions independent jobs.
3. Rust allocates `{base}/{owner}/{repo}/{job_id}` and creates the assigned branch worktree.
4. A worker agent changes only that worktree and branch.
5. Rust validates mutations and performs allowlisted `git` or `gh` subprocess calls.
6. Python opens or checks the PR, processes stacks bottom-up, applies the fix budget, and reports residual blockers.
7. After a pushed fix, the agent replies with SHA and attribution.
8. The cycle ends when the PR is merge-ready or blocked. A human remains responsible for any merge.

GitHub is the product issue source. Linear may mirror product planning for the operator's team; that team id is operator-local, not a product default. Beads tracks session claims, dependencies, and completion locally; it is not a replacement for GitHub product issues.

## Runtime paths and overrides

| Purpose | Default | Override |
| --- | --- | --- |
| Worktree root | `~/.local/share/worktrees-hives/worktrees` | `WH_WORKTREE_BASE` |
| Job worktree | `{worktree root}/{owner}/{repo}/{job_id}` | Derived only; must remain sandboxed |
| Watched state | `~/.local/share/worktrees-hives/watched.json` | `WH_STATE_PATH` |
| Rust binary used by Python | `wh` from `PATH` | `WH_BIN` |

Use platform-aware XDG/user-data resolution in implementation. Never assume a Linux-only home-directory layout when an OS API is available.

## JSON and process boundary

Version 1 responses use this envelope shape:

```json
{"ok":true,"schema_version":1,"command":"state.show","data":{},"error":null}
```

- Standard output is machine-readable JSON when `--json` is selected.
- Diagnostics belong on standard error.
- Additive fields are compatible within v1; removals or semantic renames require a schema-version change.
- `run-with-timeout` is reserved for the later process-supervisor work and must not be improvised in the foundation CLI.

See GitHub #40 and the planned `docs/json-contract.md` for the complete contract.

## Contribution workflow

Follow the portable worker contracts. They apply to every agent platform.

1. **[Safe Issue → Verified Commit](docs/workflows/safe-issue-verified-commit.md)** ([#84](https://github.com/rmems/worktrees-hives/issues/84), isolation [#6](https://github.com/rmems/worktrees-hives/issues/6)): read the issue and repo docs, isolate a worktree/branch, implement, run README gates, commit, push, comment on the issue with SHA. Never edit `main`.
2. **[Safe Verified Commit → PR](docs/workflows/safe-verified-commit-to-pr.md)** ([#8](https://github.com/rmems/worktrees-hives/issues/8) / [RM-123](https://linear.app/rpd-34/issue/RM-123/issue-pr-workflow-never-auto-merge)): open or update a PR that links the issue, hand off URL + SHA, never merge. Review checklist: [`REVIEW.md`](REVIEW.md). Babysit is a later cycle ([#9](https://github.com/rmems/worktrees-hives/issues/9)).

## Review expectations

Use [`REVIEW.md`](REVIEW.md) for the shared checklist. Reviewers should verify behavior at both the soft-policy and hard-enforcement layers, with particular attention to merge prohibition, force-push parsing, expected-branch checks, path traversal, JSON compatibility, and cross-platform path handling.

## Related planning

- Hybrid foundation: GitHub #21
- Rust core: GitHub #22 and #24–#29
- Python orchestration: GitHub #23, #30, and #37–#39
- Hybrid glue and docs: GitHub #40–#42
- Linear project: <https://linear.app/rpd-34/project/worktrees-hives-e3052de4caa3>
