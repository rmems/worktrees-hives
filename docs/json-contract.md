# JSON Contract v1

This document describes the versioned JSON envelope used for communication between the Python orchestrator and the Rust `wh` CLI.

The Rust `wh` envelope remains independently versioned at schema version 1. The Python `worktrees-hives` CLI envelopes and persisted watchlist are versioned separately at schema v2; see [Python watchlist and CLI schema v2](#python-watchlist-and-cli-schema-v2).

## Envelope Structure

All `--json` responses follow this schema:

```json
{
  "ok": true,
  "schema_version": 1,
  "command": "command.name",
  "data": {},
  "error": null
}
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `ok` | `boolean` | `true` for success, `false` for failure |
| `schema_version` | `integer` | Always `1` for this version |
| `command` | `string` | Machine-readable command identifier (e.g., `worktree.create`, `state.add`) |
| `data` | `object` | Command-specific payload (empty object `{}` on success for commands without output) |
| `error` | `object \| null` | Present only when `ok: false` |

### Error Object

```json
{
  "code": "ERROR_CODE",
  "message": "Human-readable description"
}
```

Standard error codes:
- `PolicyMergeForbidden` — Attempted to merge a PR (never allowed)
- `PolicyForcePushForbidden` — Bare `--force`/`-f` used (only `--force-with-lease` allowed)
- `PolicyBranchMismatch` — Current branch doesn't match expected job branch
- `PolicyPathEscape` — Path traversal outside sandbox
- `PolicyGitSubcommandNotAllowed` — Git subcommand not in allowlist
- `PolicyGhSubcommandNotAllowed` — GH subcommand not in allowlist
- `PolicyGhApiDenied` — `gh api` denied by default in v1
- `WhBinaryNotFoundError` — `wh` binary not on PATH or `WH_BIN`
- `WhProcessError` — `wh` exited with non-zero status
- `WhJsonDecodeError` — Stdout was not valid JSON
- `WhSchemaError` — JSON did not match v1 envelope

## Commands

### Bootstrap

```bash
wh --json
```

**Response:**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "cli.bootstrap",
  "data": {},
  "error": null
}
```

### Worktree Operations

#### `worktree.create`

Create a new isolated worktree for a job.

```bash
wh --json worktree create --repo /path/to/repo acme example-repo wh-123 feature/fix
```

**Request parameters:** `--repo` flag plus positionals `<owner> <repo_name> <job_id> <branch>`.

**Success response:**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "worktree.create",
  "data": {
    "path": "/home/user/.local/share/worktrees-hives/worktrees/acme/example-repo/wh-123",
    "branch": "feature/fix",
    "repo_root": "/path/to/repo"
  },
  "error": null
}
```

#### `worktree.list`

List all hive worktrees.

```bash
wh --json worktree list
```

**Success response:**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "worktree.list",
  "data": {
    "worktrees": [
      { "path": "/.../wh-123", "branch": "feature/fix" },
      { "path": "/.../wh-124", "branch": "feature/other" }
    ]
  },
  "error": null
}
```

#### `worktree.remove`

Remove a worktree.

```bash
wh --json worktree remove /path/to/worktree --force
```

**Success response:**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "worktree.remove",
  "data": { "removed": "/path/to/worktree" },
  "error": null
}
```

#### `worktree.prune`

Prune stale worktree administrative files.

```bash
wh --json worktree prune --repo /path/to/repo
```

**Success response:**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "worktree.prune",
  "data": {},
  "error": null
}
```

### State Operations

#### `state.show`

Show watched jobs.

```bash
# Show all
wh --json state show

# Show specific job
wh --json state show acme example-repo 42
```

**Success response (all):**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "state.show",
  "data": {
    "jobs": [
      {
        "owner": "acme",
        "repo": "example-repo",
        "number": 42,
        "kind": "issue",
        "branch": "issue-42-fix",
        "worktree_path": "/home/user/.local/share/worktrees-hives/worktrees/acme/example-repo/wh-42",
        "stack_id": "stack-1",
        "status": "pending",
        "residual_blockers": [],
        "created_at": 1700000000,
        "updated_at": 1700000100
      }
    ]
  },
  "error": null
}
```

#### `state.add`

Add a new watched job.

```bash
wh --json state add acme example-repo 42 --kind issue --branch issue-42-fix --worktree /path/to/wt --stack stack-1
```

**Success response:**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "state.add",
  "data": {
    "owner": "acme",
    "repo": "example-repo",
    "number": 42,
    "kind": "issue",
    "branch": "issue-42-fix",
    "worktree_path": "/path/to/wt",
    "stack_id": "stack-1",
    "status": "claimed",
    "residual_blockers": [],
    "created_at": 1700000000,
    "updated_at": 1700000000
  },
  "error": null
}
```

