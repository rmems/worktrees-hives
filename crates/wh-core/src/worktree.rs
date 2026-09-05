use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::error::{
    Error, PolicyCode, Result, WorktreeCreationFailure, WorktreePostconditionFailure,
};
use crate::paths::{canonicalize_for_tools, derive_worktree_path, worktree_base_path};

/// Result of a worktree creation operation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Worktree {
    /// Absolute path to the created worktree.
    pub path: PathBuf,
    /// The branch name associated with this worktree.
    pub branch: String,
    /// The repository root this worktree is linked to.
    pub repo_root: PathBuf,
    /// Fully resolved start commit for a creation result; absent from discovery-only listings.
    pub start_commit: Option<String>,
    /// Independently verified worker HEAD for a creation result; absent from listings.
    pub head_commit: Option<String>,
}

/// Inputs required to create one isolated worktree at an explicit start point.
#[derive(Debug, Clone, Copy)]
pub struct WorktreeCreateRequest<'a> {
    pub repo_root: &'a Path,
    pub owner: &'a str,
    pub repo: &'a str,
    pub job_id: &'a str,
    pub branch: &'a str,
    pub start_point: &'a str,
}

/// Manages isolated git worktrees for hive jobs.
#[derive(Debug, Default)]
pub struct WorktreeManager {
    base_path: Option<PathBuf>,
}

impl WorktreeManager {
    /// Create a new manager using the default base path.
    pub fn new() -> Result<Self> {
        let base = worktree_base_path()?;
        Self::with_base(base)
    }

    /// Create a new manager with an explicit base path (for testing or overrides).
    ///
    /// The base is created if missing and stored in canonical form so OS path
    /// aliases (e.g. macOS `/var` → `/private/var`) do not trip sandbox checks.
    pub fn with_base(base: PathBuf) -> Result<Self> {
        fs::create_dir_all(&base).map_err(|e| Error::Io {
            context: "create worktree base directory",
            source: e,
        })?;
        // Canonicalize for OS aliases (macOS /var → /private/var) but strip
        // Windows `\\?\` so `git worktree add` accepts the path.
        let base = canonicalize_for_tools(&base).map_err(|e| Error::Io {
            context: "canonicalize worktree base directory",
            source: e,
        })?;
        Ok(Self {
            base_path: Some(base),
        })
    }

    /// Get the base path this manager uses.
    pub fn base_path(&self) -> Result<&Path> {
        self.base_path.as_deref().ok_or_else(|| Error::Io {
            context: "worktree base path not initialized",
            source: std::io::Error::new(std::io::ErrorKind::NotFound, "base path not set"),
        })
    }

    /// Create a new worktree for the given job.
    ///
    /// The worktree path will be: `{base}/{owner}/{repo}/{job_id}`.
    /// `start_point` is required and is resolved to a commit before any branch
    /// mutation. Existing branches are rejected because the exact-base contract
    /// does not define a durable resume identity for an existing ref.
    pub fn create(
        &self,
        repo_root: &Path,
        owner: &str,
        repo: &str,
        job_id: &str,
        branch: &str,
        start_point: &str,
    ) -> Result<Worktree> {
        self.create_with_request(WorktreeCreateRequest {
            repo_root,
            owner,
            repo,
            job_id,
            branch,
            start_point,
        })
    }

    /// Create a worktree from a typed request used by CLI and orchestration adapters.
    pub fn create_with_request(&self, request: WorktreeCreateRequest<'_>) -> Result<Worktree> {
        validate_worktree_branch(request.branch)?;
        let base = self.base_path()?;
        validate_repo_root(request.repo_root)?;

        // Resolve the caller-selected start point before any mutation. Appending
        // ^{commit} rejects trees/blobs and peels annotated tags to commits.
        let start_commit = resolve_start_commit(request.repo_root, request.start_point)?;
        let worktree_path = prepare_worktree_path(base, &request)?;
        reject_unproven_resume(&request, &start_commit)?;
        add_worktree(&request, &worktree_path, &start_commit)?;

        let head_commit = verify_creation_postconditions(CreationPostconditions {
            repo_root: request.repo_root,
            worktree_path: &worktree_path,
            expected_branch: request.branch,
            expected_commit: &start_commit,
        })?;

        Ok(Worktree {
            path: worktree_path,
            branch: request.branch.to_owned(),
            repo_root: request.repo_root.to_path_buf(),
            start_commit: Some(start_commit),
            head_commit: Some(head_commit),
        })
    }

