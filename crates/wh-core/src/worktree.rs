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
    /// mutation. Existing branches are rejected because v1 has no durable
    /// resume identity that can prove ownership of an existing ref.
    pub fn create(
        &self,
        repo_root: &Path,
        owner: &str,
        repo: &str,
        job_id: &str,
        branch: &str,
        start_point: &str,
    ) -> Result<Worktree> {
        // Reject option-looking branch names (would be parsed as git flags).
        if branch.is_empty() || branch.starts_with('-') {
            return Err(Error::GitCommand {
                args: vec!["worktree".into(), "add".into()],
                stderr: format!("invalid branch name (empty or option-looking): {branch:?}"),
            });
        }

        let base = self.base_path()?;
        let worktree_path = derive_worktree_path(base, owner, repo, job_id)?;

        // Reject symlink *segments under the base* (not OS aliases above the base
        // such as macOS /var → /private/var). Pre-check existing segments, mkdir,
        // then re-check so we never follow a planted escape link.
        reject_symlink_components_under(base, &worktree_path)?;
        if let Some(parent) = worktree_path.parent() {
            fs::create_dir_all(parent).map_err(|e| Error::Io {
                context: "create worktree parent directories",
                source: e,
            })?;
            reject_symlink_components_under(base, parent)?;
        }

        // Verify the repo_root is a valid git repository
        if !repo_root.join(".git").exists() && !is_bare_repo(repo_root)? {
            return Err(Error::GitCommand {
                args: vec!["worktree".into(), "add".into()],
                stderr: format!("not a git repository: {}", repo_root.display()),
            });
        }

        // Resolve the caller-selected start point before any mutation. Appending
        // ^{commit} rejects trees/blobs and peels annotated tags to commits.
        let start_commit = resolve_start_commit(repo_root, start_point)?;

        // Existing refs cannot be resumed safely without a durable identity
        // binding the job, branch, repository, and expected commit.
        let branch_exists = branch_exists_in_repo(repo_root, branch)?;
        if branch_exists {
            return Err(Error::PolicyViolation {
                code: PolicyCode::WorktreeResumeUnproven,
                message: format!(
                    "refusing to reuse existing branch {branch:?} at requested commit \
                     {start_commit}: safe resume identity is not proven"
                ),
            });
        }
        // Ask Git to create the branch and linked worktree in one operation.
        // If a concurrent actor creates the ref first, Git fails rather than
        // attaching the worktree to an identity we did not create.
        let output = Command::new("git")
            .arg("-C")
            .arg(repo_root)
            .arg("worktree")
            .arg("add")
            .arg("-b")
            .arg(branch)
            .arg("--")
            .arg(&worktree_path)
            .arg(&start_commit)
            .output()
            .map_err(|e| Error::Io {
                context: "spawn git worktree add",
                source: e,
            })?;

        if !output.status.success() {
            let residual = inspect_residual_state(repo_root, &worktree_path, branch);
            return Err(Error::WorktreeCreationFailed(Box::new(
                WorktreeCreationFailure {
                    path: worktree_path,
                    branch: branch.to_owned(),
                    path_exists: residual.path_exists,
                    branch_commit: residual.branch_commit,
                    head_commit: residual.head_commit,
                    worktree_registered: residual.worktree_registered,
                    stderr: String::from_utf8_lossy(&output.stderr).to_string(),
                },
            )));
        }

        let head_commit =
            verify_creation_postconditions(repo_root, &worktree_path, branch, &start_commit)?;

        Ok(Worktree {
            path: worktree_path,
            branch: branch.to_string(),
            repo_root: repo_root.to_path_buf(),
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

/// Resolve a caller-supplied commit-ish to one exact commit object.
fn resolve_start_commit(repo_root: &Path, start_point: &str) -> Result<String> {
    if start_point.is_empty() {
        return Err(Error::GitCommand {
            args: vec!["rev-parse".into(), "--verify".into()],
            stderr: "start point must not be empty".into(),
        });
    }

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

    if !output.status.success() {
        return Err(Error::GitCommand {
            args: vec![
                "rev-parse".into(),
                "--verify".into(),
                "--end-of-options".into(),
                commitish,
            ],
            stderr: String::from_utf8_lossy(&output.stderr).to_string(),
        });
    }

    let commit = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    if commit.is_empty() {
        return Err(Error::GitCommand {
            args: vec!["rev-parse".into(), "--verify".into()],
            stderr: format!("start point {start_point:?} resolved to an empty commit id"),
        });
    }
    Ok(commit)
}

fn verify_creation_postconditions(
    repo_root: &Path,
    worktree_path: &Path,
    expected_branch: &str,
    expected_commit: &str,
) -> Result<String> {
    let actual_branch = git_stdout(
        worktree_path,
        &["symbolic-ref", "--quiet", "--short", "HEAD"],
        "verify created worktree branch",
    )
    .map_err(|error| {
        postcondition_failure(
            repo_root,
            worktree_path,
            expected_branch,
            expected_commit,
            None,
            &error.to_string(),
        )
    })?;
    let branch_commit = git_stdout(
        repo_root,
        &[
            "rev-parse",
            "--verify",
            "--end-of-options",
            &format!("refs/heads/{expected_branch}^{{commit}}"),
        ],
        "verify created branch commit",
    )
    .map_err(|error| {
        postcondition_failure(
            repo_root,
            worktree_path,
            expected_branch,
            expected_commit,
            Some(actual_branch.clone()),
            &error.to_string(),
        )
    })?;
    let head_commit = git_stdout(
        worktree_path,
        &["rev-parse", "--verify", "HEAD^{commit}"],
        "verify created worktree HEAD",
    )
    .map_err(|error| {
        postcondition_failure(
            repo_root,
            worktree_path,
            expected_branch,
            expected_commit,
            Some(actual_branch.clone()),
            &error.to_string(),
        )
    })?;

    if actual_branch != expected_branch
        || branch_commit != expected_commit
        || head_commit != expected_commit
    {
        return Err(postcondition_failure(
            repo_root,
            worktree_path,
            expected_branch,
            expected_commit,
            Some(actual_branch.clone()),
            &format!(
                "identity mismatch: actual branch={actual_branch:?} \
                 branch_commit={branch_commit}, head_commit={head_commit}"
            ),
        ));
    }
    Ok(head_commit)
}

fn postcondition_failure(
    repo_root: &Path,
    worktree_path: &Path,
    expected_branch: &str,
    expected_commit: &str,
    actual_branch: Option<String>,
    cause: &str,
) -> Error {
    let residual = inspect_residual_state(repo_root, worktree_path, expected_branch);
    Error::WorktreePostconditionFailed(Box::new(WorktreePostconditionFailure {
        path: worktree_path.to_path_buf(),
        branch: expected_branch.to_owned(),
        expected_commit: expected_commit.to_owned(),
        actual_branch,
        path_exists: residual.path_exists,
        branch_commit: residual.branch_commit,
        head_commit: residual.head_commit,
        worktree_registered: residual.worktree_registered,
        reason: cause.to_owned(),
    }))
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
        // Initialize a git repo
        Command::new("git")
            .arg("-C")
            .arg(dir)
            .arg("init")
            .arg("-b")
            .arg("main")
            .output()
            .map_err(|e| Error::Io {
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
            .create(
                &repo_root,
                "acme",
                "test-repo",
                "job-1",
                "feature/test",
                &start_commit,
            )
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

        let result = manager.create(
            &repo_root,
            "acme",
            "test-repo",
            "job-2",
            "existing-branch",
            &start_commit,
        );

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

        let result = manager.create(
            &repo_root,
            "acme",
            "test-repo",
            "job-elsewhere",
            "feature/elsewhere",
            &start_commit,
        );

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
            .create(
                &repo_root,
                "acme",
                "test-repo",
                "job-exact",
                "feature/exact",
                &requested_commit,
            )
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
    fn postconditions_reject_moved_branch_and_preserve_residual_state() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();
        let requested_commit = git_output(&repo_root, &["rev-parse", "HEAD"]);
        let manager = WorktreeManager::with_base(temp.path().join("worktrees")).unwrap();
        let wt = manager
            .create(
                &repo_root,
                "acme",
                "test-repo",
                "job-postcondition",
                "feature/postcondition",
                &requested_commit,
            )
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

        let result = verify_creation_postconditions(
            &repo_root,
            &wt.path,
            "feature/postcondition",
            &requested_commit,
        );
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
    fn create_rejects_invalid_start_point_without_creating_branch() {
        let temp = tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        let repo_root = init_test_repo(&repo).unwrap();
        let manager = WorktreeManager::with_base(temp.path().join("worktrees")).unwrap();

        let result = manager.create(
            &repo_root,
            "acme",
            "test-repo",
            "job-invalid",
            "feature/invalid",
            "refs/heads/does-not-exist",
        );

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

        let result = manager.create(
            &repo_root,
            "acme",
            "test-repo",
            "job-collision",
            "feature/rollback",
            &start_commit,
        );

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
            .create(
                &repo_root,
                "acme",
                "test-repo",
                "job-3",
                "feature/remove",
                &start_commit,
            )
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
        let result = manager.create(
            &repo_root,
            "acme",
            "test-repo",
            "../escape",
            "branch",
            "HEAD",
        );
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
            .create(
                &repo_root,
                "acme",
                "test-repo",
                "job-4",
                "feature/prune",
                &start_commit,
            )
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

        let result = manager.create(
            &repo_root,
            "acme",
            "test-repo",
            "job-sym",
            "branch-sym",
            "HEAD",
        );
        assert!(
            matches!(result, Err(Error::SandboxViolation { .. })),
            "expected SandboxViolation, got {result:?}"
        );
    }
}