#### `state.remove`

Remove a watched job.

```bash
wh --json state remove acme example-repo 42
```

**Success response:**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "state.remove",
  "data": { "removed": true },
  "error": null
}
```

### Git Operations

#### `git.run`

Run an allowlisted git command.

```bash
wh --json git run status --porcelain
```

**Success response:**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "git.run",
  "data": {
    "success": true,
    "stdout": "M  src/main.rs\n",
    "stderr": ""
  },
  "error": null
}
```

#### `git.verify-branch`

Verify the current branch matches the expected branch.

```bash
wh --json git verify-branch feature/fix --repo /path/to/repo
```

**Success response:**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "git.verify-branch",
  "data": { "matched": true },
  "error": null
}
```

### GH Operations

#### `gh.run`

Run an allowlisted gh command.

```bash
wh --json gh run pr view 42 --json number,title,state
```

#### `gh.pr-view`

View a PR with specific fields.

```bash
wh --json gh pr-view 42 --fields number,title,state --repo /path/to/repo
```

**Success response:**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "gh.pr-view",
  "data": {
    "success": true,
    "stdout": "{\"number\":42,\"title\":\"Fix bug\",\"state\":\"OPEN\"}",
    "stderr": ""
  },
  "error": null
}
```

## Research contract domain document

Research Hive experiment contracts are versioned Python domain documents. They
reuse this envelope rather than defining another transport protocol. A lab
command that carries a research contract places it at
`data.research_contract`. The following abbreviated shape illustrates nesting
only; `<command>` is a placeholder, and the shortened contract is not a valid
standalone fixture:

```json
{
  "ok": true,
  "schema_version": 1,
  "command": "lab.<command>",
  "data": {
    "research_contract": {
      "schema_version": 1,
      "research_id": "structured-contract-first-pass-evidence-v1",
      "question": "Does a frozen contract improve first-pass evidence quality?",
      "hypothesis": "The contract improves valid evidence within the token-cost bound."
    }
  },
  "error": null
}
```

The outer `schema_version` belongs to the Python/Rust transport envelope. The
nested `data.research_contract.schema_version` belongs only to the research
contract. The two versions evolve independently. GitHub #92 defines the nested
document and does not add or change a CLI command.

The canonical research-contract format is JSON. Within research-contract v1,
optional fields (`null_hypothesis`, `resource_budget`, and `split_policy`) must
be omitted when absent; an explicit JSON `null` is rejected as an invalid type.
This differs from envelope fields such as `error`, which may use `null`. Unknown
top-level fields are treated as future additive extensions: the Python parser
validates that their values are finite, JSON-compatible data, freezes them with
the rest of the contract, and re-emits them on serialization. It does not
silently discard them. Removing or renaming a known field, changing a known
field's type or meaning, or otherwise making a breaking domain change requires
a research-contract version bump.

`ResearchOutcome` reserves four first-class conclusions for later result
documents: `supported`, `not_supported`, `inconclusive`, and `invalid`. An
outcome is intentionally absent from the pre-execution `ResearchContract` so a
result cannot be recorded as though it were known when acceptance criteria were
frozen.

The complete requiredness matrix and explicit non-goals are recorded in
[`2026-08-12-research-contract-design.md`](superpowers/specs/2026-08-12-research-contract-design.md).
The complete, validating
[`research-contract-cloud-agent.json`](examples/research-contract-cloud-agent.json)
fixture models a cloud coding-agent experiment; it does not model local GPU
conditions.

## Research role domain document

Research Hive occupant roles are versioned Python domain documents. They
reuse this envelope rather than defining another transport protocol. A lab
command that carries a role contract places it at `data.research_role`.
Runtime binding of that contract to a model, provider, and agent identity
places provenance at `data.role_binding`. The following abbreviated shape
illustrates nesting only; `<command>` is a placeholder, and the shortened
objects are not a valid standalone fixture:

```json
{
  "ok": true,
  "schema_version": 1,
  "command": "lab.<command>",
  "data": {
    "research_role": {
      "schema_version": 1,
      "role_id": "verification_agent",
      "capabilities": {
        "read_repository": true,
        "read_results": true,
        "execute_tests": true,
        "modify_code": false,
        "launch_experiments": false
      }
    },
    "role_binding": {
      "schema_version": 1,
      "role_id": "verification_agent",
      "model_id": "grok-4.6",
      "provider": "xai",
      "agent_id": "verifier-1"
    }
  },
  "error": null
}
```

