//! Shared error definitions.

use std::fmt::{Display, Formatter};
use std::io;
use std::path::PathBuf;

/// Result alias for core operations.
pub type Result<T> = std::result::Result<T, Error>;

/// Residual state captured after a failed atomic worktree-add transaction.
#[derive(Debug)]
pub struct WorktreeCreationFailure {
    pub path: PathBuf,
    pub branch: String,
    pub path_exists: bool,
    pub branch_commit: Option<String>,
    pub head_commit: Option<String>,
    pub worktree_registered: bool,
    pub stderr: String,
}

/// Exact-identity postcondition failure and the residual state left in place.
#[derive(Debug)]
pub struct WorktreePostconditionFailure {
    pub path: PathBuf,
    pub branch: String,
    pub expected_commit: String,
    pub actual_branch: Option<String>,
    pub path_exists: bool,
    pub branch_commit: Option<String>,
    pub head_commit: Option<String>,
    pub worktree_registered: bool,
    pub reason: String,
}

/// Errors returned by core primitives.
#[derive(Debug)]
pub enum Error {
    /// A path segment was invalid for sandbox derivation.
    InvalidSegment { field: &'static str, value: String },
    /// A candidate path escaped or violated sandbox rules.
    SandboxViolation {
        base: PathBuf,
        candidate: PathBuf,
        reason: &'static str,
    },
    /// A filesystem operation failed.
    Io {
        context: &'static str,
        source: io::Error,
    },
    /// A git subprocess command failed.
    GitCommand { args: Vec<String>, stderr: String },
    /// A worktree create transaction failed and may have left residual state.
    WorktreeCreationFailed(Box<WorktreeCreationFailure>),
    /// Creation completed but its exact branch/ref/HEAD identity was not preserved.
    WorktreePostconditionFailed(Box<WorktreePostconditionFailure>),
    /// A git or gh command was blocked by safety policy.
    PolicyViolation {
        /// Machine-readable policy error code.
        code: PolicyCode,
        /// Human-readable explanation.
        message: String,
    },
}

/// Machine-readable error codes for policy violations.
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum PolicyCode {
    /// The git subcommand is not on the allowlist.
    SubcommandNotAllowed,
    /// Bare `--force` or `-f` was used without `--force-with-lease`.
    BareForcePush,
    /// A merge subcommand or `gh pr merge` was attempted.
    MergeBlocked,
    /// The current branch does not match the expected job branch.
    BranchMismatch,
    /// The git directory could not be resolved.
    GitDirUnavailable,
    /// The gh subcommand is not on the allowlist.
    GhSubcommandNotAllowed,
    /// A gh subcommand flag is not permitted.
    GhFlagNotAllowed,
    /// A path is outside the allowed sandbox (e.g. supervised --repo).
    PathNotAllowed,
    /// An existing worktree branch lacks a durable identity proving safe resume ownership.
    WorktreeResumeUnproven,
}

impl PolicyCode {
    /// Stable string representation for JSON error envelopes.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::SubcommandNotAllowed => "SUBCOMMAND_NOT_ALLOWED",
            Self::BareForcePush => "BARE_FORCE_PUSH",
            Self::MergeBlocked => "MERGE_BLOCKED",
            Self::BranchMismatch => "BRANCH_MISMATCH",
            Self::GitDirUnavailable => "GIT_DIR_UNAVAILABLE",
            Self::GhSubcommandNotAllowed => "GH_SUBCOMMAND_NOT_ALLOWED",
            Self::GhFlagNotAllowed => "GH_FLAG_NOT_ALLOWED",
            Self::PathNotAllowed => "PATH_NOT_ALLOWED",
            Self::WorktreeResumeUnproven => "WORKTREE_RESUME_UNPROVEN",
        }
    }
}

impl Display for PolicyCode {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

impl Display for Error {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidSegment { field, value } => {
                write!(f, "invalid {field} segment: `{value}`")
            }
            Self::SandboxViolation {
                base,
                candidate,
                reason,
            } => write!(
                f,
                "sandbox violation for `{}` under `{}`: {reason}",
                candidate.display(),
                base.display()
            ),
            Self::Io { context, source } => write!(f, "{context}: {source}"),
            Self::GitCommand { args, stderr } => {
                write!(
                    f,
                    "git command failed (`git {}`): {}",
                    args.join(" "),
                    stderr.trim()
                )
            }
            Self::WorktreeCreationFailed(failure) => {
                let WorktreeCreationFailure {
                    path,
                    branch,
                    path_exists,
                    branch_commit,
                    head_commit,
                    worktree_registered,
                    stderr,
                } = failure.as_ref();
                write!(
                    f,
                    "worktree creation failed for branch `{branch}` at `{}`: {}; residual_state \
                 path_exists={} registered={} branch_commit={} head_commit={}; automatic cleanup \
                 skipped because concurrent adoption cannot be disproven",
                    path.display(),
                    stderr.trim(),
                    path_exists,
                    worktree_registered,
                    branch_commit.as_deref().unwrap_or("<absent>"),
                    head_commit.as_deref().unwrap_or("<absent>")
                )
            }
            Self::WorktreePostconditionFailed(failure) => {
                let WorktreePostconditionFailure {
                    path,
                    branch,
                    expected_commit,
                    actual_branch,
                    path_exists,
                    branch_commit,
                    head_commit,
                    worktree_registered,
                    reason,
                } = failure.as_ref();
                write!(
                    f,
                    "worktree postcondition failed for branch `{branch}` at `{}`: expected_commit={} \
                 actual_branch={} reason={reason}; residual_state path_exists={} registered={} \
                 branch_commit={} head_commit={}; automatic cleanup skipped because concurrent \
                 adoption cannot be disproven",
                    path.display(),
                    expected_commit,
                    actual_branch.as_deref().unwrap_or("<unavailable>"),
                    path_exists,
                    worktree_registered,
                    branch_commit.as_deref().unwrap_or("<absent>"),
                    head_commit.as_deref().unwrap_or("<absent>")
                )
            }
            Self::PolicyViolation { code, message } => {
                write!(f, "policy violation [{code}]: {message}")
            }
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io { source, .. } => Some(source),
            _ => None,
        }
    }
}

impl Error {
    /// Stable error code for JSON command envelopes.
    #[must_use]
    pub const fn code(&self) -> &'static str {
        match self {
            Self::InvalidSegment { .. } => "INVALID_SEGMENT",
            Self::SandboxViolation { .. } => "SANDBOX_VIOLATION",
            Self::Io { .. } => "IO_ERROR",
            Self::GitCommand { .. } => "GIT_COMMAND_FAILED",
            Self::WorktreeCreationFailed(_) => "WORKTREE_CREATE_FAILED",
            Self::WorktreePostconditionFailed(_) => "WORKTREE_POSTCONDITION_FAILED",
            Self::PolicyViolation { code, .. } => code.as_str(),
        }
    }

    /// Process exit code used by the CLI boundary.
    #[must_use]
    pub const fn exit_code(&self) -> u8 {
        match self {
            Self::PolicyViolation { .. } | Self::WorktreePostconditionFailed(_) => 2,
            _ => 1,
        }
    }
}

impl From<io::Error> for Error {
    fn from(source: io::Error) -> Self {
        Self::Io {
            context: "io operation",
            source,
        }
    }
}