    /// List all hive worktrees under the base path.
    pub fn list(&self) -> Result<Vec<Worktree>> {
        let base = self.base_path()?;
        let mut worktrees = Vec::new();

        if !base.exists() {
            return Ok(worktrees);
        }

        // Walk the base directory: {base}/{owner}/{repo}/{job_id}
        for owner_entry in fs::read_dir(base).map_err(|e| Error::Io {
            context: "read worktree base directory",
            source: e,
        })? {
            let owner_entry = owner_entry.map_err(|e| Error::Io {
                context: "read owner entry",
                source: e,
            })?;
            if !owner_entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                continue;
            }

            for repo_entry in fs::read_dir(owner_entry.path()).map_err(|e| Error::Io {
                context: "read repo directory",
                source: e,
            })? {
                let repo_entry = repo_entry.map_err(|e| Error::Io {
                    context: "read repo entry",
                    source: e,
                })?;
                if !repo_entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                    continue;
                }

                for job_entry in fs::read_dir(repo_entry.path()).map_err(|e| Error::Io {
                    context: "read job directory",
                    source: e,
                })? {
                    let job_entry = job_entry.map_err(|e| Error::Io {
                        context: "read job entry",
                        source: e,
                    })?;
                    if !job_entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                        continue;
                    }

                    // Try to get the branch name from the worktree
                    let branch = get_worktree_branch(&job_entry.path())
                        .unwrap_or_else(|_| "unknown".to_string());

                    worktrees.push(Worktree {
                        path: job_entry.path(),
                        branch,
                        repo_root: PathBuf::new(), // Not tracked in list
                        start_commit: None,        // Not tracked in list
                        head_commit: None,         // Not tracked in list
                    });
                }
            }
        }

        Ok(worktrees)
    }

    /// Remove a worktree by its path.
    ///
    /// If `force` is true, the worktree is removed even if it has uncommitted changes.
    /// The associated branch is NOT deleted by default.
    pub fn remove(&self, worktree_path: &Path, force: bool) -> Result<()> {
        // Verify the path is within our sandbox
        let base = self.base_path()?;
        if !is_within_base(worktree_path, base)? {
            return Err(Error::SandboxViolation {
                base: base.to_path_buf(),
                candidate: worktree_path.to_path_buf(),
                reason: "worktree path is outside configured base",
            });
        }

        // Find the repo root for this worktree
        let repo_root = find_repo_root_for_worktree(worktree_path)?;

        let mut args = vec!["worktree".into(), "remove".into()];
        if force {
            args.push("--force".into());
        }
        args.push("--".into());
        args.push(worktree_path.to_string_lossy().to_string());

        let output = Command::new("git")
            .arg("-C")
            .arg(&repo_root)
            .args(&args)
            .output()
            .map_err(|e| Error::Io {
                context: "spawn git worktree remove",
                source: e,
            })?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).to_string();
            return Err(Error::GitCommand {
                args: args.iter().map(|s| s.to_string()).collect(),
                stderr,
            });
        }

        // Also clean up empty parent directories
        cleanup_empty_parents(worktree_path, base);

        Ok(())
    }

    /// Prune worktree administrative files (stale entries).
    pub fn prune(&self, repo_root: &Path) -> Result<()> {
        let output = Command::new("git")
            .arg("-C")
            .arg(repo_root)
            .arg("worktree")
            .arg("prune")
            .output()
            .map_err(|e| Error::Io {
                context: "spawn git worktree prune",
                source: e,
            })?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).to_string();
            return Err(Error::GitCommand {
                args: vec!["worktree".into(), "prune".into()],
                stderr,
            });
        }

        Ok(())
    }
}

fn validate_worktree_branch(branch: &str) -> Result<()> {
    match branch.chars().next() {
        None | Some('-') => Err(Error::GitCommand {
            args: vec!["worktree".into(), "add".into()],
            stderr: format!("invalid branch name (empty or option-looking): {branch:?}"),
        }),
        Some(_) => Ok(()),
    }
}

fn prepare_worktree_path(base: &Path, request: &WorktreeCreateRequest<'_>) -> Result<PathBuf> {
    let worktree_path = derive_worktree_path(base, request.owner, request.repo, request.job_id)?;

    // Reject symlink segments beneath the canonical base before and after
    // creating parents so a planted link is never followed.
    reject_symlink_components_under(base, &worktree_path)?;
    if let Some(parent) = worktree_path.parent() {
        fs::create_dir_all(parent).map_err(|e| Error::Io {
            context: "create worktree parent directories",
            source: e,
        })?;
        reject_symlink_components_under(base, parent)?;
    }
    Ok(worktree_path)
}

fn validate_repo_root(repo_root: &Path) -> Result<()> {
    if repo_root.join(".git").exists() {
        return Ok(());
    }
    if is_bare_repo(repo_root)? {
        return Ok(());
    }
    Err(Error::GitCommand {
        args: vec!["worktree".into(), "add".into()],
        stderr: format!("not a git repository: {}", repo_root.display()),
    })
}

fn reject_unproven_resume(request: &WorktreeCreateRequest<'_>, start_commit: &str) -> Result<()> {
    if branch_exists_in_repo(request.repo_root, request.branch)? {
        return Err(Error::PolicyViolation {
            code: PolicyCode::WorktreeResumeUnproven,
            message: format!(
                "refusing to reuse existing branch {:?} at requested commit \
                 {start_commit}: safe resume identity is not proven",
                request.branch
            ),
        });
    }
    Ok(())
}

fn add_worktree(
    request: &WorktreeCreateRequest<'_>,
    worktree_path: &Path,
    start_commit: &str,
) -> Result<()> {
    // Create the branch and linked worktree in one operation. If a concurrent
    // actor creates the ref first, Git fails rather than attaching to it.
    let output = Command::new("git")
        .arg("-C")
        .arg(request.repo_root)
        .arg("worktree")
        .arg("add")
        .arg("-b")
        .arg(request.branch)
        .arg("--")
        .arg(worktree_path)
        .arg(start_commit)
        .output()
        .map_err(|e| Error::Io {
            context: "spawn git worktree add",
            source: e,
        })?;

    if output.status.success() {
        return Ok(());
    }
    let residual = inspect_residual_state(request.repo_root, worktree_path, request.branch);
    Err(Error::WorktreeCreationFailed(Box::new(
        WorktreeCreationFailure {
            path: worktree_path.to_path_buf(),
            branch: request.branch.to_owned(),
            path_exists: residual.path_exists,
            branch_commit: residual.branch_commit,
            head_commit: residual.head_commit,
            worktree_registered: residual.worktree_registered,
            stderr: String::from_utf8_lossy(&output.stderr).to_string(),
        },
    )))
}

/// Check if a path is a bare git repository.
fn is_bare_repo(path: &Path) -> Result<bool> {
    let output = Command::new("git")
        .arg("-C")
        .arg(path)
        .arg("rev-parse")
        .arg("--is-bare-repository")
        .output()
        .map_err(|e| Error::Io {
            context: "check bare repository",
            source: e,
        })?;

    Ok(output.status.success() && String::from_utf8_lossy(&output.stdout).trim() == "true")
}

/// Check if a branch exists in the repository.
fn branch_exists_in_repo(repo_root: &Path, branch: &str) -> Result<bool> {
    let output = Command::new("git")
        .arg("-C")
        .arg(repo_root)
        .arg("show-ref")
        .arg("--verify")
        .arg("--quiet")
        .arg(format!("refs/heads/{branch}"))
        .output()
        .map_err(|e| Error::Io {
            context: "check branch existence",
            source: e,
        })?;

    match output.status.code() {
        Some(0) => Ok(true),
        Some(1) => Ok(false),
        _ => Err(Error::GitCommand {
            args: vec![
                "show-ref".into(),
                "--verify".into(),
                "--quiet".into(),
                format!("refs/heads/{branch}"),
            ],
            stderr: String::from_utf8_lossy(&output.stderr).to_string(),
        }),
    }
}

