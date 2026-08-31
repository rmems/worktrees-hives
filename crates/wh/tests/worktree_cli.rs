use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT_ID: AtomicU64 = AtomicU64::new(0);

struct TestDir(PathBuf);

impl TestDir {
    fn new() -> Self {
        let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
        let path =
            std::env::temp_dir().join(format!("wh-worktree-cli-{}-{id}", std::process::id()));
        fs::create_dir_all(&path).unwrap();
        Self(path)
    }
}

impl Drop for TestDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn git(repo: &Path, args: &[&str]) -> String {
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
    String::from_utf8(output.stdout).unwrap().trim().to_owned()
}

fn init_repo(root: &Path) -> PathBuf {
    let repo = root.join("repo");
    fs::create_dir(&repo).unwrap();
    git(&repo, &["init", "-b", "trunk"]);
    git(&repo, &["config", "user.email", "test@example.com"]);
    git(&repo, &["config", "user.name", "Test User"]);
    git(&repo, &["commit", "--allow-empty", "-m", "initial"]);
    repo
}

struct CreateRequest<'a> {
    job: &'a str,
    branch: &'a str,
    start: &'a str,
}

fn wh_create(root: &Path, repo: &Path, request: CreateRequest<'_>) -> Output {
    Command::new(env!("CARGO_BIN_EXE_wh"))
        .env("WH_WORKTREE_BASE", root.join("worktrees"))
        .args([
            "--json",
            "worktree",
            "create",
            "--schema-version",
            "2",
            "--repo",
            repo.to_str().unwrap(),
            "--start-point",
            request.start,
            "acme",
            "sample",
            request.job,
            request.branch,
        ])
        .output()
        .unwrap()
}

