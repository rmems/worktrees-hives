# worktrees-hives → `writ`

A Rust safety core for coding-agent fleets: exact-base worktree verification, `git`/`gh` mutation allowlists, path sandboxing, process containment, and **no runtime merge path at all**.

> [!IMPORTANT]
> **This repository is mid-pivot.** It is becoming **`writ`** — the enforcement and admission-control layer for agent fleets. See [#1](https://github.com/rmems/worktrees-hives/issues/1) for the product epic and [#124](https://github.com/rmems/worktrees-hives/issues/124) for the current phase. The crate is still named `wh`; the rename is tracked under milestone M2.

## What this is for

Coordination is commoditized. Claude Code agent teams, `/batch`, Cursor `/multitask`, and Codex all already decompose work and hand each agent an isolated worktree. What none of them enforce is **safe concurrent writing**.

Measured on 33,596 agent pull requests across 2,807 repositories ([arXiv:2607.04697](https://arxiv.org/abs/2607.04697)):

- **41.7%** cross-agent textual conflict rate, versus 19.8% intra-agent (non-overlapping confidence intervals)
- **79.4%** of agent PRs were open concurrently with another agent's
- **84.4%** of conflicts were in source code, and largely *structural* — agents disagreeing about whether a file should exist at all

Claude Code agent teams have real coordination and [documented zero isolation](https://code.claude.com/docs/en/agent-teams): "two teammates editing the same file leads to overwrites." `/batch` and Cursor have real isolation and no coordination. The two never co-occur, and nothing in either column enforces safe integration.

`writ` fills that gap. It does not assign work and does not create worktrees. It **admits writes**.

## Architecture

Two layers, one binary.

| Layer | Owns | Does not own |
| --- | --- | --- |
| **Enforcement** (per-repo) | Exact base, branch/path identity, path sandbox, git/gh allowlists, no merge path, force-with-lease only, process containment | Which agent does what |
| **Coordination state** (cross-repo) | Agents, leases with path scopes, ownership, blockers, freeze modes. SQLite, single file, derived from `git`/`gh`/disk | Task decomposition or scheduling |
| `git`, `gh`, OS | Version-control, GitHub, and process primitives, invoked through allowlists | Policy |

Leases are the join: coordination state that the enforcement layer checks at write time.

### Why hooks

Enforcement runs as [Claude Code hooks](https://code.claude.com/docs/en/hooks), which is what makes it unbypassable rather than advisory:

- **`PreToolUse`** — "Exit 2 means a blocking error… exit 2 blocks whether or not you print JSON: even a JSON `permissionDecision` of `allow` can't override it."
- **`WorktreeCreate`** — "Any non-zero exit code aborts worktree creation." This is the lease-admission seam.
- **`WorktreeRemove`**, **`SubagentStart`/`SubagentStop`** — lease release and agent registry.

This inverts the usual failure mode. Safety is normally opt-in: a tool must be *called* to help. As a hook, it applies whether or not the agent cooperates.

## Safety invariants

These apply to every agent, platform, and command path:

- **Never merge autonomously.** The runtime exposes no merge path. A primary interactive agent may perform one immediate merge only after a human unambiguously identifies and requests that exact PR, under the [authorization protocol](AGENTS.md#human-authorized-one-shot-merge-protocol).
- Auto-merge, merge queues, scheduled merges, and admin bypasses are always forbidden.
- Force pushes may use only `--force-with-lease`; bare `--force` and `-f` are forbidden.
- Each job edits only its assigned branch and isolated worktree.
- Mutating operations verify the expected branch and stay inside the configured path sandbox.
- Stacked pull requests are handled from the bottom of the stack upward.

Soft prompt text is not runtime enforcement. Hard stops live in Rust, at the binary boundary, where a malformed prompt cannot bypass them.

## Owner allowlist

Repository access is controlled by a **configured owner allowlist**, not a built-in org list.

- Set `WH_ALLOWED_OWNERS=acme,example-org` (comma-separated), or pass explicit owners at the API boundary.
- Empty configuration means multi-owner discovery does nothing until an operator configures scope.

Examples use generic owners such as `acme` and `example-org`.

## Build

Prerequisites: stable Rust from [rustup](https://rustup.rs/), Git, and the GitHub CLI. The workspace MSRV is Rust **1.97.1** (pinned in `rust-toolchain.toml`).

```bash
cargo build --workspace
cargo test --workspace
cargo install --path crates/wh
wh --help
```

Contributor quality gates — these are canonical, and external analyzers are advisory until reproduced:

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

## Project documentation

- [`AGENTS.md`](AGENTS.md) — the authoritative contribution, autonomy, and safety contract
- [`SKILL.md`](SKILL.md) — portable agent procedure (guidance, not a security boundary)
- [`REVIEW.md`](REVIEW.md) — pull-request lifecycle and review checklist
- [`docs/workflows/safe-issue-verified-commit.md`](docs/workflows/safe-issue-verified-commit.md) — issue → verified push
- [`docs/workflows/safe-verified-commit-to-pr.md`](docs/workflows/safe-verified-commit-to-pr.md) — verified push → PR handoff (never merges)
- Product epic: [#1](https://github.com/rmems/worktrees-hives/issues/1) · Current phase: [#124](https://github.com/rmems/worktrees-hives/issues/124)
- Threat model: [#22](https://github.com/rmems/worktrees-hives/issues/22) · Boundary contract tests: [#81](https://github.com/rmems/worktrees-hives/issues/81)
- [Linear `worktrees-hives` project](https://linear.app/rpd-34/project/worktrees-hives-e3052de4caa3)

## License

Licensed under the [Apache License 2.0](LICENSE).
