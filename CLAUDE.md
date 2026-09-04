# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

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

## Commit attribution

Every Codex-authored commit must include the exact trailers `Agent: Codex` and `Co-authored-by: Codex <noreply@openai.com>`. Never rewrite a Cursor-authored or Cursor-co-authored commit merely to change attribution; add a new correctly attributed commit instead.

## Human-authorized merges

Interactive PR monitoring belongs to the installed companion `babysit-pr` skill. That skill is portable operator guidance, not a security boundary. Rust `wh-core` remains the hard code-enforced boundary for worktree, branch, path, process, push, runtime no-merge, auto-merge, and merge-queue controls.

Merge authority is deny-by-default. The hive runtime, Python orchestrator, Rust CLI/core, interactive monitoring flows, scheduled jobs, and spawned worker agents never merge. Only the primary interactive agent may execute a one-shot merge after the human unambiguously identifies and affirmatively requests the exact pull request in the active conversation. A direct imperative such as “squash merge it” supplies approval when “it” clearly refers to the single current PR.

Follow the complete [human-authorized one-shot merge protocol in `AGENTS.md`](AGENTS.md#human-authorized-one-shot-merge-protocol). In particular:

- `babysit-pr`, green CI, “merge-ready,” a prior or standing approval, and permission to edit this policy do not authorize a merge.
- Bind authorization to the current PR, head SHA, base, and method; default to squash only when the human requests a merge without naming a method.
- Immediately before merging, use GitHub MCP to re-check the PR state, head SHA, mergeability, required checks, review decision, and all paginated review threads and trusted-bot comments.
- Disclose unresolved findings. Deferred actionable findings require the human's explicit acceptance after disclosure and a linked open GitHub issue before merge.
- Authorization expires on a head/target change, a new blocker, ambiguity, or session end. Never enable auto-merge, use a merge queue, schedule a later merge, or bypass branch protection.
- Prefer the GitHub MCP one-shot mutation, verify the merged state afterward, and claim the action only if this agent invoked it successfully.

## Build & Test

_Add your build and test commands here_

```bash
# Example:
# npm install
# npm test
```

## Architecture Overview

_Add a brief overview of your project architecture_

## Review expectations

Use [`REVIEW.md`](REVIEW.md) for the shared checklist. Reviewers should verify behavior at both the soft-policy and hard-enforcement layers, with particular attention to runtime merge prohibition and interactive merge authorization/preflight, force-push parsing, expected-branch checks, path traversal, JSON compatibility, cross-platform path handling, alternate or interactive interpreter routes, platform-specific wrapper operands and command-lookup overrides, nested environment resets and unsets, runtime configuration, lab child commands, ambient Git pagers and fsmonitor hooks, optional index-lock suppression, partial-clone lazy fetches, exact case-sensitive Git built-ins, positional config actions, named, peeled, sorted, and ref-format signature settings, alternate-ref options across revision consumers, clustered patch flags, and Git reads or mutations that can launch nested helpers, hooks, filters, viewers, transports, signature tools, credential helpers, diff tools, aliases, or archive formatters without preserving their capability requirements.

## Conventions & Patterns

_Add your project-specific conventions here_
