"""Versioned Research Hive role document, v0 catalog, gate, and binding (GitHub #93).

This module owns the role domain document, the four built-in v0 roles, the
fail-closed capability gate, and RoleBinding provenance metadata. CLI and
lab_run wiring remain out of scope.
"""

from __future__ import annotations

import json
import math
import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from worktrees_hives.errors import ResearchRoleValidationError, RoleCapabilityError

if TYPE_CHECKING:
    from collections.abc import Sequence

# Domain-document version, independent of the outer Python/Rust JSON envelope.
RESEARCH_ROLE_SCHEMA_VERSION: int = 1

_REQUIRED_FIELDS: tuple[str, ...] = (
    "schema_version",
    "role_id",
    "capabilities",
    "inputs",
    "outputs",
    "constraints",
)

_KNOWN_FIELDS: frozenset[str] = frozenset(_REQUIRED_FIELDS)


class ResearchCapability(StrEnum):
    """Closed set of Research Hive v0 capabilities. Omitted keys default false."""

    READ_REPOSITORY = "read_repository"
    READ_RESULTS = "read_results"
    EXECUTE_TESTS = "execute_tests"
    MODIFY_CODE = "modify_code"
    LAUNCH_EXPERIMENTS = "launch_experiments"


_KNOWN_CAPABILITY_NAMES: frozenset[str] = frozenset(item.value for item in ResearchCapability)