/// Leading all-hex object-id text when it is the entire start point or is
/// immediately followed by a commit-ish decoration (`~`, `^`, `@{`).
///
/// This closes abbreviated-OID smuggling such as `<abbrev>~0` while leaving
/// symbolic refs (`refs/heads/x`, `develop~1`, tags) untouched.
fn leading_hex_oid_prefix(start_point: &str) -> Option<&str> {
    let hex_len = start_point
        .bytes()
        .take_while(u8::is_ascii_hexdigit)
        .count();
    if hex_len == 0 {
        return None;
    }
    let rest = &start_point[hex_len..];
    if rest.is_empty() || is_commitish_decoration(rest) {
        Some(&start_point[..hex_len])
    } else {
        None
    }
}

fn is_commitish_decoration(rest: &str) -> bool {
    rest.starts_with('~') || rest.starts_with('^') || rest.starts_with("@{")
}

fn reject_non_full_hex_oid(hex_prefix: &str) -> Result<()> {
    if matches!(hex_prefix.len(), 40 | 64) {
        return Ok(());
    }
    Err(Error::GitCommand {
        args: vec!["rev-parse".into(), "--verify".into()],
        stderr: "all-hex start point must be a full 40- or 64-character object id".into(),
    })
}

fn reject_hex_oid_mismatch(start_point: &str, hex_prefix: &str, commit: &str) -> Result<()> {
    if hex_prefix.len() == commit.len() && commit.eq_ignore_ascii_case(hex_prefix) {
        return Ok(());
    }
    Err(Error::GitCommand {
        args: vec!["rev-parse".into(), "--verify".into()],
        stderr: format!(
            "all-hex start point must equal the full canonical object id; requested \
             {start_point:?}, resolved {commit:?}"
        ),
    })
}

/// Resolve a caller-supplied commit-ish to one exact commit object.
fn resolve_start_commit(repo_root: &Path, start_point: &str) -> Result<String> {
    reject_empty_selector(start_point, "start point must not be empty")?;
    enforce_leading_hex_oid(start_point, None)?;
    let commit = peel_to_commit(repo_root, start_point)?;
    reject_empty_selector(
        &commit,
        format!("start point {start_point:?} resolved to an empty commit id"),
    )?;
    enforce_leading_hex_oid(start_point, Some(&commit))?;
    Ok(commit)
}

fn reject_empty_selector(value: &str, stderr: impl Into<String>) -> Result<()> {
    if value.is_empty() {
        return Err(Error::GitCommand {
            args: vec!["rev-parse".into(), "--verify".into()],
            stderr: stderr.into(),
        });
    }
    Ok(())
}

fn enforce_leading_hex_oid(start_point: &str, resolved: Option<&str>) -> Result<()> {
    let Some(hex_prefix) = leading_hex_oid_prefix(start_point) else {
        return Ok(());
    };
    reject_non_full_hex_oid(hex_prefix)?;
    match resolved {
        Some(commit) => reject_hex_oid_mismatch(start_point, hex_prefix, commit),
        None => Ok(()),
    }
}

fn peel_to_commit(repo_root: &Path, start_point: &str) -> Result<String> {
    let commitish = format!("{start_point}^{{commit}}");
    let output = Command::new("git")
        .arg("-C")
        .arg(repo_root)
        .arg("rev-parse")
        .arg("--verify")
        .arg("--end-of-options")
        .arg(&commitish)
        .output()
        .map_err(|e| Error::Io {
            context: "resolve worktree start point",
            source: e,
        })?;

    if output.status.success() {
        return Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned());
    }
    Err(Error::GitCommand {
        args: vec![
            "rev-parse".into(),
            "--verify".into(),
            "--end-of-options".into(),
            commitish,
        ],
        stderr: String::from_utf8_lossy(&output.stderr).to_string(),
    })
}

struct CreationPostconditions<'a> {
    repo_root: &'a Path,
    worktree_path: &'a Path,
    expected_branch: &'a str,
    expected_commit: &'a str,
}

impl CreationPostconditions<'_> {
    fn actual_branch_ref(&self) -> Result<String> {
        git_stdout(
            self.worktree_path,
            &["symbolic-ref", "--quiet", "HEAD"],
            "verify created worktree branch",
        )
        .map_err(|error| self.failure(None, &error.to_string()))
    }

    fn branch_commit(&self, actual_branch: &str) -> Result<String> {
        git_stdout(
            self.repo_root,
            &[
                "rev-parse",
                "--verify",
                "--end-of-options",
                &format!("refs/heads/{}^{{commit}}", self.expected_branch),
            ],
            "verify created branch commit",
        )
        .map_err(|error| self.failure(Some(actual_branch.to_owned()), &error.to_string()))
    }

    fn head_commit(&self, actual_branch: &str) -> Result<String> {
        git_stdout(
            self.worktree_path,
            &["rev-parse", "--verify", "HEAD^{commit}"],
            "verify created worktree HEAD",
        )
        .map_err(|error| self.failure(Some(actual_branch.to_owned()), &error.to_string()))
    }

    fn verify_identity(
        &self,
        actual_branch_ref: &str,
        branch_commit: &str,
        head_commit: &str,
    ) -> Result<()> {
        let expected_branch_ref = format!("refs/heads/{}", self.expected_branch);
        let actual = [actual_branch_ref, branch_commit, head_commit];
        let expected = [
            expected_branch_ref.as_str(),
            self.expected_commit,
            self.expected_commit,
        ];
        if actual != expected {
            return Err(self.failure(
                Some(actual_branch_ref.to_owned()),
                &format!(
                    "identity mismatch: actual branch ref={actual_branch_ref:?} \
                     branch_commit={branch_commit}, head_commit={head_commit}"
                ),
            ));
        }
        Ok(())
    }

    fn verify_registration(&self, actual_branch_ref: &str, head_commit: &str) -> Result<()> {
        let listing = git_stdout(
            self.repo_root,
            &["worktree", "list", "--porcelain"],
            "verify created worktree registration",
        )
        .map_err(|error| self.failure(Some(actual_branch_ref.to_owned()), &error.to_string()))?;
        if !worktree_registration_matches(
            &listing,
            self.worktree_path,
            actual_branch_ref,
            head_commit,
        ) {
            return Err(self.failure(
                Some(actual_branch_ref.to_owned()),
                "worktree registration does not match the expected path, branch, and HEAD",
            ));
        }
        Ok(())
    }

    fn failure(&self, actual_branch: Option<String>, cause: &str) -> Error {
        let residual =
            inspect_residual_state(self.repo_root, self.worktree_path, self.expected_branch);
        Error::WorktreePostconditionFailed(Box::new(WorktreePostconditionFailure {
            path: self.worktree_path.to_path_buf(),
            branch: self.expected_branch.to_owned(),
            expected_commit: self.expected_commit.to_owned(),
            actual_branch,
            path_exists: residual.path_exists,
            branch_commit: residual.branch_commit,
            head_commit: residual.head_commit,
            worktree_registered: residual.worktree_registered,
            reason: cause.to_owned(),
        }))
    }
}

