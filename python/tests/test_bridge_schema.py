"""Schema-negotiation and verified-identity tests for the wh bridge."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from worktrees_hives.bridge import (
    WhClient,
    _requested_schema_version,
    _schema_selector_is_unsupported,
)
from worktrees_hives.contract import (
    ErrorResponse,
    Response,
    SuccessResponse,
    WorktreeCreateRequest,
    parse_verified_worktree_creation,
)
from worktrees_hives.errors import (
    WhContractVersionError,
    WhProcessError,
    WhSchemaError,
)

FAKE_V1_SUCCESS_JSON = json.dumps(
    {
        "ok": True,
        "schema_version": 1,
        "command": "cli.bootstrap",
        "data": {},
        "error": None,
    }
)


class TestV2EnvelopeAcceptance:
    def test_valid_v2_success_envelope(self):
        resp = Response.from_dict(
            {
                "ok": True,
                "schema_version": 2,
                "command": "cli.bootstrap",
                "data": {},
                "error": None,
            }
        )
        assert resp.schema_version == 2


class TestVerifiedWorktreeCreation:
    def test_requires_exact_path_branch_ref_and_registration(self):
        creation = parse_verified_worktree_creation(
            {
                "path": "/tmp/worktrees/acme/repo/job",
                "branch": "feature/job",
                "branch_ref": "refs/heads/feature/job",
                "start_commit": "a" * 40,
                "head_commit": "a" * 40,
                "worktree_registered": True,
            },
            requested_start_point="a" * 40,
            expected_path="/tmp/worktrees/acme/repo/job",
            expected_branch="feature/job",
        )

        assert creation.path == "/tmp/worktrees/acme/repo/job"
        assert creation.branch == "feature/job"
        assert creation.branch_ref == "refs/heads/feature/job"
        assert creation.worktree_registered is True

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("path", None),
            ("branch", None),
            ("branch_ref", None),
            ("worktree_registered", False),
        ],
    )
    def test_rejects_missing_or_unproven_registration_identity(self, field, value):
        data = {
            "path": "/tmp/worktrees/acme/repo/job",
            "branch": "feature/job",
            "branch_ref": "refs/heads/feature/job",
            "start_commit": "a" * 40,
            "head_commit": "a" * 40,
            "worktree_registered": True,
        }
        data[field] = value

        with pytest.raises(WhSchemaError):
            parse_verified_worktree_creation(
                data,
                requested_start_point="a" * 40,
                expected_path="/tmp/worktrees/acme/repo/job",
                expected_branch="feature/job",
            )


class TestSchemaNegotiation:
    @patch("worktrees_hives.bridge.subprocess.run")
    @patch("worktrees_hives.bridge._resolve_wh_binary", return_value="/usr/bin/wh")
    def test_v2_request_to_v1_binary_raises_machine_classifiable_version_error(
        self, mock_resolve, mock_run
    ):
        mock_run.return_value = MagicMock(
            returncode=2,
            stdout="",
            stderr="error: unexpected argument '--schema-version' found",
        )

        with pytest.raises(WhContractVersionError) as exc_info:
            WhClient().run(
                "worktree",
                "create",
                "--schema-version",
                "2",
                "--start-point",
                "origin/main",
            )

        assert exc_info.value.code == "CONTRACT_VERSION_UNSUPPORTED"
        assert exc_info.value.requested_schema_version == 2

    @patch("worktrees_hives.bridge.subprocess.run")
    @patch("worktrees_hives.bridge._resolve_wh_binary", return_value="/usr/bin/wh")
    def test_v2_request_rejects_v1_success_envelope(self, mock_resolve, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=FAKE_V1_SUCCESS_JSON,
            stderr="",
        )

        with pytest.raises(WhContractVersionError) as exc_info:
            WhClient().run("worktree", "create", "--schema-version", "2")

        assert exc_info.value.requested_schema_version == 2

    @patch("worktrees_hives.bridge.subprocess.run")
    @patch("worktrees_hives.bridge._resolve_wh_binary", return_value="/usr/bin/wh")
    def test_worktree_create_equals_selector_is_version_checked(self, mock_resolve, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=FAKE_V1_SUCCESS_JSON,
            stderr="",
        )

        with pytest.raises(WhContractVersionError) as exc_info:
            WhClient().run("worktree", "create", "--schema-version=2")

        assert exc_info.value.requested_schema_version == 2

    @pytest.mark.parametrize(
        "args",
        [
            ("supervisor", "run", "tool", "--schema-version", "2"),
            ("git-safe", "show", "--schema-version", "2"),
            ("gh-safe", "pr", "view", "--schema-version=2"),
        ],
    )
    @patch("worktrees_hives.bridge.subprocess.run")
    @patch("worktrees_hives.bridge._resolve_wh_binary", return_value="/usr/bin/wh")
    def test_unrelated_child_schema_selector_does_not_change_boundary_version(
        self, mock_resolve, mock_run, args
    ):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=FAKE_V1_SUCCESS_JSON,
            stderr="",
        )

        result = WhClient().run(*args)

        assert isinstance(result, SuccessResponse)
        assert result.schema_version == 1

    @patch("worktrees_hives.bridge.subprocess.run")
    @patch("worktrees_hives.bridge._resolve_wh_binary", return_value="/usr/bin/wh")
    def test_v2_clap_error_unrelated_to_version_remains_process_error(self, mock_resolve, mock_run):
        mock_run.return_value = MagicMock(
            returncode=2,
            stdout="",
            stderr="error: the following required arguments were not provided: --repo",
        )

        with pytest.raises(WhProcessError) as exc_info:
            WhClient().run("worktree", "create", "--schema-version", "2")

        assert not isinstance(exc_info.value, WhContractVersionError)

    @patch("worktrees_hives.bridge.subprocess.run")
    @patch("worktrees_hives.bridge._resolve_wh_binary", return_value="/usr/bin/wh")
    def test_schema_selector_after_double_dash_does_not_select_boundary(
        self, mock_resolve, mock_run
    ):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=FAKE_V1_SUCCESS_JSON,
            stderr="",
        )

        result = WhClient().run(
            "worktree",
            "create",
            "--",
            "--schema-version",
            "2",
        )

        assert isinstance(result, SuccessResponse)
        assert result.schema_version == 1

    def test_worktree_create_request_selects_v2_boundary(self):
        request = WorktreeCreateRequest(
            owner="acme",
            repo="sample",
            job_id="gh-1",
            branch="feature/gh-1",
            start_point="origin/main",
        )

        assert request.cli_args("/repo") == (
            "worktree",
            "create",
            "--schema-version",
            "2",
            "--repo",
            "/repo",
            "--start-point",
            "origin/main",
            "acme",
            "sample",
            "gh-1",
            "feature/gh-1",
        )


class TestResidualPolicyData:
    @patch("worktrees_hives.bridge.subprocess.run")
    @patch("worktrees_hives.bridge._resolve_wh_binary", return_value="/usr/bin/wh")
    def test_exit_1_postcondition_error_preserves_residual_data(self, mock_resolve, mock_run):
        envelope = json.dumps(
            {
                "ok": False,
                "schema_version": 1,
                "command": "worktree.create",
                "data": {"path": "/tmp/residual", "branch": "hive/gh-1"},
                "error": {
                    "code": "WORKTREE_POSTCONDITION_FAILED",
                    "message": "created worktree identity could not be verified",
                },
            }
        )
        mock_run.return_value = MagicMock(returncode=1, stdout=envelope, stderr="")
        result = WhClient().run("worktree", "create")
        assert isinstance(result, ErrorResponse)
        assert result.error.code == "WORKTREE_POSTCONDITION_FAILED"
        assert result.data == {
            "path": "/tmp/residual",
            "branch": "hive/gh-1",
        }


class TestRequestedSchemaVersion:
    """Direct coverage of schema-flag parsing helpers extracted from `_interpret`."""

    def test_space_and_equals_selectors(self) -> None:
        assert _requested_schema_version(("worktree", "create", "--schema-version", "2")) == 2
        assert _requested_schema_version(("worktree", "create", "--schema-version=2")) == 2

    def test_unrelated_commands_and_terminator_are_ignored(self) -> None:
        assert _requested_schema_version(("supervisor", "run", "--schema-version", "2")) is None
        assert _requested_schema_version(("git-safe", "show", "--schema-version", "2")) is None
        assert _requested_schema_version(("gh-safe", "pr", "view", "--schema-version=2")) is None
        assert (
            _requested_schema_version(("worktree", "create", "--", "--schema-version", "2")) is None
        )

    def test_invalid_or_missing_values_are_none(self) -> None:
        assert _requested_schema_version(("worktree", "create", "--schema-version")) is None
        assert _requested_schema_version(("worktree", "create", "--schema-version", "x")) is None
        assert _requested_schema_version(("worktree", "create", "--schema-version=")) is None

    @pytest.mark.parametrize(
        "stderr",
        [
            "error: unexpected argument '--schema-version' found",
            "unknown argument: --schema-version",
            "unrecognized option '--schema-version'",
        ],
    )
    def test_legacy_clap_markers(self, stderr: str) -> None:
        assert _schema_selector_is_unsupported(stderr)
        assert not _schema_selector_is_unsupported(
            "error: the following required arguments were not provided: --repo"
        )