@dataclass(frozen=True, slots=True)
class ResearchRoleCapabilities:
    """Explicit booleans for the five known capabilities. Missing keys are false."""

    read_repository: bool = False
    read_results: bool = False
    execute_tests: bool = False
    modify_code: bool = False
    launch_experiments: bool = False

    def __post_init__(self) -> None:
        for capability in ResearchCapability:
            value = getattr(self, capability.value)
            if type(value) is not bool:
                raise ResearchRoleValidationError(f"{capability.value} must be a boolean")

    def allows(self, capability: ResearchCapability | str) -> bool:
        """Return True only when the named capability is explicitly granted."""
        try:
            name = ResearchCapability(capability).value
        except ValueError:
            return False
        return getattr(self, name) is True

    def to_dict(self) -> dict[str, bool]:
        """Serialize every known capability key, including false defaults."""
        return {
            capability.value: getattr(self, capability.value) for capability in ResearchCapability
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ResearchRoleCapabilities:
        """Parse a capabilities object. Unknown keys are rejected."""
        if not isinstance(raw, dict):
            raise ResearchRoleValidationError("capabilities must be a JSON object")
        if not all(isinstance(name, str) for name in raw):
            raise ResearchRoleValidationError("capabilities field names must be strings")

        unknown = [name for name in raw if name not in _KNOWN_CAPABILITY_NAMES]
        if unknown:
            raise ResearchRoleValidationError(f"unknown capability {unknown[0]!r}")

        values: dict[str, bool] = {}
        for capability in ResearchCapability:
            if capability.value not in raw:
                continue
            value = raw[capability.value]
            if type(value) is not bool:
                raise ResearchRoleValidationError(f"{capability.value} must be a boolean")
            values[capability.value] = value
        return cls(**values)


@dataclass(frozen=True, slots=True)
class ResearchRole:
    """Immutable, versioned description of a Research Hive occupant role."""

    role_id: str
    capabilities: ResearchRoleCapabilities
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    constraints: Mapping[str, object]
    extensions: Mapping[str, object] = field(default_factory=dict, repr=False)
    schema_version: int = RESEARCH_ROLE_SCHEMA_VERSION
    # Derived from constraints so it cannot drift from the JSON object.
    must_be_independent_of: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        """Validate direct construction and defensively freeze all collections."""
        _validate_schema_version(self.schema_version)
        object.__setattr__(self, "role_id", _require_nonempty_string(self.role_id, "role_id"))

        if isinstance(self.capabilities, ResearchRoleCapabilities):
            capabilities = self.capabilities
        elif isinstance(self.capabilities, dict):
            capabilities = ResearchRoleCapabilities.from_dict(self.capabilities)
        else:
            raise ResearchRoleValidationError("capabilities must be a JSON object")
        object.__setattr__(self, "capabilities", capabilities)

        object.__setattr__(
            self,
            "inputs",
            _freeze_string_array(self.inputs, "inputs", require_nonempty=False),
        )
        object.__setattr__(
            self,
            "outputs",
            _freeze_string_array(self.outputs, "outputs", require_nonempty=False),
        )

        constraints = _freeze_json_object(self.constraints, "constraints")
        object.__setattr__(self, "constraints", constraints)
        object.__setattr__(self, "must_be_independent_of", _independent_of(constraints))

        extensions = _freeze_json_object(self.extensions, "extensions")
        collisions = sorted(set(extensions).intersection(_KNOWN_FIELDS))
        if collisions:
            raise ResearchRoleValidationError(
                "extensions cannot shadow known fields: " + ", ".join(collisions)
            )
        object.__setattr__(self, "extensions", extensions)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible object, preserving additive fields."""
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "role_id": self.role_id,
            "capabilities": self.capabilities.to_dict(),
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "constraints": _thaw_json_object(self.constraints),
        }
        for name, value in self.extensions.items():
            out[name] = _thaw_json_value(value)
        return out

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize as JSON text for the domain role document."""
        try:
            return (
                json.dumps(
                    self.to_dict(),
                    indent=indent,
                    sort_keys=False,
                    allow_nan=False,
                )
                + "\n"
            )
        except (TypeError, ValueError) as exc:
            raise ResearchRoleValidationError(f"cannot serialize role to JSON: {exc}") from exc

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ResearchRole:
        """Parse and validate a decoded research-role JSON object."""
        if not isinstance(raw, dict):
            raise ResearchRoleValidationError(f"role must be an object, got {type(raw).__name__}")
        if not all(isinstance(name, str) for name in raw):
            raise ResearchRoleValidationError("role field names must be strings")

        if "schema_version" not in raw:
            raise ResearchRoleValidationError("schema_version is required")
        _validate_schema_version(raw["schema_version"])

        for name in _REQUIRED_FIELDS:
            if name not in raw:
                raise ResearchRoleValidationError(f"{name} is required")

        for name in ("inputs", "outputs"):
            if not isinstance(raw[name], list):
                raise ResearchRoleValidationError(f"{name} must be an array of non-empty strings")

        extensions = {name: value for name, value in raw.items() if name not in _KNOWN_FIELDS}

        return cls(
            schema_version=raw["schema_version"],
            role_id=raw["role_id"],
            capabilities=ResearchRoleCapabilities.from_dict(
                _require_json_object(raw["capabilities"], "capabilities")
            ),
            inputs=raw["inputs"],
            outputs=raw["outputs"],
            constraints=_require_json_object(raw["constraints"], "constraints"),
            extensions=extensions,
        )


@dataclass(frozen=True, slots=True)
class RoleBinding:
    """Pair one ResearchRole contract with a model, provider, and agent identity."""

    role: ResearchRole
    model_id: str
    provider: str
    agent_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, ResearchRole):
            raise ResearchRoleValidationError("role must be a ResearchRole")
        object.__setattr__(
            self, "model_id", _require_nonempty_string(self.model_id, "model_id").strip()
        )
        object.__setattr__(
            self, "provider", _require_nonempty_string(self.provider, "provider").strip()
        )
        object.__setattr__(
            self, "agent_id", _require_nonempty_string(self.agent_id, "agent_id").strip()
        )


def binding_metadata(binding: RoleBinding) -> dict[str, Any]:
    """JSON-ready provenance for a role bound to a concrete model identity."""
    return {
        "schema_version": RESEARCH_ROLE_SCHEMA_VERSION,
        "role_id": binding.role.role_id,
        "model_id": binding.model_id,
        "provider": binding.provider,
        "agent_id": binding.agent_id,
        "capabilities": binding.role.capabilities.to_dict(),
    }


