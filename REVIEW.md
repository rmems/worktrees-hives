# Review guide

This guide defines the review and pull-request lifecycle for `worktrees-hives`. It applies to human-authored and agent-authored changes.

## Lifecycle

```text
issue -> claimed job -> isolated worktree -> focused commits -> pull request
      -> CI/review babysit cycles -> merge-ready report -> human merge decision
```

The orchestrator and its agents may prepare a pull request and report that it is merge-ready. They must never merge it or claim that an automated merge will occur.

For stacked pull requests, review and fix the bottom PR before its children. Re-evaluate children after their base changes.

## Required checklist

### Scope and traceability

- [ ] The PR links its GitHub issue and, when present, the matching Linear `RM-*` issue.
- [ ] Changes satisfy the linked acceptance criteria without unrelated refactors.
- [ ] Session work is reflected accurately in Beads.
- [ ] Generated replies identify the agent and, after a fix, include the pushed commit SHA.

### Safety

- [ ] No command, API call, prompt, or documentation path can merge a PR automatically.
- [ ] Force pushing accepts only `--force-with-lease`; bare `--force` and `-f` are rejected.
- [ ] Mutating operations verify the expected job branch.
- [ ] Paths are derived under the configured worktree base and reject traversal or escape.
- [ ] Agents edit only the assigned branch and isolated worktree.
- [ ] Python limits each PR to three code-fix commits per babysit cycle.
- [ ] Owner allowlist is configuration-only (env/API); no hard-coded personal/org defaults in product code or docs.
- [ ] Credentials, tokens, and sensitive subprocess data are absent from logs and reports.

### Behavior and compatibility

- [ ] JSON output follows the documented envelope and keeps stdout machine-readable.
- [ ] Compatible v1 changes are additive; breaking changes bump the schema version.
- [ ] Errors are actionable and policy rejections map to exit code 2.
- [ ] Cross-platform path and process behavior does not assume a Linux-only environment.
- [ ] New behavior has focused tests, including negative policy tests where relevant.
- [ ] Documentation and examples match the implemented command surface.
- [ ] Documentation command blocks preserve their caller's working directory when steps run sequentially.

## Language-specific review notes

worktrees-hives is a Python/Rust hybrid. Each layer has distinct review concerns:

### Rust review notes

Rust owns the hard safety boundary. Reviewers should check:

- Policy is enforced in `wh-core`, not only in `clap` argument definitions or Python.
- Git and GitHub operations use explicit allowlists and structured argument vectors rather than shell command strings.
- `gh pr merge` and merge-oriented `gh api` requests are impossible through public interfaces.
- Branch verification occurs immediately before mutation to reduce time-of-check/time-of-use risk.
- Canonicalization and component checks prevent `..`, symlink, or prefix-based path escape.
- State writes use a temporary file and atomic replacement without leaving partial JSON.
- Process timeouts terminate and reap children; this belongs to the later supervisor implementation.
- Public error codes are stable enough for Python to classify without parsing prose.
- Unsafe Rust remains forbidden unless a separately reviewed design justifies it.

Run:

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

### Python review notes

Python owns orchestration policy. Reviewers should check:

- `WhClient` treats `wh` as the source of truth and does not duplicate Rust mutations.
- `WH_BIN` and `PATH` lookup failures produce a clear `WhNotFoundError`.
- Exit code 2 and structured policy errors become `PolicyError`, preserving the Rust error code.
- Subprocess calls use argument arrays, bounded execution, captured output, and no shell interpolation.
- Command capability classification consumes wrapper-option operands; rejects wrapper environment assignments, ambiguous clusters, and executable read-only Git options (including accepted long-option abbreviations); recognizes console, windowed, and versioned Python launchers plus test-package `.__main__` aliases; and fails closed on unsupported wrapper escapes. Shell executors do not collapse nested test or experiment requirements.
- Discovery applies the owner allowlist before scheduling work.
- Stacks are ordered bottom-up and children are deferred while their base is blocked.
- The three-code-fix-commit budget is per PR per cycle; review replies do not consume it.
- Reports retain residual blockers and distinguish pending, blocked, failed, and merge-ready states.
- P2–P4 placeholder modules remain explicit `NotImplementedError` stubs until their issues land.

Run:

```bash
cd python
python -m pip install -e '.[test]'
pytest
```

### Agent-skill review notes

Skill text is a portable operator interface, not enforcement. Verify that it:

- Calls the Python orchestrator and `wh` instead of bypassing them with direct mutations.
- Does not promise unsupported host-platform capabilities.
- Preserves never-merge, worktree isolation, allowlist, stack order, and attribution rules.
- Distinguishes implemented commands from planned commands.
- Uses bounded, concrete worker prompts and reports unresolved work rather than hiding it.

## Review replies

Post a fix reply only after its commit is pushed. Include the short or full SHA and attribution, for example:

```text
Grok Build agent: fixed the branch check in abc1234 and added the mismatch regression test.
```

Replies may explain why no code change is needed and do not count against the code-fix budget. Resolve a thread only when the concern is addressed or the reviewer has accepted the explanation.

## Review outcome

A successful automated cycle reports the PR as **merge-ready** when required CI is green, conflicts are absent, change requests are cleared, and review threads are resolved. That status is advisory. A human reviewer decides whether and when to merge.
