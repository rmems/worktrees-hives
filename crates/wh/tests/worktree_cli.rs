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

fn wh_create(root: &Path, repo: &Path, job: &str, branch: &str, start: &str) -> Output {
    Command::new(env!("CARGO_BIN_EXE_wh"))
        .env("WH_WORKTREE_BASE", root.join("worktrees"))
        .args([
            "--json",
            "worktree",
            "create",
            "--repo",
            repo.to_str().unwrap(),
            "--start-point",
            start,
            "acme",
            "sample",
            job,
            branch,
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

#[test]
fn invalid_start_point_emits_v1_error_envelope_with_exit_1() {
    let root = TestDir::new();
    let repo = init_repo(&root.0);

    let output = wh_create(
        &root.0,
        &repo,
        "invalid",
        "feature/invalid",
        "does-not-exist",
    );
    let envelope = json(&output);

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(envelope["ok"], false);
    assert_eq!(envelope["schema_version"], 1);
    assert_eq!(envelope["command"], "worktree.create");
    assert_eq!(envelope["data"], serde_json::json!({}));
    assert_eq!(envelope["error"]["code"], "GIT_COMMAND_FAILED");
}

#[test]
fn existing_branch_policy_emits_v1_error_envelope_with_exit_2() {
    let root = TestDir::new();
    let repo = init_repo(&root.0);
    let start = git(&repo, &["rev-parse", "HEAD"]);
    git(&repo, &["branch", "feature/existing", &start]);

    let output = wh_create(&root.0, &repo, "existing", "feature/existing", &start);
    let envelope = json(&output);

    assert_eq!(output.status.code(), Some(2));
    assert_eq!(envelope["ok"], false);
    assert_eq!(envelope["command"], "worktree.create");
    assert_eq!(envelope["error"]["code"], "WORKTREE_RESUME_UNPROVEN");
}

#[test]
fn success_reports_matching_start_and_verified_head_commits() {
    let root = TestDir::new();
    let repo = init_repo(&root.0);
    let start = git(&repo, &["rev-parse", "HEAD"]);

    let output = wh_create(&root.0, &repo, "success", "feature/success", &start);
    let envelope = json(&output);

    assert!(output.status.success());
    assert_eq!(envelope["ok"], true);
    assert_eq!(envelope["data"]["start_commit"], start);
    assert_eq!(envelope["data"]["head_commit"], start);
}

#[test]
fn partial_create_failure_reports_residual_state_without_deleting_branch() {
    let root = TestDir::new();
    let repo = init_repo(&root.0);
    let start = git(&repo, &["rev-parse", "HEAD"]);
    let target = root.0.join("worktrees/acme/sample/partial");
    fs::create_dir_all(&target).unwrap();
    fs::write(target.join("occupied"), "keep\n").unwrap();

    let output = wh_create(&root.0, &repo, "partial", "feature/partial", &start);
    let envelope = json(&output);

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(envelope["ok"], false);
    assert_eq!(envelope["error"]["code"], "WORKTREE_CREATE_FAILED");
    assert_eq!(envelope["data"]["branch_commit"], start);
    assert_eq!(envelope["data"]["path_exists"], true);
    assert_eq!(envelope["data"]["worktree_registered"], false);
    assert_eq!(envelope["data"]["cleanup_performed"], false);
    assert_eq!(
        git(&repo, &["rev-parse", "refs/heads/feature/partial"]),
        start
    );
    assert_eq!(
        fs::read_to_string(target.join("occupied")).unwrap(),
        "keep\n"
    );
}
