# Review guide

This guide defines the review and pull-request lifecycle for `worktrees-hives`. It applies to human-authored and agent-authored changes.

## Lifecycle

```text
issue -> claimed job -> isolated worktree -> focused commits -> pull request
      -> companion-skill interactive monitoring -> merge-ready report -> human merge decision
      -> human merge or explicitly authorized one-shot primary-agent merge
```

The orchestrator, unattended runtime, installed companion `babysit-pr` skill, and worker agents may prepare or monitor a pull request and report that it is merge-ready, but they never merge it or claim that an automated merge will occur. The companion skill is guidance, not an enforcement boundary. A primary interactive agent may execute a human's explicit one-shot merge request only by completing the [protocol in `AGENTS.md`](AGENTS.md#human-authorized-one-shot-merge-protocol). Auto-merge and merge queues remain forbidden.

For stacked pull requests, review and fix the bottom PR before its children. Re-evaluate children after their base changes.

## Required checklist

### Scope and traceability

- [ ] The PR links its GitHub issue and, when present, the matching Linear `RM-*` issue.
- [ ] Changes satisfy the linked acceptance criteria without unrelated refactors.
- [ ] Session work is reflected accurately in Beads.
- [ ] Generated replies identify the agent and, after a fix, include the pushed commit SHA.
- [ ] Codex-authored commits contain exact `Agent: Codex` and `Co-authored-by: Codex <noreply@openai.com>` trailers without rewriting Cursor-attributed history.

### Safety

- [ ] No command, API call, prompt, or documentation path can merge a PR automatically, enqueue it, or schedule a deferred merge.
- [ ] Product runtime and worker interfaces expose no merge path; any interactive one-shot merge is outside the unattended runtime and satisfies the complete human-authorization protocol.
- [ ] Force pushing accepts only `--force-with-lease`; bare `--force` and `-f` are rejected.
- [ ] Mutating operations verify the expected job branch.
- [ ] Paths are derived under the configured worktree base and reject traversal or escape.
- [ ] Agents edit only the assigned branch and isolated worktree.
- [ ] Owner allowlist is configuration-only (env/API); no hard-coded personal/org defaults in product code or docs.
- [ ] Credentials, tokens, and sensitive subprocess data are absent from logs and reports.

### Behavior and compatibility

- [ ] JSON output follows the documented envelope and keeps stdout machine-readable.
- [ ] Rust `wh` v1 and Python CLI/watchlist v2 contracts are reviewed independently; each breaking change bumps its own schema version.
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
- `gh pr merge` and merge-oriented `gh api` requests are impossible through product runtime public interfaces.
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
- Command capability classification consumes wrapper-option operands (including platform-specific arity); rejects behavior-changing wrapper assignments except the exact Git lazy-fetch neutralizer, tracks nested `env` clears and unsets in execution order, rejects command-lookup overrides, ambiguous clusters, and unsupported wrapper escapes, and recognizes console, windowed, free-threaded, and alternative Python launchers plus test-package `.__main__` aliases. Unwrap failures, interactive Python module routes, unchecked Git runtime configuration, partial-clone lazy fetches, configured archive formatters, named, ambient, peeled, sorted, or ref-format signature rendering, alternate-ref options on any revision consumer, implicit or explicit Git helper/viewer/transport/signature/credential/difftool execution, and `lab run --command` require the conservative shell capability set, so nested code, test, or experiment requirements are not collapsed. Trusted Git built-in names are matched case-sensitively, and legacy `git config` read actions are parsed before their first positional operand so read-looking values cannot hide a write. A no-capability object-materializing Git inspection must run through an effective `env GIT_NO_LAZY_FETCH=1`, end global pagination with `-P` / `--no-pager`, and explicitly set `core.fsmonitor=false`; a no-capability `status` also requires exact global `--no-optional-locks`, preventing index refreshes from invoking `post-index-change`. Pretty-rendering history commands must also set `log.showSignature=false` and select a built-in or inline `format:` / `tformat:` pretty format. Patch-producing history flags include clustered short options, while explicitly non-querying and helper-disabled Git forms remain usable under that neutralized configuration. Mutating and otherwise unclassified Git commands preserve the full conservative capability set because filters, hooks, transports, and shell aliases can execute beneath them. Non-Git unclassified executables remain outside this mapping by design and are not a universal allowlist.
- Future #94/#95 execution wiring sanitizes inherited interpreter controls such as `PYTHONINSPECT`; the #93 classifier accepts structured argv only and cannot validate an environment override that is not represented there.
- Discovery applies the owner allowlist before scheduling work.
- Stacks are ordered bottom-up and children are deferred while their base is blocked.
- Python watchlist v1 inputs migrate on rewrite to v2 without `fix_count`, `max_fixes`, or `babysit_cycle`; unrelated additive job fields remain preserved.
- Reports retain residual blockers and distinguish pending, blocked, failed, and merge-ready states.
- P2–P4 placeholder modules remain explicit `NotImplementedError` stubs until their issues land.

