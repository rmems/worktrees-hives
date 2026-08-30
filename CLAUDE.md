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

- Use `bd` as the lightweight canonical task state: one task per cohesive tranche, claimed before code. Do not substitute TodoWrite, TaskCreate, or markdown TODO lists.
- Run `bd prime` for command reference when needed. Acceptance text, Linear sync, GitHub child issues, project metadata, and audit reports may follow implementation but must be complete by PR handoff.
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files.

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Session Completion

For authorized implementation, complete the cohesive tranche: run focused gates during work and the full native gates once before push; commit, push, and create or update the PR for handoff. Record remaining follow-up in Beads and complete required tracking and metadata by PR handoff. Do not add redundant full-suite runs, serial audits, or cleanup that is unrelated to the tranche. A specific user instruction that withholds a push or PR action controls that action. Merge is always a separate, explicitly authorized operation.
<!-- END BEADS INTEGRATION -->

## Commit attribution

Every Codex-authored commit must include the exact trailers `Agent: Codex` and `Co-authored-by: Codex <noreply@openai.com>`. Never rewrite a Cursor-authored or Cursor-co-authored commit merely to change attribution; add a new correctly attributed commit instead.

## Team-maintainer fast path

An explicit user request to implement scoped work authorizes the assigned team maintainer to create the scoped branch/worktree, edit code, commit, make the first push, and create the PR without repeated confirmation. That authority never authorizes a merge, auto-merge, merge queue, destructive action, or work outside the assigned scope; Rust remains the hard enforcement boundary.

- **Beads:** Use Beads as lightweight canonical state: one task per cohesive tranche and claim it before coding. Complete acceptance prose, Linear sync, GitHub child issues, project metadata, and audit reports may follow implementation, but must be complete by PR handoff rather than blocking the first edit.
- **Isolation and alignment:** A dirty or stale primary checkout is not a blocker. Preserve it, bootstrap a clean source/clone and use `wh` for the assigned worktree. A newly created, unpublished assigned branch may be fast-forwarded or rebased to the verified remote base before edits.
- **Parallel work:** Subagents may work in declared disjoint paths or separate worktrees. One controller owns any shared index, commit, and push; overlapping writes are forbidden.
- **Review and validation:** After the first tested implementation, default to one independent review matched to the risk. Add reviewers only for named high-risk boundaries or actual findings. Run one baseline, focused gates while working, and the full native gates once before push; do not require serial policy audits before a known-safe code path or duplicate full-suite runs from every subagent.
- **Routine remediation:** Automatically fix safe mechanical findings within scope. Stop only for a genuine ownership collision, a destructive or out-of-scope action, an unresolved Critical/Important correctness issue, or a material user design decision.
- **GitHub access:** GitHub MCP remains preferred; when it is unavailable, use `gh` immediately rather than waiting for connector retries.
- **Handoff:** In the team-maintainer profile, commit, push, and PR handoff are expected outcomes of authorized implementation. Merge remains separately and explicitly authorized under the protocol above.

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
