# worktrees-hives

`worktrees-hives` is a multi-platform foundation for turning issues into pull requests and babysitting those pull requests with isolated subagents. It combines an agent skill, a Python policy orchestrator, and a Rust safety core.

> [!IMPORTANT]
> The project never auto-merges. It prepares pull requests for a human merge decision.

The repository is in its foundation phase. The Rust workspace is available; the Python package and complete agent skill are tracked separately.

## Architecture

worktrees-hives is a **Python/Rust hybrid**. Rust owns performance, memory discipline, and hard safety enforcement. Python owns orchestration policy, agent glue, and human-readable reporting. Agent skills (`SKILL.md`) describe when and how agents call the tooling.

```text
Agent platform / SKILL.md
          |
          | intent and operator context
          v
Python orchestrator (worktrees_hives)
          | wh subprocess calls + JSON envelope v1
          v
Rust CLI (wh) / wh-core
          | allowlisted subprocess operations
          v
git / gh / operating system
```

| Layer | Owns | Does not own |
| --- | --- | --- |
| Agent skill | Prompts and guidance for when and how an agent calls the tooling | Enforceable safety policy |
| Python `worktrees_hives` | Discovery, partitioning, issue-to-PR and babysit policy, stack ordering, fix budgets, and reports | Direct worktree or unsafe git mutation |
| Rust `wh-core` + `wh` | Worktrees, durable job state, process supervision, path sandboxing, branch verification, and hard git/GitHub safety stops | High-level agent policy |
| `git`, `gh`, OS | Version-control, GitHub, and process primitives invoked through Rust | Hive policy |

**Why a hybrid?** Rust enforces safety-sensitive mutation rules (never-merge, force-with-lease, branch verification, path sandboxing) at the binary boundary where a malformed prompt or Python bug cannot bypass them. Python handles the orchestration logic that benefits from rapid iteration and rich ecosystem tooling. The agent skill layer remains portable across platforms.

The Python/Rust boundary is CLI-first and uses versioned JSON instead of PyO3. The contract is versioned independently so Python and Rust can evolve without sharing an in-process ABI. The v1 contract is tracked in [GitHub #40](https://github.com/rmems/worktrees-hives/issues/40); its documentation will live at `docs/json-contract.md`.

See [`AGENTS.md`](AGENTS.md) for detailed source ownership, data flow, and per-layer responsibilities.

## Safety invariants

These rules apply to every agent, platform, and command path:

- **Never merge pull requests.** No `gh pr merge`, merge API, or equivalent automated path is allowed.
- Force pushes may use only `--force-with-lease`; bare `--force` and `-f` are forbidden.
- Each job edits only its assigned branch and isolated worktree.
- Mutating operations must verify the expected job branch and remain inside the configured path sandbox.
- A babysit cycle may create at most **three code-fix commits per PR**. Review replies are not capped.
- Stacked pull requests are handled from the bottom of the stack upward.
- Review replies are posted only after the fix is pushed and include the pushed SHA plus attribution, for example: `Grok Build agent: fixed in abc1234`.

Soft prompt text is not considered enforcement. Hard stops belong in Rust so a malformed prompt or Python bug cannot bypass them.

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

## Python package

The Python bridge is planned in [GitHub #30](https://github.com/rmems/worktrees-hives/issues/30). Once that package lands under `python/`, install it in editable mode with the `test` extra so the `pytest` gate can run:

```bash
python -m pip install -e './python[test]'
```

Python will invoke `wh` from `WH_BIN` or `PATH` and consume the versioned JSON contract. It will not duplicate Rust-owned state or mutation logic.

## Project documentation

- [`AGENTS.md`](AGENTS.md) — agent roles, boundaries, data flow, and worktree rules
- [`docs/workflows/safe-issue-verified-commit.md`](docs/workflows/safe-issue-verified-commit.md) — issue → verified push
- [`docs/workflows/safe-verified-commit-to-pr.md`](docs/workflows/safe-verified-commit-to-pr.md) — verified push → PR (never merge)
- [`REVIEW.md`](REVIEW.md) — pull-request lifecycle and review checklist
- [`docs/aggregate-report.md`](docs/aggregate-report.md) — aggregate discoveries report format (Markdown table + JSON)
- Hybrid foundation epic: [GitHub #21](https://github.com/rmems/worktrees-hives/issues/21)
- Rust core epic: [GitHub #22](https://github.com/rmems/worktrees-hives/issues/22)
- Python orchestration epic: [GitHub #23](https://github.com/rmems/worktrees-hives/issues/23)
- [Linear `worktrees-hives` project](https://linear.app/rpd-34/project/worktrees-hives-e3052de4caa3)

## License

Licensed under the [Apache License 2.0](LICENSE).