def parse_research_role_json(text: str) -> ResearchRole:
    """Decode JSON text into a validated, immutable research role."""
    if not text or not text.strip():
        raise ResearchRoleValidationError("research role JSON is empty")

    def _reject_non_finite(value: str) -> float:
        raise ResearchRoleValidationError(f"research role JSON contains non-finite number: {value}")

    try:
        raw = json.loads(text, parse_constant=_reject_non_finite)
    except json.JSONDecodeError as exc:
        raise ResearchRoleValidationError(f"research role is not valid JSON: {exc}") from exc
    except ResearchRoleValidationError:
        raise
    if not isinstance(raw, dict):
        raise ResearchRoleValidationError("research role JSON root must be an object")
    return ResearchRole.from_dict(raw)


def _v0_role_document(
    role_id: str,
    *,
    capabilities: dict[str, bool],
    inputs: list[str],
    outputs: list[str],
    must_be_independent_of: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": RESEARCH_ROLE_SCHEMA_VERSION,
        "role_id": role_id,
        "capabilities": dict(capabilities),
        "inputs": list(inputs),
        "outputs": list(outputs),
        "constraints": {"must_be_independent_of": list(must_be_independent_of)},
    }


_V0_ROLE_DOCUMENTS: tuple[dict[str, Any], ...] = (
    _v0_role_document(
        "research_coordinator",
        capabilities={
            "read_repository": True,
            "read_results": True,
            "execute_tests": False,
            "modify_code": False,
            "launch_experiments": False,
        },
        inputs=["question", "research_contract"],
        outputs=["experiment_plan.json"],
        must_be_independent_of=[],
    ),
    _v0_role_document(
        "experiment_agent",
        capabilities={
            "read_repository": True,
            "read_results": True,
            "execute_tests": True,
            "modify_code": True,
            "launch_experiments": True,
        },
        inputs=["research_contract", "experiment_plan"],
        outputs=["result_artifacts", "findings.json", "findings.md"],
        must_be_independent_of=[],
    ),
    _v0_role_document(
        "verification_agent",
        capabilities={
            "read_repository": True,
            "read_results": True,
            "execute_tests": True,
            "modify_code": False,
            "launch_experiments": False,
        },
        inputs=["hypothesis", "experiment_manifest", "result_artifacts"],
        outputs=["findings.json", "verification.md"],
        must_be_independent_of=["experiment_author"],
    ),
    _v0_role_document(
        "artifact_agent",
        capabilities={
            "read_repository": True,
            "read_results": True,
            "execute_tests": False,
            "modify_code": False,
            "launch_experiments": False,
        },
        inputs=["research_contract", "result_artifacts", "verification.md"],
        outputs=["artifact_bundle", "provenance.json"],
        must_be_independent_of=[],
    ),
)


def _validate_schema_version(value: object) -> None:
    if type(value) is not int:
        raise ResearchRoleValidationError("schema_version must be an int")
    schema_version: int = value
    if schema_version != RESEARCH_ROLE_SCHEMA_VERSION:
        raise ResearchRoleValidationError(
            f"unsupported schema_version {schema_version} (expected {RESEARCH_ROLE_SCHEMA_VERSION})"
        )


def _require_nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchRoleValidationError(f"{name} is required and must be a non-empty string")
    return value.strip()