Run:

```bash
cd python
python -m pip install -e '.[test]'
pytest
```

The required Qlty Cloud check is reproduced with `qlty check` from the repository root. Configuration lives in [`.qlty/qlty.toml`](.qlty/qlty.toml): `python/tests/**` is test code, Bandit B101/B108 are ignored only on test paths, and production Python Bandit analysis stays enabled.

### Agent-skill review notes

Skill text is a portable operator interface, not enforcement. Verify that it:

- Calls the Python orchestrator and `wh` instead of bypassing them with direct mutations.
- Does not promise unsupported host-platform capabilities.
- Preserves deny-by-default merge behavior, the bounded primary-agent authorization protocol, worktree isolation, allowlist, stack order, and attribution rules.
- Distinguishes implemented commands from planned commands.
- Uses bounded, concrete worker prompts and reports unresolved work rather than hiding it.

## Review replies

Post a fix reply only after its commit is pushed. Include the short or full SHA and attribution, for example:

```text
Grok Build agent: fixed the branch check in abc1234 and added the mismatch regression test.
```

Replies may explain why no code change is needed. Resolve a thread only when the concern is addressed or the reviewer has accepted the explanation.

## Human-authorized one-shot merge review

Before a primary interactive agent executes a requested merge, verify all of the following:

- [ ] The human's current message unambiguously identifies and affirmatively requests the exact PR, either directly or by reference to the single current PR; no standing permission or inferred intent is being reused.
- [ ] The expected repository, PR number, base, head SHA, and merge method were stated. The method is the human's choice, or squash when the request omitted a method.
- [ ] A fresh GitHub MCP preflight confirms the PR is open, non-draft, conflict-free, mergeable, still at that head SHA, and has all required checks terminal and successful without a protection bypass.
- [ ] The current review decision, every paginated review thread, and trusted-bot comments were inspected.
- [ ] Any unresolved findings were disclosed before the final human decision. Every explicitly deferred actionable finding has a linked open GitHub issue; duplicate, obsolete, or non-actionable findings have a documented disposition.
- [ ] No new blocker appeared and authorization did not expire because the target/head changed, the method became ambiguous, or the session ended.
- [ ] The operation is an immediate one-shot merge through GitHub MCP when available. Auto-merge, merge queues, scheduled merges, and admin bypasses are disabled.
- [ ] The post-mutation read confirms the merged state and captures the method and resulting merge commit SHA before the agent claims success.

## Review outcome

A successful interactive-monitoring report marks the PR as **merge-ready** when required CI is green, conflicts are absent, change requests are cleared, and review threads are resolved. That status is advisory and does not authorize a merge. A human reviewer decides whether and when to merge, then either merges personally or explicitly requests a one-shot primary-agent merge under the checklist above.
