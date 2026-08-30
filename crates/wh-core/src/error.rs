//! Shared error definitions.

use std::fmt::{Display, Formatter};
use std::io;
use std::path::PathBuf;

/// Result alias for core operations.
pub type Result<T> = std::result::Result<T, Error>;

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

impl From<io::Error> for Error {
    fn from(source: io::Error) -> Self {
        Self::Io {
            context: "io operation",
            source,
        }
    }
}