def _freeze_string_array(
    value: object,
    name: str,
    *,
    require_nonempty: bool,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ResearchRoleValidationError(f"{name} must be an array of non-empty strings")
    if require_nonempty and not value:
        raise ResearchRoleValidationError(f"{name} must contain at least one item")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ResearchRoleValidationError(f"{name} must be an array of non-empty strings")
    return tuple(value)


def _require_json_object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResearchRoleValidationError(f"{name} must be a JSON object")
    return value


def _independent_of(constraints: Mapping[str, object]) -> tuple[str, ...]:
    if "must_be_independent_of" not in constraints:
        return ()
    return _freeze_string_array(
        constraints["must_be_independent_of"],
        "must_be_independent_of",
        require_nonempty=False,
    )


def _freeze_json_object(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ResearchRoleValidationError(f"{path} must be a JSON object")
    frozen: dict[str, object] = {}
    for name, nested in value.items():
        if not isinstance(name, str):
            raise ResearchRoleValidationError(f"{path} field names must be strings")
        frozen[name] = _freeze_json_value(nested, f"{path}.{name}")
    return MappingProxyType(frozen)


def _freeze_json_value(value: object, path: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ResearchRoleValidationError(f"{path} must be a finite JSON number")
        return value
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json_value(item, f"{path}[{index}]") for index, item in enumerate(value)
        )
    if isinstance(value, Mapping):
        return _freeze_json_object(value, path)
    raise ResearchRoleValidationError(
        f"{path} contains unsupported JSON value of type {type(value).__name__}"
    )


def _thaw_json_object(value: Mapping[str, object]) -> dict[str, object]:
    return {name: _thaw_json_value(nested) for name, nested in value.items()}


def _thaw_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _thaw_json_object(value)
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value


V0_RESEARCH_ROLES: Mapping[str, ResearchRole] = MappingProxyType(
    {document["role_id"]: ResearchRole.from_dict(document) for document in _V0_ROLE_DOCUMENTS}
)


def v0_role(role_id: str) -> ResearchRole:
    """Look up a built-in v0 role. Unknown ids are a validation error."""
    try:
        return V0_RESEARCH_ROLES[role_id]
    except KeyError as exc:
        raise ResearchRoleValidationError(f"unknown v0 role: {role_id!r}") from exc


_GIT_READ_ONLY_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "blame",
        "cat-file",
        "count-objects",
        "describe",
        "diff",
        "for-each-ref",
        "grep",
        "help",
        "log",
        "ls-files",
        "ls-tree",
        "name-rev",
        "rev-list",
        "rev-parse",
        "shortlog",
        "show",
        "status",
        "version",
        "whatchanged",
    }
)
_SHELL_EXECUTORS: frozenset[str] = frozenset(
    {
        "bash",
        "bash.exe",
        "busybox",
        "busybox.exe",
        "cmd",
        "cmd.exe",
        "dash",
        "dash.exe",
        "fish",
        "fish.exe",
        "ksh",
        "ksh.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "sh",
        "sh.exe",
        "zsh",
        "zsh.exe",
    }
)
_PYTHON_INTERPRETERS: frozenset[str] = frozenset(
    {
        "py",
        "py.exe",
        "python",
        "python3",
        "python.exe",
        "python3.exe",
        "pythonw",
        "pythonw.exe",
    }
)
_PYTHON_SIMPLE_SHORT_OPTIONS: frozenset[str] = frozenset("bBdEhiIOPqRsSuvVx")
_PYTHON_TEST_MODULES: frozenset[str] = frozenset(
    {"pytest", "pytest.__main__", "unittest", "unittest.__main__"}
)
_PYTHON_HIVE_MODULES: frozenset[str] = frozenset({"worktrees_hives.cli"})
_LAUNCH_CLI_NAMES: frozenset[str] = frozenset(
    {"worktrees-hives", "worktrees-hives.exe", "wh-orch", "wh-orch.exe"}
)
_HIVE_GLOBALS_WITH_VALUE: frozenset[str] = frozenset({"--state"})
_CARGO_GLOBALS_WITH_VALUE: frozenset[str] = frozenset(
    {"--color", "--config", "--explain", "--manifest-path", "-C", "-Z"}
)
_ENV_OPTIONS_WITH_VALUE: frozenset[str] = frozenset(
    {"-a", "--argv0", "-C", "--chdir", "-S", "--split-string", "-u", "--unset"}
)
_TIMEOUT_OPTIONS_WITH_VALUE: frozenset[str] = frozenset({"-k", "--kill-after", "-s", "--signal"})
_NICE_OPTIONS_WITH_VALUE: frozenset[str] = frozenset({"-n", "--adjustment"})
_SHELL_REQUIRED_CAPABILITIES: frozenset[ResearchCapability] = frozenset(
    {
        ResearchCapability.EXECUTE_TESTS,
        ResearchCapability.MODIFY_CODE,
        ResearchCapability.LAUNCH_EXPERIMENTS,
    }
)


