# Research Hive v0 declarative roles + capability policy

## Scope

GitHub #93 adds a machine-readable Research Hive role contract. A role
declares what an occupant may read, execute, modify, and emit. Model and
provider identity is runtime configuration bound to a role, not a class of
role.

This is a Python domain document plus a fail-closed policy gate. It does not
add orchestration (#94), independent-verification workflow (#95), artifact
bundles (#96), a CLI command, or Rust code.

## Architecture

Add `worktrees_hives.research_roles` following the research-contract pattern
from #92:

- a version constant checked by exact equality;
- a five-value `ResearchCapability` string enum;
- a frozen `ResearchRoleCapabilities` record of explicit booleans;
- a frozen, slotted `ResearchRole` dataclass;
- a frozen `RoleBinding` that pairs one role with one model/provider/agent;
- JSON parsing and serialization helpers;
- `ResearchRoleValidationError` for schema failures;
- `RoleCapabilityError` (`PolicyError` subclass, code
  `ROLE_CAPABILITY_DENIED`) for enforcement failures.

`findings.AgentRole` (`agent` | `subagent`) is a lab-job occupancy class and
must not be reused. Research roles are capability contracts.

JSON is the only canonical input and output format. No dependency is added.

## Role fields and validation

| Field | Presence | Validation |
| --- | --- | --- |
| `schema_version` | Required | Integer (not Boolean), exactly version 1 |
| `role_id` | Required | Non-empty string. Not limited to the four v0 ids so later roles plug in. |
| `capabilities` | Required | Object. Each known key is a boolean. **Omitted keys are `false`** (fail closed). Unknown capability keys are rejected. |
| `inputs` | Required; may be empty | Array of non-empty strings |
| `outputs` | Required; may be empty | Array of non-empty strings |
| `constraints` | Required; object may be empty | JSON object. If `must_be_independent_of` is present it must be an array of non-empty strings. Other constraint keys are frozen and re-emitted. |

`role_id` is the canonical JSON key (the issue YAML used `id` only as a sketch).

Known capabilities, exactly these five:

- `read_repository`
- `read_results`
- `execute_tests`
- `modify_code`
- `launch_experiments`

All modeled arrays become tuples. Constraint objects become read-only
mappings. Unknown top-level role fields are accepted, recursively frozen, and
re-emitted (same additive-v1 rule as the research contract).

## Four v0 built-in roles

`V0_RESEARCH_ROLES` is an immutable catalog. Callers look up by `role_id`.

| role_id | read_repository | read_results | execute_tests | modify_code | launch_experiments | must_be_independent_of |
| --- | --- | --- | --- | --- | --- | --- |
| `research_coordinator` | true | true | false | false | false | (none) |
| `experiment_agent` | true | true | true | true | true | (none) |
| `verification_agent` | true | true | true | false | false | `experiment_author` |
| `artifact_agent` | true | true | false | false | false | (none) |

Catalog entries also declare inputs/outputs used by later orchestration. They
are documentary for #93; #94 consumes them.

## Binding and provenance

```text
RoleBinding(role, model_id, provider, agent_id)
```

The same `ResearchRole` value can be bound to two different
`(provider, model_id)` pairs. Binding does not copy or mutate the role.

`binding_metadata(binding)` returns a JSON-ready object for run metadata:

- `role_id`
- `model_id`
- `provider`
- `agent_id`
- `capabilities` (all five keys, explicit booleans)
- `schema_version`

This is not the #96 artifact bundle. It is the minimum provenance #93 requires.

## Enforcement

Capability checks fail closed: a missing or false capability denies the action.

```text
assert_capability(role, capability) -> None
assert_role_command_allowed(role, command) -> None
```

`assert_role_command_allowed`:

1. Calls existing `lab_run.assert_command_allowed` first. Rust/lab never-merge
   and force-push denials remain authoritative.
2. Classifies the command into zero or more `ResearchCapability` values.
3. Calls `assert_capability` for each classified capability.

Classification is conservative and explicit:

- `modify_code`: `git add`, `commit`, `apply`, `rebase`, `cherry-pick`,
  `reset`, `restore`, `mv`, `rm`, `checkout`, `switch`, plus `patch`.
- `execute_tests`: `pytest`, `python -m pytest`, `python -m unittest`,
  `cargo test`.
- `launch_experiments`: `worktrees-hives lab`, `wh-orch lab`, argv starting
  with `lab` as a recognized hive CLI verb. Do not invent a broader scheduler.
- `read_repository` and `read_results` are not inferred from commands.

Unclassified commands impose no extra role capability (the lab/Rust gate still
applies). That is fail-closed on declared capabilities, not a universal
command allowlist.

A read-only verifier therefore cannot take a `git commit` / `git add` /
`git apply` path even when the command would otherwise be allowed by
`assert_command_allowed`.

Do not wire this gate into `lab run` in #93. Document that #94 (and later
#95) must call it before granting a role a command or mutating action.

## Envelope placement

The role document nests under the existing v1 envelope. Illustrative only:

```json
{
  "ok": true,
  "schema_version": 1,
  "command": "lab.<command>",
  "data": {
    "research_role": { "schema_version": 1, "role_id": "verification_agent" },
    "role_binding": {
      "role_id": "verification_agent",
      "provider": "xai",
      "model_id": "grok-4.6",
      "agent_id": "verifier-1"
    }
  },
  "error": null
}
```

Outer `schema_version` is the transport envelope. Nested
`research_role.schema_version` is this domain document.

## Out of scope

No part of #94 or later: no coordinator loop, no experiment-plan execution, no
separation-of-duty runtime beyond recording `must_be_independent_of`, no
artifact tar/zip, no CLI subcommand, no Rust changes.