fn verify_creation_postconditions(postconditions: CreationPostconditions<'_>) -> Result<String> {
    let actual_branch_ref = postconditions.actual_branch_ref()?;
    let branch_commit = postconditions.branch_commit(&actual_branch_ref)?;
    let head_commit = postconditions.head_commit(&actual_branch_ref)?;
    postconditions.verify_identity(&actual_branch_ref, &branch_commit, &head_commit)?;
    postconditions.verify_registration(&actual_branch_ref, &head_commit)?;
    Ok(head_commit)
}

fn worktree_registration_matches(
    listing: &str,
    expected_path: &Path,
    expected_branch_ref: &str,
    expected_head: &str,
) -> bool {
    listing.split("\n\n").any(|entry| {
        let mut path = None;
        let mut branch = None;
        let mut head = None;
        for line in entry.lines() {
            if let Some(value) = line.strip_prefix("worktree ") {
                path = Some(Path::new(value));
            } else if let Some(value) = line.strip_prefix("branch ") {
                branch = Some(value);
            } else if let Some(value) = line.strip_prefix("HEAD ") {
                head = Some(value);
            }
        }
        path == Some(expected_path)
            && branch == Some(expected_branch_ref)
            && head == Some(expected_head)
    })
}

fn git_stdout(repo: &Path, args: &[&str], context: &'static str) -> Result<String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(args)
        .output()
        .map_err(|e| Error::Io { context, source: e })?;
    if !output.status.success() {
        return Err(Error::GitCommand {
            args: args.iter().map(|arg| (*arg).to_owned()).collect(),
            stderr: String::from_utf8_lossy(&output.stderr).to_string(),
        });
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

fn optional_git_stdout(repo: &Path, args: &[&str]) -> Option<String> {
    Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(args)
        .output()
        .ok()
        .filter(|output| output.status.success())
        .map(|output| String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

struct ResidualState {
    path_exists: bool,
    branch_commit: Option<String>,
    head_commit: Option<String>,
    worktree_registered: bool,
}

fn inspect_residual_state(repo_root: &Path, worktree_path: &Path, branch: &str) -> ResidualState {
    let branch_commit = optional_git_stdout(
        repo_root,
        &[
            "rev-parse",
            "--verify",
            &format!("refs/heads/{branch}^{{commit}}"),
        ],
    );
    let head_commit =
        optional_git_stdout(worktree_path, &["rev-parse", "--verify", "HEAD^{commit}"]);
    let worktree_registered = optional_git_stdout(repo_root, &["worktree", "list", "--porcelain"])
        .is_some_and(|listing| {
            listing.lines().any(|line| {
                line.strip_prefix("worktree ")
                    .is_some_and(|path| Path::new(path) == worktree_path)
            })
        });
    ResidualState {
        path_exists: worktree_path.exists(),
        branch_commit,
        head_commit,
        worktree_registered,
    }
}

/// Get the branch name associated with a worktree.
fn get_worktree_branch(worktree_path: &Path) -> Result<String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(worktree_path)
        .arg("rev-parse")
        .arg("--abbrev-ref")
        .arg("HEAD")
        .output()
        .map_err(|e| Error::Io {
            context: "get worktree branch",
            source: e,
        })?;

    if !output.status.success() {
        return Err(Error::GitCommand {
            args: vec!["rev-parse".into(), "--abbrev-ref".into(), "HEAD".into()],
            stderr: String::from_utf8_lossy(&output.stderr).to_string(),
        });
    }

    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

/// Find the repository root for a given worktree path.
fn find_repo_root_for_worktree(worktree_path: &Path) -> Result<PathBuf> {
    let output = Command::new("git")
        .arg("-C")
        .arg(worktree_path)
        .arg("rev-parse")
        .arg("--git-common-dir")
        .output()
        .map_err(|e| Error::Io {
            context: "find repo root for worktree",
            source: e,
        })?;

    if !output.status.success() {
        return Err(Error::GitCommand {
            args: vec!["rev-parse".into(), "--git-common-dir".into()],
            stderr: String::from_utf8_lossy(&output.stderr).to_string(),
        });
    }

    let git_dir = String::from_utf8_lossy(&output.stdout).into_owned();
    let git_dir_path = PathBuf::from(git_dir.trim());

    // For worktrees, the common dir is the main repo's .git directory.
    // The repo root is the parent of .git (never panic on malformed paths).
    if git_dir_path.ends_with(".git") {
        git_dir_path
            .parent()
            .map(|p| p.to_path_buf())
            .ok_or_else(|| Error::GitCommand {
                args: vec!["rev-parse".into(), "--git-common-dir".into()],
                stderr: format!("invalid git directory path: {}", git_dir_path.display()),
            })
    } else {
        // Bare repo case
        Ok(git_dir_path)
    }
}

/// Reject symlink components of `path` that lie *under* `base`.
///
/// Components of `base` itself (and ancestors) are not checked: on macOS the
/// system temp root lives under `/var` → `/private/var`, which is a legitimate
/// OS alias, not an escape. Escape risk is owner/repo/job segments that are
/// symlinks pointing outside the sandbox.
fn reject_symlink_components_under(base: &Path, path: &Path) -> Result<()> {
    let relative = path
        .strip_prefix(base)
        .map_err(|_| Error::SandboxViolation {
            base: base.to_path_buf(),
            candidate: path.to_path_buf(),
            reason: "path is not under worktree base",
        })?;

    let mut cur = base.to_path_buf();
    for comp in relative.components() {
        cur.push(comp);
        if !cur.exists() {
            continue;
        }
        let meta = fs::symlink_metadata(&cur).map_err(|e| Error::Io {
            context: "stat path component for symlink check",
            source: e,
        })?;
        if meta.file_type().is_symlink() {
            return Err(Error::SandboxViolation {
                base: base.to_path_buf(),
                candidate: cur,
                reason: "symlink component under worktree base is not allowed",
            });
        }
    }
    Ok(())
}

/// Check if a path is within the base directory (sandbox check).
fn is_within_base(path: &Path, base: &Path) -> Result<bool> {
    let canonical_path = canonicalize_for_tools(path).map_err(|e| Error::Io {
        context: "canonicalize candidate path",
        source: e,
    })?;
    let canonical_base = canonicalize_for_tools(base).map_err(|e| Error::Io {
        context: "canonicalize base path",
        source: e,
    })?;

    Ok(canonical_path.starts_with(&canonical_base))
}

/// Clean up empty parent directories up to the base.
fn cleanup_empty_parents(path: &Path, base: &Path) {
    let mut current = path.parent();
    while let Some(parent) = current {
        if parent == base {
            break;
        }
        // Only remove if empty
        if fs::read_dir(parent)
            .map(|mut d| d.next().is_none())
            .unwrap_or(false)
        {
            let _ = fs::remove_dir(parent);
            current = parent.parent();
        } else {
            break;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::tempdir;

    fn init_test_repo(dir: &Path) -> Result<PathBuf> {
        init_test_repo_with_object_format(dir, None)
    }

    fn init_test_repo_with_object_format(
        dir: &Path,
        object_format: Option<&str>,
    ) -> Result<PathBuf> {
        // Initialize a git repo
        let mut init = Command::new("git");
        init.arg("-C").arg(dir).arg("init");
        if let Some(format) = object_format {
            init.arg(format!("--object-format={format}"));
        }
        init.arg("-b").arg("main").output().map_err(|e| Error::Io {
            context: "git init",
            source: e,
        })?;

        // Configure git for testing
        Command::new("git")
            .arg("-C")
            .arg(dir)
            .arg("config")
            .arg("user.email")
            .arg("test@example.com")
            .output()
            .map_err(|e| Error::Io {
                context: "git config email",
                source: e,
            })?;

        Command::new("git")
            .arg("-C")
            .arg(dir)
            .arg("config")
            .arg("user.name")
            .arg("Test User")
            .output()
            .map_err(|e| Error::Io {
                context: "git config name",
                source: e,
            })?;

        // Create initial commit
        fs::write(dir.join("README.md"), "# Test Repo\n").unwrap();
        Command::new("git")
            .arg("-C")
            .arg(dir)
            .arg("add")
            .arg("README.md")
            .output()
            .map_err(|e| Error::Io {
                context: "git add",
                source: e,
            })?;

        Command::new("git")
            .arg("-C")
            .arg(dir)
            .arg("commit")
            .arg("-m")
            .arg("Initial commit")
            .output()
            .map_err(|e| Error::Io {
                context: "git commit",
                source: e,
            })?;

        Ok(dir.to_path_buf())
    }

    fn git_output(repo: &Path, args: &[&str]) -> String {
        let output = Command::new("git")
            .arg("-C")
            .arg(repo)
            .args(args)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "git {args:?} failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8(output.stdout).unwrap().trim().to_string()
    }

    #[test]
    fn create_and_list_worktree() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();

        let base = temp.path().join("worktrees");
        let manager = WorktreeManager::with_base(base.clone()).unwrap();

        let start_commit = git_output(&repo_root, &["rev-parse", "HEAD"]);

        // Create a worktree for a new branch
        let wt = manager
            .create_with_request(WorktreeCreateRequest {
                repo_root: &repo_root,
                owner: "acme",
                repo: "test-repo",
                job_id: "job-1",
                branch: "feature/test",
                start_point: &start_commit,
            })
            .unwrap();

        assert!(wt.path.exists());
        assert_eq!(wt.branch, "feature/test");
        assert_eq!(wt.start_commit.as_deref(), Some(start_commit.as_str()));

        // List worktrees
        let listed = manager.list().unwrap();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].path, wt.path);
    }

    #[test]
    fn positional_create_api_remains_compatible() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();
        let start_commit = git_output(&repo_root, &["rev-parse", "HEAD"]);
        let manager = WorktreeManager::with_base(temp.path().join("worktrees")).unwrap();

        let wt = manager
            .create(
                &repo_root,
                "acme",
                "test-repo",
                "job-positional",
                "feature/positional",
                &start_commit,
            )
            .unwrap();

        assert_eq!(wt.head_commit.as_deref(), Some(start_commit.as_str()));
    }

    #[test]
    fn create_rejects_existing_branch_without_resume_identity() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();

        // Create a branch in the repo
        Command::new("git")
            .arg("-C")
            .arg(&repo_root)
            .arg("branch")
            .arg("existing-branch")
            .output()
            .unwrap();

        let base = temp.path().join("worktrees");
        let manager = WorktreeManager::with_base(base).unwrap();

        let start_commit = git_output(&repo_root, &["rev-parse", "HEAD"]);

        let result = manager.create_with_request(WorktreeCreateRequest {
            repo_root: &repo_root,
            owner: "acme",
            repo: "test-repo",
            job_id: "job-2",
            branch: "existing-branch",
            start_point: &start_commit,
        });

        assert!(matches!(
            result,
            Err(Error::PolicyViolation {
                code: PolicyCode::WorktreeResumeUnproven,
                ..
            })
        ));
        assert!(
            !manager
                .base_path()
                .unwrap()
                .join("acme/test-repo/job-2")
                .exists()
        );
    }

    #[test]
    fn create_rejects_branch_checked_out_in_another_worktree() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();
        let start_commit = git_output(&repo_root, &["rev-parse", "HEAD"]);
        let other = temp.path().join("other-worktree");
        git_output(
            &repo_root,
            &[
                "worktree",
                "add",
                "-b",
                "feature/elsewhere",
                "--",
                other.to_str().unwrap(),
                &start_commit,
            ],
        );
        let manager = WorktreeManager::with_base(temp.path().join("worktrees")).unwrap();

        let result = manager.create_with_request(WorktreeCreateRequest {
            repo_root: &repo_root,
            owner: "acme",
            repo: "test-repo",
            job_id: "job-elsewhere",
            branch: "feature/elsewhere",
            start_point: &start_commit,
        });

        assert!(matches!(
            result,
            Err(Error::PolicyViolation {
                code: PolicyCode::WorktreeResumeUnproven,
                ..
            })
        ));
        assert_eq!(
            git_output(&other, &["rev-parse", "HEAD"]),
            start_commit,
            "the unrelated checked-out branch must remain untouched"
        );
    }

    #[test]
    fn create_uses_resolved_start_commit_not_ambient_head() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();
        let requested_commit = git_output(&repo_root, &["rev-parse", "HEAD"]);

        fs::write(repo_root.join("later.txt"), "ambient head only\n").unwrap();
        git_output(&repo_root, &["add", "later.txt"]);
        git_output(&repo_root, &["commit", "-m", "advance ambient HEAD"]);
        let ambient_head = git_output(&repo_root, &["rev-parse", "HEAD"]);
        assert_ne!(requested_commit, ambient_head);

        let manager = WorktreeManager::with_base(temp.path().join("worktrees")).unwrap();
        let wt = manager
            .create_with_request(WorktreeCreateRequest {
                repo_root: &repo_root,
                owner: "acme",
                repo: "test-repo",
                job_id: "job-exact",
                branch: "feature/exact",
                start_point: &requested_commit,
            })
            .unwrap();

        assert_eq!(wt.start_commit.as_deref(), Some(requested_commit.as_str()));
        assert_eq!(wt.head_commit.as_deref(), Some(requested_commit.as_str()));
        assert_eq!(
            git_output(&wt.path, &["rev-parse", "HEAD"]),
            requested_commit
        );
        assert!(!wt.path.join("later.txt").exists());
    }

    #[test]
    fn create_verifies_full_branch_ref_when_same_named_tag_exists() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();
        let requested_commit = git_output(&repo_root, &["rev-parse", "HEAD"]);
        git_output(&repo_root, &["tag", "feature/collision", &requested_commit]);

        let manager = WorktreeManager::with_base(temp.path().join("worktrees")).unwrap();
        let wt = manager
            .create_with_request(WorktreeCreateRequest {
                repo_root: &repo_root,
                owner: "acme",
                repo: "test-repo",
                job_id: "job-collision",
                branch: "feature/collision",
                start_point: &requested_commit,
            })
            .unwrap();

        assert_eq!(
            git_output(&wt.path, &["symbolic-ref", "--quiet", "HEAD"]),
            "refs/heads/feature/collision"
        );
        assert_eq!(wt.head_commit.as_deref(), Some(requested_commit.as_str()));
    }

    #[test]
    fn postconditions_reject_moved_branch_and_preserve_residual_state() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();
        let requested_commit = git_output(&repo_root, &["rev-parse", "HEAD"]);
        let manager = WorktreeManager::with_base(temp.path().join("worktrees")).unwrap();
        let wt = manager
            .create_with_request(WorktreeCreateRequest {
                repo_root: &repo_root,
                owner: "acme",
                repo: "test-repo",
                job_id: "job-postcondition",
                branch: "feature/postcondition",
                start_point: &requested_commit,
            })
            .unwrap();

        fs::write(repo_root.join("moved.txt"), "moved\n").unwrap();
        git_output(&repo_root, &["add", "moved.txt"]);
        git_output(&repo_root, &["commit", "-m", "moved branch target"]);
        let moved_commit = git_output(&repo_root, &["rev-parse", "HEAD"]);
        git_output(
            &repo_root,
            &[
                "update-ref",
                "refs/heads/feature/postcondition",
                &moved_commit,
            ],
        );

        let result = verify_creation_postconditions(CreationPostconditions {
            repo_root: &repo_root,
            worktree_path: &wt.path,
            expected_branch: "feature/postcondition",
            expected_commit: &requested_commit,
        });
        assert!(matches!(result, Err(Error::WorktreePostconditionFailed(_))));
        assert_eq!(
            git_output(
                &repo_root,
                &["rev-parse", "refs/heads/feature/postcondition"]
            ),
            moved_commit
        );
        assert!(wt.path.exists());
    }

    #[test]
    fn registration_identity_requires_matching_path_branch_and_head() {
        let path = Path::new("/tmp/hive/job");
        let expected_head = "a".repeat(40);
        let correct = format!(
            "worktree /tmp/hive/job\nHEAD {expected_head}\nbranch refs/heads/feature/job\n\n"
        );
        let wrong_branch = format!(
            "worktree /tmp/hive/job\nHEAD {expected_head}\nbranch refs/heads/feature/other\n\n"
        );

        assert!(worktree_registration_matches(
            &correct,
            path,
            "refs/heads/feature/job",
            &expected_head,
        ));
        assert!(!worktree_registration_matches(
            &wrong_branch,
            path,
            "refs/heads/feature/job",
            &expected_head,
        ));
    }

    #[test]
    fn create_rejects_invalid_start_point_without_creating_branch() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();
        let manager = WorktreeManager::with_base(temp.path().join("worktrees")).unwrap();

        let result = manager.create_with_request(WorktreeCreateRequest {
            repo_root: &repo_root,
            owner: "acme",
            repo: "test-repo",
            job_id: "job-invalid",
            branch: "feature/invalid",
            start_point: "refs/heads/does-not-exist",
        });

        assert!(matches!(result, Err(Error::GitCommand { .. })));
        let branch_check = Command::new("git")
            .arg("-C")
            .arg(&repo_root)
            .args([
                "show-ref",
                "--verify",
                "--quiet",
                "refs/heads/feature/invalid",
            ])
            .status()
            .unwrap();
        assert!(!branch_check.success());
    }

    #[test]
    fn create_rejects_abbreviated_all_hex_start_point_without_mutation() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();
        let full_commit = git_output(&repo_root, &["rev-parse", "HEAD"]);
        let abbreviated = &full_commit[..12];
        let manager = WorktreeManager::with_base(temp.path().join("worktrees")).unwrap();

        let result = manager.create_with_request(WorktreeCreateRequest {
            repo_root: &repo_root,
            owner: "acme",
            repo: "test-repo",
            job_id: "job-abbreviated",
            branch: "feature/abbreviated",
            start_point: abbreviated,
        });

        assert!(matches!(result, Err(Error::GitCommand { .. })));
        assert!(
            git_output(&repo_root, &["branch", "--list", "feature/abbreviated"])
                .trim()
                .is_empty()
        );
        assert!(
            !manager
                .base_path()
                .unwrap()
                .join("acme/test-repo/job-abbreviated")
                .exists()
        );
    }

    #[test]
    fn create_accepts_uppercase_full_object_id_and_returns_canonical_lowercase() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();
        let full_commit = git_output(&repo_root, &["rev-parse", "HEAD"]);
        let uppercase_commit = full_commit.to_ascii_uppercase();
        let manager = WorktreeManager::with_base(temp.path().join("worktrees")).unwrap();

        let wt = manager
            .create_with_request(WorktreeCreateRequest {
                repo_root: &repo_root,
                owner: "acme",
                repo: "test-repo",
                job_id: "job-uppercase",
                branch: "feature/uppercase",
                start_point: &uppercase_commit,
            })
            .unwrap();

        assert_eq!(wt.start_commit.as_deref(), Some(full_commit.as_str()));
        assert_eq!(wt.head_commit.as_deref(), Some(full_commit.as_str()));
    }

    #[test]
    fn create_accepts_uppercase_full_sha256_and_returns_canonical_lowercase() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo_with_object_format(&repo, Some("sha256")).unwrap();
        let full_commit = git_output(&repo_root, &["rev-parse", "HEAD"]);
        assert_eq!(full_commit.len(), 64);
        let uppercase_commit = full_commit.to_ascii_uppercase();
        let manager = WorktreeManager::with_base(temp.path().join("worktrees")).unwrap();

        let wt = manager
            .create_with_request(WorktreeCreateRequest {
                repo_root: &repo_root,
                owner: "acme",
                repo: "test-repo",
                job_id: "job-uppercase-sha256",
                branch: "feature/uppercase-sha256",
                start_point: &uppercase_commit,
            })
            .unwrap();

        assert_eq!(wt.start_commit.as_deref(), Some(full_commit.as_str()));
        assert_eq!(wt.head_commit.as_deref(), Some(full_commit.as_str()));
    }

    #[test]
    fn create_rejects_sha256_prefix_with_sha1_width_without_mutation() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo_with_object_format(&repo, Some("sha256")).unwrap();
        let full_commit = git_output(&repo_root, &["rev-parse", "HEAD"]);
        assert_eq!(full_commit.len(), 64);
        let abbreviated = &full_commit[..40];
        let manager = WorktreeManager::with_base(temp.path().join("worktrees")).unwrap();
        let expected_path = manager
            .base_path()
            .unwrap()
            .join("acme/test-repo/job-sha256-prefix");

        let result = manager.create_with_request(WorktreeCreateRequest {
            repo_root: &repo_root,
            owner: "acme",
            repo: "test-repo",
            job_id: "job-sha256-prefix",
            branch: "feature/sha256-prefix",
            start_point: abbreviated,
        });

        assert!(matches!(result, Err(Error::GitCommand { .. })));
        assert!(
            git_output(&repo_root, &["branch", "--list", "feature/sha256-prefix"])
                .trim()
                .is_empty()
        );
        assert!(!expected_path.exists());
        assert!(
            !git_output(&repo_root, &["worktree", "list", "--porcelain"])
                .contains(&expected_path.to_string_lossy().to_string())
        );
    }

    fn assert_create_rejects_start_point_without_mutation(
        manager: &WorktreeManager,
        repo_root: &Path,
        start_point: &str,
        job_id: &str,
        branch: &str,
    ) {
        let expected_path = manager
            .base_path()
            .unwrap()
            .join(format!("acme/test-repo/{job_id}"));
        let result = manager.create_with_request(WorktreeCreateRequest {
            repo_root,
            owner: "acme",
            repo: "test-repo",
            job_id,
            branch,
            start_point,
        });
        assert!(
            matches!(result, Err(Error::GitCommand { .. })),
            "expected GitCommand reject for {start_point:?}, got {result:?}"
        );
        assert!(
            git_output(repo_root, &["branch", "--list", branch])
                .trim()
                .is_empty()
        );
        assert!(!expected_path.exists());
    }

    #[test]
    fn create_rejects_decorated_abbreviated_hex_start_points_without_mutation() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();
        let full_commit = git_output(&repo_root, &["rev-parse", "HEAD"]);
        let abbreviated = &full_commit[..12];
        let manager = WorktreeManager::with_base(temp.path().join("worktrees")).unwrap();

        for (suffix, job_id, branch) in [
            ("~0", "job-abbrev-tilde", "feature/abbrev-tilde"),
            ("^0", "job-abbrev-caret", "feature/abbrev-caret"),
            ("^{commit}", "job-abbrev-peel", "feature/abbrev-peel"),
        ] {
            let start_point = format!("{abbreviated}{suffix}");
            assert_create_rejects_start_point_without_mutation(
                &manager,
                &repo_root,
                &start_point,
                job_id,
                branch,
            );
        }
    }

    #[test]
    fn create_rejects_sha256_prefix_with_tilde_zero_without_mutation() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo_with_object_format(&repo, Some("sha256")).unwrap();
        let full_commit = git_output(&repo_root, &["rev-parse", "HEAD"]);
        assert_eq!(full_commit.len(), 64);
        let decorated = format!("{}~0", &full_commit[..40]);
        let manager = WorktreeManager::with_base(temp.path().join("worktrees")).unwrap();

        assert_create_rejects_start_point_without_mutation(
            &manager,
            &repo_root,
            &decorated,
            "job-sha256-prefix-tilde",
            "feature/sha256-prefix-tilde",
        );
    }

    #[test]
    fn create_accepts_symbolic_ref_and_full_object_id() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();
        let full_commit = git_output(&repo_root, &["rev-parse", "HEAD"]);
        let manager = WorktreeManager::with_base(temp.path().join("worktrees")).unwrap();

        let from_ref = manager
            .create_with_request(WorktreeCreateRequest {
                repo_root: &repo_root,
                owner: "acme",
                repo: "test-repo",
                job_id: "job-symbolic",
                branch: "feature/symbolic",
                start_point: "refs/heads/main",
            })
            .unwrap();
        assert_eq!(from_ref.start_commit.as_deref(), Some(full_commit.as_str()));

        let from_oid = manager
            .create_with_request(WorktreeCreateRequest {
                repo_root: &repo_root,
                owner: "acme",
                repo: "test-repo",
                job_id: "job-full-oid",
                branch: "feature/full-oid",
                start_point: &full_commit,
            })
            .unwrap();
        assert_eq!(from_oid.start_commit.as_deref(), Some(full_commit.as_str()));
    }

    #[test]
    fn create_failure_reports_and_preserves_residual_branch() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();
        let start_commit = git_output(&repo_root, &["rev-parse", "HEAD"]);
        let manager = WorktreeManager::with_base(temp.path().join("worktrees")).unwrap();
        let target = manager
            .base_path()
            .unwrap()
            .join("acme/test-repo/job-collision");
        fs::create_dir_all(&target).unwrap();
        fs::write(target.join("occupied"), "force worktree add failure\n").unwrap();

        let result = manager.create_with_request(WorktreeCreateRequest {
            repo_root: &repo_root,
            owner: "acme",
            repo: "test-repo",
            job_id: "job-collision",
            branch: "feature/rollback",
            start_point: &start_commit,
        });

        assert!(
            matches!(
                result,
                Err(Error::WorktreeCreationFailed(ref failure))
                    if failure.branch_commit.as_ref() == Some(&start_commit)
                        && !failure.worktree_registered
            ),
            "unexpected result: {result:?}"
        );
        assert_eq!(
            git_output(&repo_root, &["rev-parse", "refs/heads/feature/rollback"]),
            start_commit
        );
        fs::write(repo_root.join("adopted.txt"), "adopted after failure\n").unwrap();
        git_output(&repo_root, &["add", "adopted.txt"]);
        git_output(&repo_root, &["commit", "-m", "adopt residual branch"]);
        let adopted_commit = git_output(&repo_root, &["rev-parse", "HEAD"]);
        git_output(
            &repo_root,
            &["update-ref", "refs/heads/feature/rollback", &adopted_commit],
        );
        assert_eq!(
            git_output(&repo_root, &["rev-parse", "refs/heads/feature/rollback"]),
            adopted_commit,
            "no delayed cleanup may delete a residual branch adopted after failure"
        );
        assert_eq!(
            fs::read_to_string(target.join("occupied")).unwrap(),
            "force worktree add failure\n"
        );
    }

    #[test]
    fn remove_worktree() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();

        let base = temp.path().join("worktrees");
        let manager = WorktreeManager::with_base(base.clone()).unwrap();

        let start_commit = git_output(&repo_root, &["rev-parse", "HEAD"]);

        let wt = manager
            .create_with_request(WorktreeCreateRequest {
                repo_root: &repo_root,
                owner: "acme",
                repo: "test-repo",
                job_id: "job-3",
                branch: "feature/remove",
                start_point: &start_commit,
            })
            .unwrap();

        assert!(wt.path.exists());

        manager.remove(&wt.path, false).unwrap();

        assert!(!wt.path.exists());
    }

    #[test]
    fn reject_path_outside_sandbox() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();

        let base = temp.path().join("worktrees");
        let manager = WorktreeManager::with_base(base).unwrap();

        // Try to create a worktree with path traversal in job_id
        let result = manager.create_with_request(WorktreeCreateRequest {
            repo_root: &repo_root,
            owner: "acme",
            repo: "test-repo",
            job_id: "../escape",
            branch: "branch",
            start_point: "HEAD",
        });
        assert!(result.is_err());
    }

    #[test]
    fn prune_worktrees() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();

        let base = temp.path().join("worktrees");
        let manager = WorktreeManager::with_base(base).unwrap();

        let start_commit = git_output(&repo_root, &["rev-parse", "HEAD"]);

        let wt = manager
            .create_with_request(WorktreeCreateRequest {
                repo_root: &repo_root,
                owner: "acme",
                repo: "test-repo",
                job_id: "job-4",
                branch: "feature/prune",
                start_point: &start_commit,
            })
            .unwrap();

        // Remove the worktree directory manually (simulating stale state)
        fs::remove_dir_all(&wt.path).unwrap();

        // Prune should clean up the git worktree admin files
        manager.prune(&repo_root).unwrap();
    }

    #[test]
    fn reject_symlink_owner_under_base() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();

        let base = temp.path().join("worktrees");
        let outside = temp.path().join("outside");
        fs::create_dir_all(&outside).unwrap();
        let manager = WorktreeManager::with_base(base.clone()).unwrap();

        // Pre-create owner segment as a symlink that escapes the base.
        let owner_link = manager.base_path().unwrap().join("acme");
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(&outside, &owner_link).unwrap();
        }
        #[cfg(windows)]
        {
            std::os::windows::fs::symlink_dir(&outside, &owner_link).unwrap();
        }

        let result = manager.create_with_request(WorktreeCreateRequest {
            repo_root: &repo_root,
            owner: "acme",
            repo: "test-repo",
            job_id: "job-sym",
            branch: "branch-sym",
            start_point: "HEAD",
        });
        assert!(
            matches!(result, Err(Error::SandboxViolation { .. })),
            "expected SandboxViolation, got {result:?}"
        );
    }
}