def assert_capability(role: ResearchRole, capability: ResearchCapability | str) -> None:
    """Deny when the role does not explicitly grant ``capability``."""
    try:
        cap = (
            capability
            if isinstance(capability, ResearchCapability)
            else ResearchCapability(capability)
        )
    except ValueError:
        raise RoleCapabilityError(role.role_id, str(capability)) from None
    if not role.capabilities.allows(cap):
        raise RoleCapabilityError(role.role_id, cap.value)


def classify_command(command: str | Sequence[str]) -> frozenset[ResearchCapability]:
    """Map structured argv to required capabilities, including common wrappers."""
    raw_argv = _command_to_argv(command)
    if not raw_argv:
        return frozenset()

    argv = _unwrap_command(raw_argv)
    if argv is None:
        return frozenset({ResearchCapability.MODIFY_CODE})
    if not argv:
        return frozenset()

    executable = _token_basename(argv[0])

    if executable in {"git", "git.exe"}:
        sub_i, configuration_requires_modify = _skip_git_global_options(argv, 1)
        if configuration_requires_modify:
            return frozenset({ResearchCapability.MODIFY_CODE})
        if sub_i < len(argv) and not _git_command_is_read_only(argv, sub_i):
            return frozenset({ResearchCapability.MODIFY_CODE})
        return frozenset()

    if executable in _SHELL_EXECUTORS:
        return _SHELL_REQUIRED_CAPABILITIES

    if executable in {"patch", "patch.exe"}:
        return frozenset({ResearchCapability.MODIFY_CODE})

    if executable in {"py.test", "py.test.exe", "pytest", "pytest.exe"}:
        return frozenset({ResearchCapability.EXECUTE_TESTS})

    if _is_python_interpreter(executable):
        module_invocation = _python_module_invocation(argv)
        if module_invocation is not None:
            module, arguments_start = module_invocation
            if module in _PYTHON_TEST_MODULES:
                return frozenset({ResearchCapability.EXECUTE_TESTS})
            if module in _PYTHON_HIVE_MODULES and _hive_cli_invokes_lab(argv, arguments_start):
                return frozenset({ResearchCapability.LAUNCH_EXPERIMENTS})

    if executable in {"cargo", "cargo.exe"} and _cargo_invokes_test(argv, 1):
        return frozenset({ResearchCapability.EXECUTE_TESTS})

    if executable in {"lab", "lab.exe"}:
        return frozenset({ResearchCapability.LAUNCH_EXPERIMENTS})

    if executable in _LAUNCH_CLI_NAMES and _hive_cli_invokes_lab(argv, 1):
        return frozenset({ResearchCapability.LAUNCH_EXPERIMENTS})

    return frozenset()


def assert_role_command_allowed(role: ResearchRole, command: str | Sequence[str]) -> None:
    """Apply lab never-merge policy, then deny capabilities the role does not grant."""
    from worktrees_hives.lab_run import assert_command_allowed

    assert_command_allowed(command)
    required_capabilities = classify_command(command)
    for capability in ResearchCapability:
        if capability in required_capabilities:
            assert_capability(role, capability)


def _strip_win_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def _command_to_argv(command: str | Sequence[str]) -> list[str]:
    """Normalize command to argv. Duplicates lab_run; do not import its privates."""
    if isinstance(command, str):
        parts = shlex.split(command, posix=(os.name != "nt"))
        if os.name == "nt":
            parts = [_strip_win_quotes(part) for part in parts]
        return parts
    return list(command)


def _token_basename(token: str) -> str:
    return os.path.basename(token.rstrip("/\\")).casefold()


def _is_python_interpreter(executable: str) -> bool:
    """Recognize console/windowed and numerically versioned Python executables."""
    if executable in _PYTHON_INTERPRETERS:
        return True
    name = executable.removesuffix(".exe")
    for prefix in ("python", "pythonw"):
        if not name.startswith(prefix):
            continue
        version = name.removeprefix(prefix)
        if version and all(part.isascii() and part.isdecimal() for part in version.split(".")):
            return True
    return False