fn json(output: &Output) -> serde_json::Value {
    serde_json::from_slice(&output.stdout).unwrap_or_else(|error| {
        panic!(
            "stdout was not a JSON envelope: {error}; stdout={:?}; stderr={:?}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        )
    })
}

fn assert_error_envelope(
    output: &Output,
    expected_exit: i32,
    expected_schema: u64,
    expected_code: &str,
) -> serde_json::Value {
    let envelope = json(output);
    assert_eq!(output.status.code(), Some(expected_exit));
    assert_eq!(
        serde_json::json!({
            "ok": false,
            "schema_version": expected_schema,
            "command": "worktree.create",
            "error_code": expected_code,
        }),
        serde_json::json!({
            "ok": envelope["ok"],
            "schema_version": envelope["schema_version"],
            "command": envelope["command"],
            "error_code": envelope["error"]["code"],
        })
    );
    envelope
}

#[test]
fn legacy_v1_create_fails_with_machine_readable_upgrade_error_without_mutation() {
    let root = TestDir::new();
    let repo = init_repo(&root.0);
    let output = Command::new(env!("CARGO_BIN_EXE_wh"))
        .env("WH_WORKTREE_BASE", root.0.join("worktrees"))
        .args([
            "--json",
            "worktree",
            "create",
            "--repo",
            repo.to_str().unwrap(),
            "acme",
            "sample",
            "legacy",
            "feature/legacy",
        ])
        .output()
        .unwrap();
    let envelope = assert_error_envelope(&output, 1, 1, "CONTRACT_UPGRADE_REQUIRED");

    assert_eq!(envelope["data"]["required_schema_version"], 2);
    assert!(!root.0.join("worktrees/acme/sample/legacy").exists());
    assert!(
        git(&repo, &["branch", "--list", "feature/legacy"])
            .trim()
            .is_empty()
    );
}

#[test]
fn v2_create_without_start_point_fails_with_machine_readable_error_without_mutation() {
    let root = TestDir::new();
    let repo = init_repo(&root.0);
    let output = Command::new(env!("CARGO_BIN_EXE_wh"))
        .env("WH_WORKTREE_BASE", root.0.join("worktrees"))
        .args([
            "--json",
            "worktree",
            "create",
            "--schema-version",
            "2",
            "--repo",
            repo.to_str().unwrap(),
            "acme",
            "sample",
            "missing",
            "feature/missing",
        ])
        .output()
        .unwrap();
    assert_error_envelope(&output, 1, 2, "START_POINT_REQUIRED");

    assert!(!root.0.join("worktrees/acme/sample/missing").exists());
    assert!(
        git(&repo, &["branch", "--list", "feature/missing"])
            .trim()
            .is_empty()
    );
}

#[test]
fn invalid_start_point_emits_v2_error_envelope_with_exit_1() {
    let root = TestDir::new();
    let repo = init_repo(&root.0);

    let output = wh_create(
        &root.0,
        &repo,
        CreateRequest {
            job: "invalid",
            branch: "feature/invalid",
            start: "does-not-exist",
        },
    );
    let envelope = assert_error_envelope(&output, 1, 2, "GIT_COMMAND_FAILED");

    assert_eq!(envelope["data"], serde_json::json!({}));
}

#[test]
fn existing_branch_policy_emits_v2_error_envelope_with_exit_2() {
    let root = TestDir::new();
    let repo = init_repo(&root.0);
    let start = git(&repo, &["rev-parse", "HEAD"]);
    git(&repo, &["branch", "feature/existing", &start]);

    let output = wh_create(
        &root.0,
        &repo,
        CreateRequest {
            job: "existing",
            branch: "feature/existing",
            start: &start,
        },
    );
    assert_error_envelope(&output, 2, 2, "WORKTREE_RESUME_UNPROVEN");
}

#[test]
fn v2_success_reports_verified_path_ref_commit_and_registration_identity() {
    let root = TestDir::new();
    let repo = init_repo(&root.0);
    let start = git(&repo, &["rev-parse", "HEAD"]);

    let output = wh_create(
        &root.0,
        &repo,
        CreateRequest {
            job: "success",
            branch: "feature/success",
            start: &start,
        },
    );
    let envelope = json(&output);

    assert!(output.status.success());
    assert_eq!(
        serde_json::json!({
            "ok": true,
            "schema_version": 2,
            "path": root.0.join("worktrees/acme/sample/success"),
            "branch": "feature/success",
            "branch_ref": "refs/heads/feature/success",
            "repo_root": repo,
            "start_commit": start,
            "head_commit": start,
            "worktree_registered": true,
        }),
        serde_json::json!({
            "ok": envelope["ok"],
            "schema_version": envelope["schema_version"],
            "path": envelope["data"]["path"],
            "branch": envelope["data"]["branch"],
            "branch_ref": envelope["data"]["branch_ref"],
            "repo_root": envelope["data"]["repo_root"],
            "start_commit": envelope["data"]["start_commit"],
            "head_commit": envelope["data"]["head_commit"],
            "worktree_registered": envelope["data"]["worktree_registered"],
        })
    );
}

#[test]
fn partial_create_failure_reports_residual_state_without_deleting_branch() {
    let root = TestDir::new();
    let repo = init_repo(&root.0);
    let start = git(&repo, &["rev-parse", "HEAD"]);
    let target = root.0.join("worktrees/acme/sample/partial");
    fs::create_dir_all(&target).unwrap();
    fs::write(target.join("occupied"), "keep\n").unwrap();

    let output = wh_create(
        &root.0,
        &repo,
        CreateRequest {
            job: "partial",
            branch: "feature/partial",
            start: &start,
        },
    );
    let envelope = assert_error_envelope(&output, 1, 2, "WORKTREE_CREATE_FAILED");

    assert_eq!(
        serde_json::json!({
            "branch_commit": start,
            "path_exists": true,
            "worktree_registered": false,
            "cleanup_performed": false,
        }),
        serde_json::json!({
            "branch_commit": envelope["data"]["branch_commit"],
            "path_exists": envelope["data"]["path_exists"],
            "worktree_registered": envelope["data"]["worktree_registered"],
            "cleanup_performed": envelope["data"]["cleanup_performed"],
        })
    );
    assert_eq!(
        git(&repo, &["rev-parse", "refs/heads/feature/partial"]),
        start
    );
    assert_eq!(
        fs::read_to_string(target.join("occupied")).unwrap(),
        "keep\n"
    );
}