The outer `schema_version` belongs to the Python/Rust transport envelope. The
nested `data.research_role.schema_version` belongs only to the research role
document. The two versions evolve independently. GitHub #93 defines the nested
document and binding metadata and does not add or change a CLI command.

A role declares five capabilities: `read_repository`, `read_results`,
`execute_tests`, `modify_code`, and `launch_experiments`. Omitted capability
keys are treated as `false`. The built-in v0 catalog defines four role ids:
`research_coordinator`, `experiment_agent`, `verification_agent`, and
`artifact_agent`.

Capability enforcement is a Python policy gate, not a CLI command. Future
research orchestration (GitHub #94 and #95) must call
`assert_role_command_allowed` before granting a role a command. That function
always calls `assert_command_allowed` first. Rust remains authoritative for
actual `git` and `gh` execution.

The complete role field matrix and explicit non-goals are recorded in
[`2026-08-18-research-roles-design.md`](superpowers/specs/2026-08-18-research-roles-design.md).
The complete, validating
[`research-roles-v0.json`](examples/research-roles-v0.json)
fixture models the four built-in v0 roles.

## Compatibility Policy

- **Additive changes** (new optional fields in `data`, new commands) are compatible within v1.
- **Breaking changes** (removing/renaming fields, changing types, removing commands) require a schema version bump to v2.
- Consumers MUST ignore unknown fields in `data`.
- Consumers MUST handle `error` being `null` or an object.

This policy applies to the Rust `wh` envelope. It does not version the Python watchlist file or the Python `worktrees-hives` CLI envelopes; those use the separate v2 rules below.

## Python watchlist and CLI schema v2

The Python persisted watchlist (`watchlist.json` / `WH_WATCHLIST_PATH`) and the Python `worktrees-hives --json` envelopes are schema v2. This bump is independent of the Rust `wh` v1 envelope above. Rust `wh status` / `wh jobs` remain documented in [`status-schema.md`](status-schema.md).

### Persisted watchlist

- A missing `schema_version` is treated as `1`.
- Reads accept integer `schema_version` `1` or `2` only (not booleans, numeric strings, or floats).
- Legacy v1 files load successfully. The next mutation that writes the file rewrites it as v2 (read-only paths and `check` with zero matching jobs do not rewrite).
- On rewrite, retired babysit-only fields `fix_count`, `max_fixes`, and `babysit_cycle` are omitted.
- Unrelated unknown job fields and unknown top-level keys are preserved.

Legacy v1 input (accepted on read):

```json
{
  "schema_version": 1,
  "jobs": {
    "wh-42": {
      "job_id": "wh-42",
      "owner": "acme",
      "repo": "example-repo",
      "branch": "issue-42-fix",
      "status": "pending",
      "fix_count": 2,
      "max_fixes": 3,
      "babysit_cycle": "before-removal",
      "kind": "issue"
    }
  }
}
```

After the next mutation, the same record is persisted as v2. The retired fields are gone; `kind` remains:

```json
{
  "schema_version": 2,
  "jobs": {
    "wh-42": {
      "job_id": "wh-42",
      "owner": "acme",
      "repo": "example-repo",
      "branch": "issue-42-fix",
      "status": "pending",
      "stack_id": null,
      "residual_blockers": [],
      "pr_number": null,
      "pr_url": null,
      "last_check": null,
      "error": null,
      "kind": "issue"
    }
  }
}
```

### Python CLI envelopes

`worktrees-hives --json` uses `schema_version: 2`. Watchlist commands emit `watchlist.list`, `watchlist.add`, `watchlist.remove`, and `watchlist.check`. Those envelopes omit `fix_count`, `max_fixes`, and `babysit_cycle`. See [`state-show.json`](examples/state-show.json) and [`state-add.json`](examples/state-add.json).

## Fixtures

Example JSON files for testing are located in `docs/examples/`:

- `bootstrap.json`
- `worktree-create.json`
- `worktree-list.json`
- `state-show.json` — Python `watchlist.list` schema v2 envelope (no retired babysit fields)
- `state-add.json` — Python `watchlist.add` schema v2 envelope (no retired babysit fields)
- `git-run.json`
- `error-policy.json`
- `research-contract-cloud-agent.json`
- `research-roles-v0.json`

## Python Validation

The Python `worktrees_hives.contract` module provides `Response.from_dict()` for validation. It raises `WhSchemaError` if the envelope doesn't match v1.

```python
from worktrees_hives.contract import Response, classify
from worktrees_hives.errors import WhSchemaError

raw = json.loads(stdout)
response = Response.from_dict(raw)  # Validates schema
typed = classify(response)  # SuccessResponse | ErrorResponse
```

## Rust Validation

The Rust `wh_core::contract` module provides the same validation via `Response::from_dict()`.
