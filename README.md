# worktrees-hives

`worktrees-hives` is a multi-platform foundation for turning issues into reviewable pull-request handoffs with isolated subagents. It combines repository guidance, a Python orchestration layer, and a Rust safety core. The installed companion `babysit-pr` skill owns interactive pull-request monitoring.

> [!IMPORTANT]
> The project never auto-merges. Its runtime and workers prepare pull requests for a human merge decision; a primary interactive agent may execute only an explicitly requested one-shot merge under the [`AGENTS.md` protocol](AGENTS.md#human-authorized-one-shot-merge-protocol).

The repository is in its foundation phase. The Rust workspace is available; the Python package and complete agent skill are tracked separately.

## Architecture

worktrees-hives is a **Python/Rust hybrid**. Rust owns performance, memory discipline, and hard safety enforcement. Python owns orchestration policy, agent glue, and human-readable reporting. Agent skills (`SKILL.md`) describe when and how agents call the tooling.

```text
Agent platform / SKILL.md
          |
          | intent and operator context
          v
Python orchestrator (worktrees_hives)
          | wh subprocess calls + versioned JSON envelope
          v
Rust CLI (wh) / wh-core
          | allowlisted subprocess operations
          v
git / gh / operating system
```

| Layer | Owns | Does not own |
| --- | --- | --- |
| Agent skill | Portable prompts and repository guidance for when and how an agent calls the tooling; the installed companion `babysit-pr` skill owns interactive PR monitoring | Enforceable safety policy |
| Python `worktrees_hives` | Discovery, partitioning, issue-to-PR orchestration, local watchlist state, stack ordering, and reports | Direct worktree or unsafe git mutation |
| Rust `wh-core` + `wh` | Worktrees, durable job state, process supervision, path sandboxing, branch verification, and hard git/GitHub safety stops | High-level agent policy |
| Interactive host connector | One-shot merge after a current human request and live preflight | Worker, unattended, queued, or inferred merges |
| `git`, `gh`, OS | Version-control, GitHub, and process primitives invoked through Rust | Hive policy |

**Why a hybrid?** Rust enforces safety-sensitive runtime mutation rules (no runtime merge path, force-with-lease, branch verification, path sandboxing) at the binary boundary where a malformed prompt or Python bug cannot bypass them. Python handles the orchestration logic that benefits from rapid iteration and rich ecosystem tooling. The agent skill layer remains portable across platforms.

The Python/Rust boundary is CLI-first and uses versioned JSON instead of PyO3. The contract versions request grammar as well as response envelopes so Python and Rust can evolve without sharing an in-process ABI. Most commands remain on v1; exact-base `worktree.create` selects v2 explicitly. See [`docs/json-contract.md`](docs/json-contract.md).

See [`AGENTS.md`](AGENTS.md) for detailed source ownership, data flow, and per-layer responsibilities.

## Safety invariants

These rules apply to every agent, platform, and command path:

- **Never merge autonomously.** The runtime, orchestrators, interactive monitoring flows, scheduled jobs, and worker agents expose no merge path.
- A primary interactive agent may perform one immediate merge only after the human unambiguously identifies and affirmatively requests that exact PR and the agent completes the fresh, SHA-sensitive [authorization and review protocol](AGENTS.md#human-authorized-one-shot-merge-protocol).
- Auto-merge, merge queues, scheduled merges, and admin bypasses are always forbidden.
- Force pushes may use only `--force-with-lease`; bare `--force` and `-f` are forbidden.
- Each job edits only its assigned branch and isolated worktree.
- Mutating operations must verify the expected job branch and remain inside the configured path sandbox.
- **Interactive PR monitoring is companion-skill guidance, not an enforcement boundary.** The installed `babysit-pr` skill handles that monitoring. Rust `wh-core` remains the hard code-enforced boundary for worktree, branch, path, process, push, runtime no-merge, auto-merge, and merge-queue controls.
- Stacked pull requests are handled from the bottom of the stack upward.
- Review replies are posted only after the fix is pushed and include the pushed SHA plus attribution, for example: `Grok Build agent: fixed in abc1234`.

Soft prompt text is not considered runtime enforcement. Runtime hard stops belong in Rust so a malformed prompt or Python bug cannot bypass them; the narrowly authorized interactive merge uses the host's GitHub connector outside the unattended product runtime.

## Owner allowlist

Repository access is controlled by a **configured owner allowlist**, not a built-in org list.

- Set `WH_ALLOWED_OWNERS=acme,example-org` (comma-separated), and/or
- Pass explicit `owners=` / `allowed_owners=` in Python APIs.

Empty configuration means multi-owner discovery and scheduling do nothing until operators configure scope.
Examples use generic owners such as `acme` and `example-org`.


## Build and install `wh`

Prerequisites:

- Stable Rust from [rustup](https://rustup.rs/)
- Git
- GitHub CLI for future GitHub operations

The workspace MSRV is Rust **1.97.1** (`rust-toolchain.toml` pins that channel). The Python package requires **Python ≥ 3.14.7** (CI uses 3.14.7).

```bash
cargo build --workspace
cargo test --workspace
cargo install --path crates/wh
wh --help
```

Contributor quality gates:

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

The Qlty Cloud PR check is reproduced locally with `qlty check` (see [`.qlty/qlty.toml`](.qlty/qlty.toml)).

## Python package

The Python bridge is planned in [GitHub #30](https://github.com/rmems/worktrees-hives/issues/30). Once that package lands under `python/`, install it in editable mode with the `test` extra so the `pytest` gate can run:

```bash
python -m pip install -e './python[test]'
```

Python will invoke `wh` from `WH_BIN` or `PATH` and consume the versioned JSON contract. It will not duplicate Rust-owned state or mutation logic.

### Python watchlist and CLI migration

The Python `worktrees-hives` CLI JSON envelope and persisted watchlist use schema version 2. When a legacy v1 watchlist is rewritten, it is migrated to v2: retired `fix_count`, `max_fixes`, and `babysit_cycle` fields are omitted while unrelated additive job fields are preserved.

This Python migration does **not** change the Rust `wh` CLI contract. Rust `wh` continues to use its independently versioned v1 JSON envelope and state examples.

## Project documentation

- [`AGENTS.md`](AGENTS.md) — agent roles, boundaries, data flow, and worktree rules
- [`docs/workflows/safe-issue-verified-commit.md`](docs/workflows/safe-issue-verified-commit.md) — issue → verified push
- [`docs/workflows/safe-verified-commit-to-pr.md`](docs/workflows/safe-verified-commit-to-pr.md) — verified push → PR handoff (that workflow never merges)
- [`REVIEW.md`](REVIEW.md) — pull-request lifecycle and review checklist
- [`docs/aggregate-report.md`](docs/aggregate-report.md) — aggregate discoveries report format (Markdown table + JSON)
- Hybrid foundation epic: [GitHub #21](https://github.com/rmems/worktrees-hives/issues/21)
- Rust core epic: [GitHub #22](https://github.com/rmems/worktrees-hives/issues/22)
- Python orchestration epic: [GitHub #23](https://github.com/rmems/worktrees-hives/issues/23)
- [Linear `worktrees-hives` project](https://linear.app/rpd-34/project/worktrees-hives-e3052de4caa3)

## License

Licensed under the [Apache License 2.0](LICENSE).