def _skip_git_global_options(argv: list[str], start: int) -> tuple[int, bool]:
    """Advance to the subcommand and flag unchecked config as modification-capable."""
    index = start
    configuration_requires_modify = False
    while index < len(argv):
        token = argv[index]
        if not token.startswith("-"):
            break
        if token == "-c" or (token.startswith("-c") and token != "-C"):
            configuration_requires_modify = True
        if token == "--config-env" or token.startswith("--config-env="):
            configuration_requires_modify = True
        if token in {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}:
            index += 2
            continue
        if token.startswith(("--git-dir=", "--work-tree=", "--namespace=", "--config-env=")):
            index += 1
            continue
        if token.startswith("--") and "=" in token:
            index += 1
            continue
        index += 1
    return index, configuration_requires_modify


def _git_command_is_read_only(argv: list[str], subcommand_index: int) -> bool:
    """Allow known inspection forms while treating unknown Git forms as mutating."""
    subcommand = argv[subcommand_index].casefold()
    arguments = argv[subcommand_index + 1 :]
    for token in arguments:
        if token == "--":
            break
        if token == "--output" or token.startswith("--output="):
            return False
        if subcommand == "grep" and (
            token.startswith("-O") or _is_git_long_option_prefix(token, "--open-files-in-pager")
        ):
            return False
    if subcommand in _GIT_READ_ONLY_SUBCOMMANDS:
        return True
    if subcommand == "branch":
        return not arguments or arguments == ["--show-current"]
    if subcommand == "remote":
        while arguments and arguments[0] in {"-v", "--verbose"}:
            arguments = arguments[1:]
        return not arguments or arguments[0] in {"get-url", "show"}
    if subcommand == "config":
        read_actions = {
            "--get",
            "--get-all",
            "--get-regexp",
            "--get-urlmatch",
            "--list",
            "-l",
        }
        write_actions = {
            "--add",
            "--edit",
            "-e",
            "--remove-section",
            "--rename-section",
            "--replace-all",
            "--unset",
            "--unset-all",
            "remove-section",
            "rename-section",
            "set",
            "unset",
        }
        return bool(read_actions.intersection(arguments)) and not write_actions.intersection(
            arguments
        )
    return False


def _is_git_long_option_prefix(token: str, option: str) -> bool:
    """Fail closed when a Git long-option token abbreviates ``option``."""
    name = token.partition("=")[0]
    return name.startswith("--") and option.startswith(name)


def _option_takes_value(token: str, value_options: frozenset[str]) -> bool:
    """True for a value-taking flag or a unique long-option prefix (argparse allow_abbrev)."""
    if token in value_options:
        return True
    if not token.startswith("--") or token == "--" or "=" in token:
        return False
    return sum(1 for option in value_options if option.startswith(token)) == 1


def _skip_leading_options(argv: list[str], start: int, *, value_options: frozenset[str]) -> int:
    """Advance past leading flags, including those that consume a following argument."""
    index = start
    while index < len(argv):
        token = argv[index]
        if not token.startswith("-"):
            break
        if _option_takes_value(token, value_options):
            index += 2
            continue
        if token.startswith("--") and "=" in token:
            index += 1
            continue
        index += 1
    return index


def _skip_wrapper_options(
    argv: list[str],
    start: int,
    *,
    value_options: frozenset[str],
) -> int | None:
    """Return the first operand, failing closed on ambiguous short-option clusters."""
    index = start
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return index + 1
        if not token.startswith("-") or token == "-":
            return index
        if not token.startswith("--") and len(token) > 2:
            return None
        if _option_takes_value(token, value_options):
            index += 2
        else:
            index += 1
    return index


