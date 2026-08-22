# Research Hive v0 roles + capability policy Implementation Plan

> **Implementation reference:** This completed plan records the intended sequence, while Beads is the sole source of task state. Agents must use the worktree and branch assigned to their current job.

**Goal:** Add a versioned, fail-closed Research Hive role contract so four v0 roles are declarative capabilities, not model personas (GitHub #93 / RM-598).

**Architecture:** Follow `worktrees_hives.research` (#92). New module `worktrees_hives.research_roles` owns schema, catalog, binding metadata, and a Python policy gate that composes with `lab_run.assert_command_allowed`. Rust remains the mutation authority. No CLI and no `lab run` wiring.

**Tech Stack:** Python 3.14, pytest, ruff, mypy. No new dependencies.

**Worktree:** Use the isolated worktree and feature branch assigned to the current job. Never edit the default branch.

Before running task commands, set `ASSIGNED_WORKTREE` and `ASSIGNED_BRANCH` from the current job assignment (never infer or hard-code them), then run this pre-edit checklist and define the reusable mutation guard:

```bash
assert_assigned_branch() {
  test "$(git rev-parse --show-toplevel)" = "${ASSIGNED_WORKTREE:?set from job assignment}"
  test "$(git branch --show-current)" = "${ASSIGNED_BRANCH:?set from job assignment}"
}

cd "${ASSIGNED_WORKTREE:?set from job assignment}"
assert_assigned_branch
test -z "$(git status --porcelain)"
git fetch
git status --short --branch
test "$(git rev-parse HEAD)" = "$(git rev-parse '@{upstream}')"
```

Abort before editing if any checklist command fails or the status shows an unexpected remote.
Call `assert_assigned_branch` immediately before every `git add`, `git commit`, pull, or push below.

**TDD:** Every production change starts with a failing test. Watch it fail, then implement.

**Commit trailer:** include `GitHub-Issue: <configured-owner>/<repository>#93` and `Linear: RM-598`. Agent attribution belongs in the commit body and must use the executing agent identity supplied by the current job; replace `<executing-agent-id>` below rather than copying an identity from this plan.

**Quality gates after each task (from the worktree `python/` directory):**

```bash
python3 -m pytest tests/test_research_roles.py -q
python3 -m pytest -q
python3 -m ruff check src/worktrees_hives/research_roles.py src/worktrees_hives/errors.py src/worktrees_hives/__init__.py tests/test_research_roles.py
python3 -m ruff format --check src tests
python3 -m mypy --config-file pyproject.toml
```

---

## File map

- Create: `python/src/worktrees_hives/research_roles.py` — domain types, catalog, binding, enforcement
- Create: `python/tests/test_research_roles.py` — unit tests
- Create: `docs/examples/research-roles-v0.json` — four-role fixture
- Create: `docs/superpowers/specs/2026-08-18-research-roles-design.md` — already written by the controller
- Modify: `python/src/worktrees_hives/errors.py` — `ResearchRoleValidationError`, `RoleCapabilityError`
- Modify: `python/src/worktrees_hives/__init__.py` — public exports
- Modify: `docs/json-contract.md` — nest `research_role` / `role_binding` and document the enforcement point

Do not modify `lab_run.py`, `cli.py`, Rust, or `findings.AgentRole`.

---

### Task 1: ResearchRole schema + validation

**Beads:** `worktrees-hives-ob0`

**Files:**
- Create: `python/src/worktrees_hives/research_roles.py`
- Create: `python/tests/test_research_roles.py`
- Modify: `python/src/worktrees_hives/errors.py`

**Do not** add `V0_RESEARCH_ROLES`, `RoleBinding`, or command enforcement yet.

- **Step 1: Write failing tests for errors + role parse**

Add to `errors.py` only after the first test fails for missing names. Put tests in `python/tests/test_research_roles.py`:

```python
"""Tests for Research Hive v0 roles + capability policy (GitHub #93)."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from typing import Any

import pytest

from worktrees_hives.errors import PolicyError, ResearchRoleValidationError
from worktrees_hives.research_roles import (
    RESEARCH_ROLE_SCHEMA_VERSION,
    ResearchCapability,
    ResearchRole,
    parse_research_role_json,
)


def _valid_role(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "schema_version": RESEARCH_ROLE_SCHEMA_VERSION,
        "role_id": "verification_agent",
        "capabilities": {
            "read_repository": True,
            "read_results": True,
            "execute_tests": True,
            "modify_code": False,
            "launch_experiments": False,
        },
        "inputs": ["hypothesis", "experiment_manifest", "result_artifacts"],
        "outputs": ["findings.json", "verification.md"],
        "constraints": {"must_be_independent_of": ["experiment_author"]},
    }
    raw.update(overrides)
    return raw


class TestResearchRoleRoundTrip:
    def test_dict_round_trip(self) -> None:
        raw = _valid_role()
        role = ResearchRole.from_dict(raw)
        assert role.to_dict() == raw
        assert ResearchRole.from_dict(role.to_dict()) == role

    def test_json_round_trip(self) -> None:
        role = ResearchRole.from_dict(_valid_role())
        assert parse_research_role_json(role.to_json()) == role

    def test_omitted_capability_defaults_false(self) -> None:
        raw = _valid_role(capabilities={"read_repository": True})
        role = ResearchRole.from_dict(raw)
        assert role.capabilities.read_repository is True
        assert role.capabilities.modify_code is False
        assert role.capabilities.launch_experiments is False
        assert role.to_dict()["capabilities"]["modify_code"] is False

    def test_unknown_additive_fields_are_preserved(self) -> None:
        raw = _valid_role(review_protocol={"blind": True})
        role = ResearchRole.from_dict(raw)
        assert role.to_dict()["review_protocol"] == {"blind": True}

    def test_capability_vocabulary_is_exact(self) -> None:
        assert [c.value for c in ResearchCapability] == [
            "read_repository",
            "read_results",
            "execute_tests",
            "modify_code",
            "launch_experiments",
        ]


class TestResearchRoleValidation:
    @pytest.mark.parametrize("name", ["schema_version", "role_id", "capabilities", "inputs", "outputs", "constraints"])
    def test_rejects_missing_required_field(self, name: str) -> None:
        raw = _valid_role()
        del raw[name]
        with pytest.raises(ResearchRoleValidationError, match=name):
            ResearchRole.from_dict(raw)

    def test_rejects_empty_role_id(self) -> None:
        with pytest.raises(ResearchRoleValidationError, match="role_id"):
            ResearchRole.from_dict(_valid_role(role_id="  "))

    def test_rejects_unknown_capability_key(self) -> None:
        with pytest.raises(ResearchRoleValidationError, match="unknown capability"):
            ResearchRole.from_dict(_valid_role(capabilities={"merge_pull_request": True}))

    def test_rejects_non_bool_capability(self) -> None:
        with pytest.raises(ResearchRoleValidationError, match="read_repository"):
            ResearchRole.from_dict(_valid_role(capabilities={"read_repository": "yes"}))

    def test_rejects_non_object_capabilities(self) -> None:
        with pytest.raises(ResearchRoleValidationError, match="capabilities"):
            ResearchRole.from_dict(_valid_role(capabilities=["modify_code"]))

    def test_allows_empty_inputs_and_outputs(self) -> None:
        role = ResearchRole.from_dict(_valid_role(inputs=[], outputs=[]))
        assert role.inputs == ()
        assert role.outputs == ()

    def test_rejects_blank_input(self) -> None:
        with pytest.raises(ResearchRoleValidationError, match="inputs"):
            ResearchRole.from_dict(_valid_role(inputs=["hypothesis", ""]))

    def test_rejects_non_list_must_be_independent_of(self) -> None:
        with pytest.raises(ResearchRoleValidationError, match="must_be_independent_of"):
            ResearchRole.from_dict(_valid_role(constraints={"must_be_independent_of": "experiment_author"}))

    def test_constraints_may_be_empty(self) -> None:
        role = ResearchRole.from_dict(_valid_role(constraints={}))
        assert role.must_be_independent_of == ()

    def test_rejects_invalid_schema_version(self) -> None:
        with pytest.raises(ResearchRoleValidationError, match="schema_version"):
            ResearchRole.from_dict(_valid_role(schema_version=2))

    def test_rejects_malformed_json(self) -> None:
        with pytest.raises(ResearchRoleValidationError):
            parse_research_role_json("{not json")


class TestResearchRoleImmutability:
    def test_input_mutation_cannot_change_role(self) -> None:
        raw = _valid_role()
        role = ResearchRole.from_dict(raw)
        raw["inputs"].append("late")
        raw["capabilities"]["modify_code"] = True
        assert "late" not in role.inputs
        assert role.capabilities.modify_code is False

    def test_frozen(self) -> None:
        role = ResearchRole.from_dict(_valid_role())
        with pytest.raises(FrozenInstanceError):
            role.role_id = "other"  # type: ignore[misc]
```

- **Step 2: Run tests to verify they fail**

```bash
(cd python && python3 -m pytest tests/test_research_roles.py -q)
```

Expected: import or collection failure (`ResearchRoleValidationError` / `research_roles` missing).

- **Step 3: Add errors**

In `python/src/worktrees_hives/errors.py`, after `ResearchValidationError`:

```python
class ResearchRoleValidationError(WhError):
    """Raised when a Research Hive role document is invalid."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Invalid research role: {detail}")


class RoleCapabilityError(PolicyError):
    """Raised when a research role is denied a capability or command."""

    def __init__(self, role_id: str, capability: str) -> None:
        self.role_id = role_id
        self.capability = capability
        super().__init__(
            "ROLE_CAPABILITY_DENIED",
            f"role {role_id!r} is not allowed to {capability}",
        )
```

`RoleCapabilityError` may be unused until Task 3. That is fine; Task 1 tests do not import it yet.

- **Step 4: Implement `research_roles.py` (schema only)**

Implement:

- `RESEARCH_ROLE_SCHEMA_VERSION = 1`
- `ResearchCapability` StrEnum with the five values above
- frozen `ResearchRoleCapabilities` with five bool fields defaulting `False`, `from_dict` / `to_dict` / `allows`
- frozen slotted `ResearchRole` with `role_id`, `capabilities`, `inputs`, `outputs`, `constraints` (Mapping), `must_be_independent_of` (tuple derived from constraints), `extensions`, `schema_version`
- `from_dict`, `to_dict`, `to_json`, `parse_research_role_json`
- omitted capability keys → `False`
- unknown capability keys → `ResearchRoleValidationError` matching `unknown capability`
- `constraints.must_be_independent_of` optional; if present, array of non-empty strings
- unknown top-level fields frozen into `extensions` and re-emitted
- reuse the same freeze/thaw/non-finite JSON rules as `research.py` (copy helpers into this module; do not import private `_freeze_*` from `research.py`)

`must_be_independent_of` should be a convenience attribute populated in `__post_init__` from `constraints`. Include it in equality via the frozen dataclass (derive it, do not take it as a constructor field that can drift). Suggested constructor field: only `constraints`; set `must_be_independent_of` with `object.__setattr__`.

- **Step 5: Re-run tests**

```bash
(cd python && python3 -m pytest tests/test_research_roles.py -q)
```

Expected: PASS.

- **Step 6: Commit**

```bash
assert_assigned_branch
git add python/src/worktrees_hives/errors.py \
        python/src/worktrees_hives/research_roles.py \
        python/tests/test_research_roles.py
assert_assigned_branch
git commit -m "$(cat <<'EOF'
feat(python): add Research Hive role schema (#93)

Versioned ResearchRole + fail-closed capability defaults.

GitHub-Issue: <configured-owner>/<repository>#93
Linear: RM-598
Agent: <executing-agent-id> (task 1)
EOF
)"
```

---

### Task 2: Four v0 role catalog + JSON fixture

**Beads:** `worktrees-hives-jg1`

**Files:**
- Modify: `python/src/worktrees_hives/research_roles.py`
- Modify: `python/tests/test_research_roles.py`
- Create: `docs/examples/research-roles-v0.json`

- **Step 1: Write failing catalog tests**

```python
from pathlib import Path
from worktrees_hives.research_roles import V0_RESEARCH_ROLES, v0_role

FIXTURE_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "examples" / "research-roles-v0.json"
)


class TestV0Catalog:
    def test_four_role_ids(self) -> None:
        assert tuple(V0_RESEARCH_ROLES) == (
            "research_coordinator",
            "experiment_agent",
            "verification_agent",
            "artifact_agent",
        )

    def test_verification_agent_cannot_modify_or_launch(self) -> None:
        role = v0_role("verification_agent")
        assert role.capabilities.modify_code is False
        assert role.capabilities.launch_experiments is False
        assert role.capabilities.execute_tests is True
        assert role.must_be_independent_of == ("experiment_author",)

    def test_experiment_agent_may_modify_and_launch(self) -> None:
        role = v0_role("experiment_agent")
        assert role.capabilities.modify_code is True
        assert role.capabilities.launch_experiments is True

    def test_coordinator_and_artifact_are_non_mutating(self) -> None:
        for role_id in ("research_coordinator", "artifact_agent"):
            role = v0_role(role_id)
            assert role.capabilities.modify_code is False
            assert role.capabilities.execute_tests is False
            assert role.capabilities.launch_experiments is False
            assert role.capabilities.read_repository is True
            assert role.capabilities.read_results is True

    def test_unknown_catalog_id_raises(self) -> None:
        with pytest.raises(ResearchRoleValidationError, match="unknown v0 role"):
            v0_role("literature_agent")

    def test_fixture_round_trip(self) -> None:
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        roles = [ResearchRole.from_dict(item) for item in payload["roles"]]
        assert [r.role_id for r in roles] == list(V0_RESEARCH_ROLES)
        for role in roles:
            assert role == v0_role(role.role_id)
```

- **Step 2: Run to verify fail**

```bash
(cd python && python3 -m pytest tests/test_research_roles.py::TestV0Catalog -q)
```

Expected: FAIL (`V0_RESEARCH_ROLES` missing).

- **Step 3: Implement catalog + fixture**

`v0_role(role_id: str) -> ResearchRole` looks up `V0_RESEARCH_ROLES` or raises `ResearchRoleValidationError` matching `unknown v0 role`.

Catalog capability matrix (spec):

| role_id | read_repository | read_results | execute_tests | modify_code | launch_experiments | must_be_independent_of |
| --- | --- | --- | --- | --- | --- | --- |
| research_coordinator | true | true | false | false | false | [] |
| experiment_agent | true | true | true | true | true | [] |
| verification_agent | true | true | true | false | false | ["experiment_author"] |
| artifact_agent | true | true | false | false | false | [] |

Suggested inputs/outputs (must be stable; fixture and catalog must match):

- coordinator: inputs `["question", "research_contract"]`, outputs `["experiment_plan.json"]`
- experiment_agent: inputs `["research_contract", "experiment_plan"]`, outputs `["result_artifacts", "findings.json", "findings.md"]`
- verification_agent: inputs `["hypothesis", "experiment_manifest", "result_artifacts"]`, outputs `["findings.json", "verification.md"]`
- artifact_agent: inputs `["research_contract", "result_artifacts", "verification.md"]`, outputs `["artifact_bundle", "provenance.json"]`

`docs/examples/research-roles-v0.json` shape:

```json
{
  "schema_version": 1,
  "roles": [ { "...full role objects..." } ]
}
```

`V0_RESEARCH_ROLES` is a `MappingProxyType` (or other read-only mapping) keyed in the order above.

- **Step 4: Tests pass + commit**

```bash
(cd python && python3 -m pytest tests/test_research_roles.py -q)
assert_assigned_branch
git add python/src/worktrees_hives/research_roles.py \
        python/tests/test_research_roles.py \
        docs/examples/research-roles-v0.json
assert_assigned_branch
git commit -m "$(cat <<'EOF'
feat(python): add four v0 Research Hive role catalog (#93)

Declarative coordinator, experiment, verification, and artifact roles.

GitHub-Issue: <configured-owner>/<repository>#93
Linear: RM-598
Agent: <executing-agent-id> (task 2)
EOF
)"
```

---

### Task 3: Fail-closed capability enforcement

**Beads:** `worktrees-hives-esz`

**Files:**
- Modify: `python/src/worktrees_hives/research_roles.py`
- Modify: `python/tests/test_research_roles.py`

- **Step 1: Write failing enforcement tests**

```python
from worktrees_hives.errors import PolicyError, RoleCapabilityError
from worktrees_hives.research_roles import (
    assert_capability,
    assert_role_command_allowed,
    classify_command,
    v0_role,
)


class TestCapabilityEnforcement:
    def test_false_capability_is_denied(self) -> None:
        role = v0_role("verification_agent")
        with pytest.raises(RoleCapabilityError, match="modify_code") as exc:
            assert_capability(role, ResearchCapability.MODIFY_CODE)
        assert exc.value.code == "ROLE_CAPABILITY_DENIED"
        assert isinstance(exc.value, PolicyError)

    def test_true_capability_is_allowed(self) -> None:
        assert_capability(v0_role("verification_agent"), ResearchCapability.EXECUTE_TESTS)

    def test_verifier_cannot_git_commit(self) -> None:
        with pytest.raises(RoleCapabilityError, match="modify_code"):
            assert_role_command_allowed(v0_role("verification_agent"), ["git", "commit", "-am", "x"])

    def test_verifier_cannot_git_add_or_apply(self) -> None:
        role = v0_role("verification_agent")
        with pytest.raises(RoleCapabilityError, match="modify_code"):
            assert_role_command_allowed(role, "git add src/foo.py")
        with pytest.raises(RoleCapabilityError, match="modify_code"):
            assert_role_command_allowed(role, ["git", "apply", "change.patch"])

    def test_verifier_may_run_pytest(self) -> None:
        assert_role_command_allowed(v0_role("verification_agent"), ["pytest", "-q"])
        assert_role_command_allowed(v0_role("verification_agent"), ["python", "-m", "pytest", "-q"])

    def test_experiment_agent_may_commit(self) -> None:
        assert_role_command_allowed(v0_role("experiment_agent"), ["git", "commit", "-m", "x"])

    def test_verifier_cannot_launch_lab(self) -> None:
        with pytest.raises(RoleCapabilityError, match="launch_experiments"):
            assert_role_command_allowed(
                v0_role("verification_agent"),
                ["worktrees-hives", "lab", "run", "--hypothesis-id", "h1"],
            )

    def test_merge_still_denied_by_lab_policy(self) -> None:
        with pytest.raises(PolicyError, match="NEVER_MERGE"):
            assert_role_command_allowed(v0_role("experiment_agent"), "gh pr merge 1 --squash")

    def test_classify_commit_is_modify_code(self) -> None:
        assert ResearchCapability.MODIFY_CODE in classify_command(["git", "commit", "-m", "x"])
        assert ResearchCapability.EXECUTE_TESTS in classify_command(["pytest"])
        assert ResearchCapability.LAUNCH_EXPERIMENTS in classify_command(
            ["wh-orch", "lab", "run"]
        )
        assert classify_command(["git", "status"]) == frozenset()
```

- **Step 2: Run to verify fail**

```bash
(cd python && python3 -m pytest tests/test_research_roles.py::TestCapabilityEnforcement -q)
```

Expected: FAIL (functions missing).

- **Step 3: Implement enforcement**

```python
def assert_capability(role: ResearchRole, capability: ResearchCapability | str) -> None:
    cap = capability if isinstance(capability, ResearchCapability) else ResearchCapability(capability)
    if not role.capabilities.allows(cap):
        raise RoleCapabilityError(role.role_id, cap.value)


def classify_command(command: str | Sequence[str]) -> frozenset[ResearchCapability]:
    ...


def assert_role_command_allowed(role: ResearchRole, command: str | Sequence[str]) -> None:
    from worktrees_hives.lab_run import assert_command_allowed

    assert_command_allowed(command)
    for cap in classify_command(command):
        assert_capability(role, cap)
```

Classification rules (must match tests):

- Normalize via the same argv split idea as `lab_run._command_to_argv` (you may duplicate a tiny helper; do not export lab_run privates).
- Detect `git` / `git.exe`, parse supported global options, and inspect the subcommand. Unchecked runtime configuration (`-c` / `--config-env`) requires `modify_code`.
- Git classification is deny-by-default: only `_GIT_READ_ONLY_SUBCOMMANDS` and the explicitly inspected read-only forms require no capability. Options that write output or launch a selected process, such as `grep -O` / `--open-files-in-pager`, map to `modify_code`. Every other subcommand maps to `modify_code`, including `add`, `am`, `apply`, `checkout`, `cherry-pick`, `clean`, `commit`, `merge`, `mv`, `pull`, `rebase`, `reset`, `restore`, `revert`, `rm`, `stash`, and `switch`. Basename `patch` also maps to `modify_code`.
- Supported command wrappers are parsed conservatively; environment assignments, ambiguous short-option clusters, and unsupported GNU `env -S` escapes fail closed as `modify_code`.
- `execute_tests`: basename `pytest`; or an unversioned/versioned Python interpreter with `-m pytest`, `-m pytest.__main__`, `-m unittest`, or `-m unittest.__main__`; or `cargo test` (after cargo global options, treat `test` as the cargo subcommand).
- `launch_experiments`: argv `worktrees-hives lab …`, `wh-orch lab …`, an unversioned/versioned Python interpreter running `-m worktrees_hives.cli lab …`, or first token `lab`.
- `git status` / `git log` / `git diff` → empty set.

Import `RoleCapabilityError` from `worktrees_hives.errors`.

- **Step 4: Tests pass + commit**

```bash
(cd python && python3 -m pytest tests/test_research_roles.py -q)
assert_assigned_branch
git add python/src/worktrees_hives/research_roles.py python/tests/test_research_roles.py
assert_assigned_branch
git commit -m "$(cat <<'EOF'
feat(python): enforce Research Hive role capabilities (#93)

Fail-closed gate composes with lab never-merge policy.

GitHub-Issue: <configured-owner>/<repository>#93
Linear: RM-598
Agent: <executing-agent-id> (task 3)
EOF
)"
```

---

### Task 4: RoleBinding provenance + exports + docs

**Beads:** `worktrees-hives-eii`

**Files:**
- Modify: `python/src/worktrees_hives/research_roles.py`
- Modify: `python/tests/test_research_roles.py`
- Modify: `python/src/worktrees_hives/__init__.py`
- Modify: `docs/json-contract.md`

- **Step 1: Write failing binding/export tests**

```python
from worktrees_hives.errors import ResearchRoleValidationError
from worktrees_hives.research_roles import RoleBinding, binding_metadata, v0_role


class TestRoleBinding:
    def test_two_models_bind_to_same_role_contract(self) -> None:
        role = v0_role("verification_agent")
        grok = RoleBinding(role=role, model_id="grok-4.6", provider="xai", agent_id="verifier-a")
        claude = RoleBinding(
            role=role, model_id="claude-opus", provider="anthropic", agent_id="verifier-b"
        )
        assert grok.role == claude.role
        assert grok.role is role
        assert grok.model_id != claude.model_id
        meta_a = binding_metadata(grok)
        meta_b = binding_metadata(claude)
        assert meta_a["role_id"] == meta_b["role_id"] == "verification_agent"
        assert meta_a["provider"] == "xai"
        assert meta_b["provider"] == "anthropic"
        assert meta_a["capabilities"]["modify_code"] is False
        assert meta_a["schema_version"] == RESEARCH_ROLE_SCHEMA_VERSION

    def test_rejects_blank_identities(self) -> None:
        role = v0_role("verification_agent")
        with pytest.raises(ResearchRoleValidationError, match="model_id"):
            RoleBinding(role=role, model_id=" ", provider="xai", agent_id="a")
```

Also add `test_package_exports_research_roles` in the same file:

```python
def test_package_exports_research_roles() -> None:
    import worktrees_hives as wh

    assert wh.ResearchRole is ResearchRole
    assert wh.RoleBinding is RoleBinding
    assert wh.v0_role is v0_role
    assert wh.assert_role_command_allowed is assert_role_command_allowed
```

- **Step 2: Run to verify fail**

```bash
(cd python && python3 -m pytest tests/test_research_roles.py::TestRoleBinding tests/test_research_roles.py::test_package_exports_research_roles -q)
```

Expected: FAIL (`RoleBinding` missing and/or export missing).

- **Step 3: Implement binding + exports + docs**

`RoleBinding` frozen slotted dataclass: `role: ResearchRole`, `model_id: str`, `provider: str`, `agent_id: str`. Non-empty stripped strings.

`binding_metadata(binding) -> dict[str, Any]`:

```python
{
    "schema_version": RESEARCH_ROLE_SCHEMA_VERSION,
    "role_id": binding.role.role_id,
    "model_id": binding.model_id,
    "provider": binding.provider,
    "agent_id": binding.agent_id,
    "capabilities": binding.role.capabilities.to_dict(),
}
```

Export from `python/src/worktrees_hives/__init__.py` (import + `__all__`):

- `RESEARCH_ROLE_SCHEMA_VERSION`
- `ResearchCapability`
- `ResearchRole`
- `ResearchRoleCapabilities`
- `RoleBinding`
- `RoleCapabilityError`
- `ResearchRoleValidationError`
- `V0_RESEARCH_ROLES`
- `assert_capability`
- `assert_role_command_allowed`
- `binding_metadata`
- `classify_command`
- `parse_research_role_json`
- `v0_role`

Document in `docs/json-contract.md` immediately after the research-contract section:

- domain document lives at `data.research_role`
- binding/provenance lives at `data.role_binding`
- independent `schema_version`
- five capabilities, omitted = false
- four v0 role ids
- enforcement point: `assert_role_command_allowed` must be called by future research orchestration (#94/#95) **before** a role is granted a command; it always calls `assert_command_allowed` first; Rust remains authoritative for actual git/gh
- #93 does not add a CLI command
- link the spec and `docs/examples/research-roles-v0.json`

- **Step 4: Full Python gates + commit**

```bash
(cd python && python3 -m pytest -q && python3 -m ruff check src tests && python3 -m mypy --config-file pyproject.toml)
```

```bash
assert_assigned_branch
git add python/src/worktrees_hives/research_roles.py \
        python/src/worktrees_hives/__init__.py \
        python/src/worktrees_hives/errors.py \
        python/tests/test_research_roles.py \
        docs/json-contract.md \
        docs/examples/research-roles-v0.json \
        docs/superpowers/specs/2026-08-18-research-roles-design.md \
        docs/superpowers/plans/2026-08-18-research-roles.md
assert_assigned_branch
git commit -m "$(cat <<'EOF'
feat(python): bind Research Hive roles to model identity (#93)

RoleBinding provenance plus envelope documentation.

GitHub-Issue: <configured-owner>/<repository>#93
Linear: RM-598
Agent: <executing-agent-id> (task 4)
EOF
)"
```

Include the design spec and this plan in the last commit if they are not yet committed. If the controller already committed them, skip those paths.

---

## Session completion

After the last task, run the complete gates, update the claimed Beads issue, and push from the assigned branch. Do not declare completion until the local and upstream SHAs match.

```bash
assert_assigned_branch
cd "${ASSIGNED_WORKTREE:?set from job assignment}/python"
python3 -m pytest -q
python3 -m ruff check src tests
python3 -m ruff format --check src tests
python3 -m mypy --config-file pyproject.toml
cd "${ASSIGNED_WORKTREE:?set from job assignment}"
bd close "${CLAIMED_BEADS_ID:?set to the claimed task}"
test -z "$(git status --porcelain)"
assert_assigned_branch
git pull --rebase
assert_assigned_branch
git push
git status --short --branch
test "$(git rev-parse HEAD)" = "$(git rev-parse '@{upstream}')"
```

---

## Spec coverage checklist

| #93 acceptance | Task |
| --- | --- |
| Versioned role schema/type | 1 |
| Four v0 roles represented declaratively | 2 |
| Capability enforcement point documented | 3 (code) + 4 (docs) |
| Tests: read-only verifier cannot use a forbidden code-mutation path | 3 |
| Runtime can bind two model/provider identities to the same role | 4 |
| Role + model identity in run metadata | 4 |

## Out of scope (do not implement)

- Wiring into `lab run` / CLI
- Coordinator orchestration (#94)
- Separation-of-duty runtime beyond storing `must_be_independent_of` (#95)
- Artifact bundle assembly (#96)
- Rust changes
