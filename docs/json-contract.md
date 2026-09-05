# JSON Contract v1 and exact-base v2

This document describes the versioned JSON envelope used for communication between the Python orchestrator and the Rust `wh` CLI.

The Rust `wh` envelope remains independently versioned at schema version 1 for
bootstrap, status, state, safe Git/GitHub, supervisor, and non-create worktree
commands. The Python `worktrees-hives` CLI envelopes and persisted watchlist are
versioned separately at schema v2; see [Python watchlist and CLI schema v2](#python-watchlist-and-cli-schema-v2).

## Boundary versions

The outer `schema_version` versions the complete Python/Rust boundary: response
envelopes, command names, required and optional request arguments, and error/exit
semantics. It is not only a response-object version.

- Version 1 remains the default for bootstrap, status, state, safe Git/GitHub,
  supervisor, and non-create worktree commands.
- `worktree.create` version 1 is now a fail-closed, non-mutating migration stub.
  It returns `CONTRACT_UPGRADE_REQUIRED` instead of deriving a branch start from
  the controller checkout's ambient `HEAD`.
- `worktree.create` version 2 is selected explicitly with
  `--schema-version 2`. It requires `--start-point`, resolves that input to a
  canonical commit before mutation, and returns schema-version 2 success or
  failure envelopes.

The v1 migration stub has no scheduled removal. It remains supported until a
future, separately announced boundary version defines its removal, so legacy
machine callers retain a deterministic upgrade signal throughout the current
0.x series. It never regains mutating behavior.

## Envelope Structure

After command-line parsing succeeds, all `--json` responses follow this schema:

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
| `schema_version` | `integer` | Selected boundary version (`1`, or `2` for exact-base `worktree.create`) |
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
- `WhSchemaError` — JSON did not match a supported v1/v2 envelope
- `WORKTREE_RESUME_UNPROVEN` — An existing branch cannot be safely identified as this job
- `WORKTREE_POSTCONDITION_FAILED` — The created worktree/ref/HEAD identity changed or disagreed
- `WORKTREE_CREATE_FAILED` — Git creation failed and residual state is reported in the message
- `CONTRACT_UPGRADE_REQUIRED` — A v1 `worktree.create` request reached the non-mutating migration stub
- `START_POINT_REQUIRED` — A v2 `worktree.create` request omitted `--start-point`
- `CONTRACT_VERSION_UNSUPPORTED` — Python selected v2 but an older binary could not parse that selector

`--json` is normally a dispatched-command contract. Errors raised by the clap parser before
dispatch (for example, a missing required argument, an unknown option, `--help`, or
`--version`) remain clap's human-readable stderr output and exit code; they are not v1
JSON envelopes. The `worktree.create` parser deliberately accepts a missing
`--start-point` so both the legacy v1 request and the malformed v2 request reach
dispatch and receive machine-readable errors without mutation. Once
`worktree.create` dispatches, policy failures use exit code 2 and
operational failures (including an invalid/unresolvable start point) use exit code 1,
with an `ok:false` envelope on stdout.

If Git may have left a branch or worktree registration behind, the failure
envelope's additive `data` fields report `path`, `branch`, `path_exists`,
`branch_commit`, `head_commit`, `worktree_registered`, and
`cleanup_performed:false`. The operation deliberately does not delete residual
state whose ownership could have been concurrently adopted; callers must stop
and surface it for explicit reconciliation.

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

Create a new isolated worktree for a job using the exact-base v2 boundary.

```bash
wh --json worktree create --schema-version 2 --repo /path/to/repo --start-point origin/trunk acme example-repo wh-123 feature/fix
```

**Request parameters:** required `--schema-version 2`, `--repo`, and
`--start-point <commit-or-ref>` flags plus
positionals `<owner> <repo_name> <job_id> <branch>`. The start point is resolved to a
commit before any mutation. Existing branches are rejected unless a future contract can
prove a durable resume identity.

The legacy v1 shape remains parseable solely for migration:

```bash
wh --json worktree create --repo /path/to/repo acme example-repo wh-123 feature/fix
```

It exits 1 without creating a path or branch and emits:

```json
{
  "ok": false,
  "schema_version": 1,
  "command": "worktree.create",
  "data": { "required_schema_version": 2 },
  "error": {
    "code": "CONTRACT_UPGRADE_REQUIRED",
    "message": "worktree.create schema v1 is a non-mutating migration stub; retry with --schema-version 2 and an explicit --start-point"
  }
}
```

**Success response:**
```json
{
  "ok": true,
  "schema_version": 2,
  "command": "worktree.create",
  "data": {
    "path": "/home/user/.local/share/worktrees-hives/worktrees/acme/example-repo/wh-123",
    "branch": "feature/fix",
    "branch_ref": "refs/heads/feature/fix",
    "repo_root": "/path/to/repo",
    "start_commit": "0123456789abcdef0123456789abcdef01234567",
    "head_commit": "0123456789abcdef0123456789abcdef01234567",
    "worktree_registered": true
  },
  "error": null
}
```

`start_commit` is the canonical commit to which the caller's start point resolved.
`head_commit` is independently read from the completed worker and must equal
`start_commit`. `branch_ref` is the verified full symbolic ref, and
`worktree_registered:true` proves `git worktree list --porcelain` reported the same
canonical path, branch ref, and HEAD. Python orchestration consumers require and
re-verify all of these fields against their request. An all-hex caller start point must
exactly equal the canonical resolved object id: 40 characters in a SHA-1 repository or
64 characters in a SHA-256 repository. Uppercase full ids are accepted and
canonicalized to lowercase. A 40-character prefix in a SHA-256 repository is rejected
as abbreviated even when Git can resolve it uniquely; symbolic refs are resolved by
Rust.

### Mixed-version behavior

- A legacy v1 client calling a v2-capable binary receives the schema-v1
  `CONTRACT_UPGRADE_REQUIRED` envelope above. No branch or worktree is created.
- A v2-capable Python client always includes `--schema-version 2` and
  `--start-point`, accepts schema 1 and 2 envelopes, and requires the complete
  verified v2 success identity.
- If that client invokes a legacy v1 binary, the old binary rejects the unknown
  selector before dispatch. `WhClient` converts that otherwise human-only
  failure into `WhContractVersionError` with stable code
  `CONTRACT_VERSION_UNSUPPORTED` and `requested_schema_version=2`.
- A v2 request missing `--start-point` receives a schema-v2
  `START_POINT_REQUIRED` error with exit 1 and no mutation. An invalid or
  unresolvable start point receives the existing schema-v2 operational error.

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
- **Breaking changes** include removing or renaming fields, changing types or
  meanings, removing commands, or adding a required request argument. They
  require a new selected boundary version.
- Consumers MUST ignore unknown fields in `data`.
- Consumers MUST handle `error` being `null` or an object.
- Exact-base `worktree.create` v2 fields are required by v2-capable Python
  consumers even though a generic envelope parser continues to allow additive
  data.

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
- `worktree-create-v1-upgrade-required.json`
- `worktree-list.json`
- `state-show.json` — Python `watchlist.list` schema v2 envelope (no retired babysit fields)
- `state-add.json` — Python `watchlist.add` schema v2 envelope (no retired babysit fields)
- `git-run.json`
- `error-policy.json`
- `research-contract-cloud-agent.json`
- `research-roles-v0.json`

## Python Validation

The Python `worktrees_hives.contract` module provides `Response.from_dict()` for validation. It accepts supported v1/v2 envelopes and raises `WhSchemaError` for malformed or unsupported versions.

```python
from worktrees_hives.contract import Response, classify
from worktrees_hives.errors import WhSchemaError

raw = json.loads(stdout)
response = Response.from_dict(raw)  # Validates supported boundary schemas
typed = classify(response)  # SuccessResponse | ErrorResponse
```

## Rust Validation

The Rust `wh_core::contract` module provides the same validation via `Response::from_dict()`.