def _env_split_string_payload(argv: list[str], index: int) -> tuple[str, int] | None:
    """Return an env split-string payload and the number of consumed argv items."""
    token = argv[index]
    if token == "-S":
        return (argv[index + 1], 2) if index + 1 < len(argv) else None
    if token.startswith("-S") and not token.startswith("--"):
        return token[2:], 1
    if not token.startswith("--"):
        return None

    option, separator, inline_value = token.partition("=")
    if option != "--split-string" and not (option != "--" and "--split-string".startswith(option)):
        return None
    if separator:
        return inline_value, 1
    return (argv[index + 1], 2) if index + 1 < len(argv) else None


def _unwrap_env(argv: list[str]) -> list[str] | None:
    """Peel GNU env options while rejecting behavior-changing assignments."""
    current = argv
    index = 1
    split_count = 0
    while index < len(current):
        token = current[index]
        if token == "--":
            index += 1
            continue
        if token == "-":
            index += 1
            continue
        if not token.startswith("-"):
            if "=" in token:
                return None
            return current[index:]

        split_payload = _env_split_string_payload(current, index)
        if split_payload is not None:
            payload, consumed = split_payload
            split_count += 1
            if split_count > 8:
                return None
            if "$" in payload or "\\" in payload:
                return None
            try:
                expanded = _command_to_argv(payload)
            except ValueError:
                return None
            current = current[:index] + expanded + current[index + consumed :]
            continue

        if not token.startswith("--") and len(token) > 2:
            return None
        if _option_takes_value(token, _ENV_OPTIONS_WITH_VALUE):
            if index + 1 >= len(current):
                return None
            index += 2
        else:
            index += 1
    return []


def _unwrap_command(argv: list[str]) -> list[str] | None:
    """Peel supported argv wrappers without inspecting ordinary command arguments."""
    current = argv
    while current:
        executable = _token_basename(current[0])
        if executable in {"env", "env.exe"}:
            unwrapped = _unwrap_env(current)
            if unwrapped is None:
                return None
            current = unwrapped
            continue
        elif executable in {"timeout", "timeout.exe"}:
            index = _skip_wrapper_options(current, 1, value_options=_TIMEOUT_OPTIONS_WITH_VALUE)
            if index is None:
                return None
            index += 1  # duration
        elif executable in {"nice", "nice.exe"}:
            index = _skip_wrapper_options(current, 1, value_options=_NICE_OPTIONS_WITH_VALUE)
            if index is None:
                return None
        else:
            return current
        current = current[index:] if index < len(current) else []
    return current


def _cargo_invokes_test(argv: list[str], start: int) -> bool:
    """Recognize ``cargo test`` even when a ``+toolchain`` prefix is present."""
    index = start
    if index < len(argv) and argv[index].startswith("+"):
        index += 1
    index = _skip_leading_options(argv, index, value_options=_CARGO_GLOBALS_WITH_VALUE)
    return index < len(argv) and argv[index].casefold() in {"t", "test"}


def _python_module_invocation(argv: list[str]) -> tuple[str, int] | None:
    """Return a Python ``-m`` module and its argument start before any script operand."""
    index = 1
    while index < len(argv):
        token = argv[index]
        if token in {"-", "--"}:
            return None
        if not token.startswith("-"):
            return None

        if token.startswith("-") and not token.startswith("--"):
            short_options = token[1:]
            for option_index, option in enumerate(short_options):
                if option == "m":
                    inline_module = short_options[option_index + 1 :]
                    if inline_module:
                        return inline_module, index + 1
                    if index + 1 >= len(argv):
                        return None
                    return argv[index + 1], index + 2
                if option == "c":
                    return None
                if option not in _PYTHON_SIMPLE_SHORT_OPTIONS:
                    break

        if token in {"-W", "-X", "--check-hash-based-pycs"}:
            index += 2
        else:
            index += 1
    return None


def _hive_cli_invokes_lab(argv: list[str], start: int) -> bool:
    """Recognize the lab subcommand after supported hive CLI global options."""
    subcommand_index = _skip_leading_options(
        argv,
        start,
        value_options=_HIVE_GLOBALS_WITH_VALUE,
    )
    return subcommand_index < len(argv) and argv[subcommand_index].casefold() in {
        "lab",
        "lab.exe",
    }
