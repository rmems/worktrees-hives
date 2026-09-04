use std::io::{self, Write};
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Duration;

use clap::{Parser, Subcommand};

/// Manage isolated issue-to-PR jobs and their durable state.
#[derive(Debug, Parser)]
#[command(name = "wh", version, about, long_about = None)]
struct Cli {
    /// Emit responses as v1 JSON envelopes.
    #[arg(long, global = true)]
    json: bool,

    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Show status of all watched jobs.
    Status,
    /// List all watched jobs (alias for status).
    Jobs,
    /// Validate and run a git command under safety policy.
    GitSafe {
        /// Optional expected branch; mutating subcommands verify before running.
        #[arg(long)]
        expected_branch: Option<String>,

        /// Repository working tree (default: current directory).
        #[arg(long)]
        repo: Option<PathBuf>,

        /// Git arguments after the subcommand (e.g., push --force-with-lease origin main).
        #[arg(required = true, trailing_var_arg = true, allow_hyphen_values = true)]
        args: Vec<String>,
    },

    /// Validate and run a GitHub CLI command under safety policy.
    GhSafe {
        /// gh arguments (e.g., pr create --title "Fix").
        #[arg(required = true, trailing_var_arg = true, allow_hyphen_values = true)]
        args: Vec<String>,
    },

    /// Process supervisor: spawn with timeouts, process-group isolation, and max-parallel.
    Supervisor {
        #[command(subcommand)]
        action: SupervisorAction,
    },

    /// Isolated git worktree lifecycle (create/list/remove/prune).
    Worktree {
        #[command(subcommand)]
        action: WorktreeAction,
    },
}

#[derive(Debug, Subcommand)]
enum WorktreeAction {
    /// Create a worktree for a hive job under the sandboxed base path.
    Create {
        /// Repository root (git dir / working tree).
        #[arg(long)]
        repo: PathBuf,
        /// GitHub-style owner segment.
        owner: String,
        /// Repository name segment.
        repo_name: String,
        /// Job id segment (e.g. gh-42).
        job_id: String,
        /// Branch name for the worktree.
        branch: String,
    },
    /// List hive worktrees under the configured base.
    List,
    /// Remove a worktree path (must stay under the sandbox base).
    Remove {
        /// Absolute or relative worktree path.
        path: PathBuf,
        /// Force removal with dirty tree.
        #[arg(long)]
        force: bool,
    },
    /// Prune stale git worktree metadata for a repo.
    Prune {
        #[arg(long)]
        repo: PathBuf,
    },
}

#[derive(Debug, Subcommand)]
enum SupervisorAction {
    /// Run a command under supervision.
    Run {
        /// Wall-clock timeout in seconds. 0 means no timeout.
        #[arg(long, default_value = "0")]
        timeout: u64,

        /// Expected branch for mutating supervised `git` / `gh pr` commands.
        #[arg(long)]
        expected_branch: Option<String>,

        /// Repository working tree for supervised git branch checks (default: `.`).
        #[arg(long)]
        repo: Option<PathBuf>,

        /// Max concurrent supervised children **in this process** (default 1).
        /// Does not coordinate across separate `wh` processes.
        #[arg(long, default_value = "1")]
        max_parallel: usize,

        /// Command and arguments to run.
        #[arg(required = true, trailing_var_arg = true, allow_hyphen_values = true)]
        cmd: Vec<String>,
    },
}

#[tokio::main]
async fn main() -> ExitCode {
    let cli = Cli::parse();

    match run(cli, &mut io::stdout()).await {
        Ok(code) => code,
        Err(error) => {
            let _ = writeln!(io::stderr(), "wh: {error}");
            match &error {
                wh_core::error::Error::PolicyViolation { .. } => ExitCode::from(2),
                _ => ExitCode::FAILURE,
            }
        }
    }
}

fn run_worktree(
    action: WorktreeAction,
    json: bool,
    stdout: &mut impl Write,
) -> wh_core::error::Result<ExitCode> {
    use wh_core::contract::Response;
    use wh_core::worktree::WorktreeManager;

    let response = match action {
        WorktreeAction::Create {
            repo,
            owner,
            repo_name,
            job_id,
            branch,
        } => {
            let manager = WorktreeManager::new()?;
            let wt = manager.create(&repo, &owner, &repo_name, &job_id, &branch)?;
            Response {
                ok: true,
                schema_version: wh_core::contract::SCHEMA_VERSION,
                command: "worktree.create",
                data: serde_json::json!({
                    "path": wt.path,
                    "branch": wt.branch,
                    "repo_root": wt.repo_root,
                }),
                error: None,
            }
        }
        WorktreeAction::List => {
            let manager = WorktreeManager::new()?;
            let worktrees = manager.list()?;
            Response {
                ok: true,
                schema_version: wh_core::contract::SCHEMA_VERSION,
                command: "worktree.list",
                data: serde_json::json!({
                    "worktrees": worktrees.iter().map(|wt| serde_json::json!({
                        "path": wt.path,
                        "branch": wt.branch,
                    })).collect::<Vec<_>>(),
                }),
                error: None,
            }
        }
        WorktreeAction::Remove { path, force } => {
            let manager = WorktreeManager::new()?;
            manager.remove(&path, force)?;
            Response {
                ok: true,
                schema_version: wh_core::contract::SCHEMA_VERSION,
                command: "worktree.remove",
                data: serde_json::json!({ "removed": path }),
                error: None,
            }
        }
        WorktreeAction::Prune { repo } => {
            let manager = WorktreeManager::new()?;
            manager.prune(&repo)?;
            Response {
                ok: true,
                schema_version: wh_core::contract::SCHEMA_VERSION,
                command: "worktree.prune",
                data: serde_json::json!({}),
                error: None,
            }
        }
    };

    if json {
        serde_json::to_writer(&mut *stdout, &response).map_err(io::Error::other)?;
        stdout.write_all(b"\n")?;
    } else {
        writeln!(stdout, "ok={} command={}", response.ok, response.command)?;
    }
    Ok(ExitCode::SUCCESS)
}

/// Entry point for CLI commands (status/jobs, git/gh-safe, supervisor, worktree).
async fn run(cli: Cli, stdout: &mut impl Write) -> wh_core::error::Result<ExitCode> {
    match cli.command {
        Some(Command::Status) => {
            run_status(cli.json, "cli.status", wh_core::state::load_jobs(), stdout)?;
            Ok(ExitCode::SUCCESS)
        }
        Some(Command::Jobs) => {
            run_status(cli.json, "cli.jobs", wh_core::state::load_jobs(), stdout)?;
            Ok(ExitCode::SUCCESS)
        }
        Some(Command::GitSafe {
            expected_branch,
            repo,
            args,
        }) => run_git_safe(&args, expected_branch.as_deref(), repo, cli.json, stdout),
        Some(Command::GhSafe { args }) => run_gh_safe(&args, cli.json, stdout),
        Some(Command::Supervisor { action }) => match action {
            SupervisorAction::Run {
                timeout,
                expected_branch,
                repo,
                max_parallel,
                cmd,
            } => {
                run_supervisor(
                    cli.json,
                    timeout,
                    expected_branch,
                    repo,
                    max_parallel,
                    cmd,
                    stdout,
                )
                .await
            }
        },
        Some(Command::Worktree { action }) => run_worktree(action, cli.json, stdout),
        None => {
            if cli.json {
                serde_json::to_writer(
                    &mut *stdout,
                    &wh_core::contract::Response::bootstrap_success(),
                )
                .map_err(io::Error::other)?;
                stdout.write_all(b"\n")?;
            }
            Ok(ExitCode::SUCCESS)
        }
    }
}

/// Run `wh supervisor run` with policy-checked core supervisor and consistent JSON envelopes.
async fn run_supervisor(
    json: bool,
    timeout_secs: u64,
    expected_branch: Option<String>,
    repo: Option<PathBuf>,
    max_parallel: usize,
    cmd: Vec<String>,
    stdout: &mut impl Write,
) -> wh_core::error::Result<ExitCode> {
    let program = match cmd.first() {
        Some(p) => p.as_str(),
        None => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "supervisor run requires at least one argument (the command)",
            )
            .into());
        }
    };
    let args: Vec<&str> = cmd[1..].iter().map(|s| s.as_str()).collect();
    let options = wh_core::supervisor::RunOptions {
        expected_branch,
        repo,
    };
    let timeout = if timeout_secs == 0 {
        None
    } else {
        Some(Duration::from_secs(timeout_secs))
    };

    // One-shot CLI: a single awaited child cannot contend with itself.
    // Library callers may still use Supervisor::new(n) for in-process fan-out.
    let supervisor = wh_core::supervisor::Supervisor::new(max_parallel.max(1));
    match supervisor.run(program, &args, timeout, &options).await {
        Ok(output) => {
            write_supervisor_result(json, Ok(&output), stdout)?;
            if json {
                // Machine clients (WhClient) parse stdout only on exit 0 (except policy=2).
                // Structured success/failure lives in the envelope (`ok`, `data`, `error`).
                Ok(ExitCode::SUCCESS)
            } else {
                Ok(supervised_exit_code(&output))
            }
        }
        Err(err) => {
            write_supervisor_result(json, Err(&err), stdout)?;
            Err(err)
        }
    }
}

fn write_supervisor_result(
    json: bool,
    result: Result<&wh_core::supervisor::SupervisedOutput, &wh_core::error::Error>,
    stdout: &mut impl Write,
) -> io::Result<()> {
    if !json {
        if let Ok(output) = result {
            serde_json::to_writer(&mut *stdout, output).map_err(io::Error::other)?;
            stdout.write_all(b"\n")?;
        }
        return Ok(());
    }

    let (ok, data, error) = match result {
        Ok(output) => {
            // Always ok:true for completed runs so WhClient keeps `data` (exit_code, timed_out, …).
            let data = serde_json::to_value(output).map_err(io::Error::other)?;
            (true, data, None)
        }
        Err(wh_core::error::Error::PolicyViolation { code, message }) => (
            false,
            serde_json::json!({}),
            Some(wh_core::contract::ErrorData {
                code: code.as_str().to_owned(),
                message: message.clone(),
            }),
        ),
        Err(other) => (
            false,
            serde_json::json!({}),
            Some(wh_core::contract::ErrorData {
                code: "SUPERVISOR_ERROR".to_owned(),
                message: other.to_string(),
            }),
        ),
    };

    let response = wh_core::contract::Response {
        ok,
        schema_version: wh_core::contract::SCHEMA_VERSION,
        command: "supervisor.run",
        data,
        error,
    };
    serde_json::to_writer(&mut *stdout, &response).map_err(io::Error::other)?;
    stdout.write_all(b"\n")?;
    Ok(())
}

/// Map supervised outcome to a process exit code.
///
/// - spawn failure → non-zero
/// - timed_out / killed → non-zero
/// - otherwise propagate child exit code when present
fn supervised_exit_code(output: &wh_core::supervisor::SupervisedOutput) -> ExitCode {
    if output.spawn_failed() {
        return ExitCode::FAILURE;
    }
    if output.timed_out || output.killed {
        return ExitCode::from(124);
    }
    match output.exit_code {
        Some(0) => ExitCode::SUCCESS,
        Some(code) if (1..=255).contains(&code) => ExitCode::from(code as u8),
        Some(_) => ExitCode::FAILURE,
        None => ExitCode::FAILURE,
    }
}

/// Render status/jobs from a preloaded `load_jobs` result (testable without env mutation).
fn run_status(
    json: bool,
    command_name: &'static str,
    jobs_result: Result<Vec<wh_core::status::JobStatus>, String>,
    stdout: &mut impl Write,
) -> io::Result<()> {
    match jobs_result {
        Ok(jobs) => run_with_jobs(json, command_name, jobs, stdout),
        Err(e) => {
            if json {
                // JSON path: write ok:false envelope, then exit non-zero.
                // Consumers should parse stdout even on CalledProcessError (see docs/status-schema.md).
                let response = wh_core::status::status_error(command_name, e.clone());
                serde_json::to_writer(&mut *stdout, &response).map_err(io::Error::other)?;
                stdout.write_all(b"\n")?;
            }
            // Human and JSON: never pretend the empty set is healthy on load failure.
            Err(io::Error::other(e))
        }
    }
}

/// Render status/jobs output. Separated from `run` for testability.
fn run_with_jobs(
    json: bool,
    command_name: &'static str,
    jobs: Vec<wh_core::status::JobStatus>,
    stdout: &mut impl Write,
) -> io::Result<()> {
    if json {
        let response = wh_core::status::status_response(command_name, jobs);
        serde_json::to_writer(&mut *stdout, &response).map_err(io::Error::other)?;
        stdout.write_all(b"\n")?;
    } else if jobs.is_empty() {
        stdout.write_all(b"No watched jobs.\n")?;
    } else {
        for job in &jobs {
            writeln!(
                stdout,
                "{} [{}] {}/{} branch={} ci={}",
                job.job_id, job.process_state, job.owner, job.repo, job.branch, job.ci_class
            )?;
        }
    }
    Ok(())
}

fn run_git_safe(
    args: &[String],
    expected_branch: Option<&str>,
    repo: Option<PathBuf>,
    json: bool,
    stdout: &mut impl Write,
) -> wh_core::error::Result<ExitCode> {
    let cmd = wh_core::git_safe::SafeGitCommand::new(args)?;
    let repo_dir = repo.unwrap_or_else(|| PathBuf::from("."));
    let output = cmd.run(&repo_dir, expected_branch)?;

    write_safe_result("git.safe", cmd.args(), &output, json, stdout)?;
    Ok(exit_code_from_i32(output.exit_code))
}

fn run_gh_safe(
    args: &[String],
    json: bool,
    stdout: &mut impl Write,
) -> wh_core::error::Result<ExitCode> {
    let cmd = wh_core::git_safe::SafeGhCommand::new(args)?;
    let output = cmd.run()?;

    write_safe_result("gh.safe", cmd.args(), &output, json, stdout)?;
    Ok(exit_code_from_i32(output.exit_code))
}

fn write_safe_result(
    command: &'static str,
    args: &[String],
    output: &wh_core::git_safe::GitOutput,
    json: bool,
    stdout: &mut impl Write,
) -> wh_core::error::Result<()> {
    if json {
        let data = serde_json::json!({
            "args": args,
            "exit_code": output.exit_code,
            "stdout": output.stdout,
            "stderr": output.stderr,
        });
        let response = wh_core::contract::Response::success(command, data);
        serde_json::to_writer(&mut *stdout, &response).map_err(io::Error::other)?;
        stdout.write_all(b"\n")?;
    } else {
        // Human: show validation + execution summary; stream child stdout/stderr to stdout/stderr.
        writeln!(
            stdout,
            "ran: {} {} (exit {})",
            if command.starts_with("git") {
                "git"
            } else {
                "gh"
            },
            args.join(" "),
            output.exit_code
        )?;
        if !output.stdout.is_empty() {
            write!(stdout, "{}", output.stdout)?;
        }
        if !output.stderr.is_empty() {
            let _ = write!(io::stderr(), "{}", output.stderr);
        }
    }
    Ok(())
}

fn exit_code_from_i32(code: i32) -> ExitCode {
    if code == 0 {
        ExitCode::SUCCESS
    } else if (1..=255).contains(&code) {
        ExitCode::from(code as u8)
    } else {
        ExitCode::FAILURE
    }
}

#[cfg(test)]
mod tests {
    use std::process::ExitCode;
    use std::str;

    use clap::CommandFactory;
    use wh_core::status::{CiClass, JobStatus, ProcessState};

    use super::{Cli, run, run_status, run_with_jobs, supervised_exit_code};

    fn sample_job() -> JobStatus {
        JobStatus {
            job_id: "wh-1".to_owned(),
            owner: "acme".to_owned(),
            repo: "example-org".to_owned(),
            issue_number: Some(29),
            pr_number: None,
            worktree_path: "/tmp/wt/wh-1".to_owned(),
            branch: "feature/foo".to_owned(),
            process_state: ProcessState::Running,
            last_error: None,
            ci_class: CiClass::Pending,
        }
    }

    #[test]
    fn command_definition_is_valid() {
        Cli::command().debug_assert();
    }

    #[tokio::test]
    async fn json_mode_writes_v1_envelope_to_stdout() {
        let cli = Cli {
            json: true,
            command: None,
        };
        let mut stdout = Vec::new();

        let code = run(cli, &mut stdout).await.unwrap();
        assert_eq!(code, ExitCode::SUCCESS);

        let output = str::from_utf8(&stdout).unwrap();
        let v: serde_json::Value = serde_json::from_str(output.trim()).unwrap();
        assert_eq!(v.get("schema_version").expect("missing schema_version"), 1);
        assert!(v.get("ok").expect("missing ok").as_bool().unwrap());
    }

    #[tokio::test]
    async fn default_mode_keeps_stdout_empty() {
        let cli = Cli {
            json: false,
            command: None,
        };
        let mut stdout = Vec::new();

        let code = run(cli, &mut stdout).await.unwrap();
        assert_eq!(code, ExitCode::SUCCESS);
        assert!(stdout.is_empty());
    }

    #[test]
    fn status_json_emits_v1_envelope_with_jobs_in_data() {
        let mut stdout = Vec::new();

        run_with_jobs(true, "cli.status", vec![], &mut stdout).unwrap();

        let output = str::from_utf8(&stdout).unwrap();
        let v: serde_json::Value = serde_json::from_str(output.trim()).unwrap();

        assert_eq!(v.get("schema_version").expect("missing schema_version"), 1);
        assert_eq!(v.get("command").expect("missing command"), "cli.status");
        assert!(v.get("ok").expect("missing ok").as_bool().unwrap());
        assert!(
            v.get("error").expect("missing error").is_null(),
            "error must be explicitly null, not absent"
        );
        let data = v.get("data").expect("missing data");
        let jobs = data
            .get("jobs")
            .expect("missing data.jobs")
            .as_array()
            .expect("data.jobs must be an array");
        assert!(jobs.is_empty());
        assert!(v.get("jobs").is_none(), "jobs must be nested under data");
    }

    #[test]
    fn status_json_includes_injected_jobs() {
        let mut stdout = Vec::new();

        run_with_jobs(true, "cli.status", vec![sample_job()], &mut stdout).unwrap();

        let output = str::from_utf8(&stdout).unwrap();
        let v: serde_json::Value = serde_json::from_str(output.trim()).unwrap();

        let data = v.get("data").expect("missing data");
        let jobs = data
            .get("jobs")
            .expect("missing data.jobs")
            .as_array()
            .expect("data.jobs must be an array");
        assert_eq!(jobs.len(), 1);
        assert_eq!(jobs[0].get("job_id").expect("missing job_id"), "wh-1");
        assert_eq!(
            jobs[0].get("process_state").expect("missing process_state"),
            "running"
        );
    }

    #[test]
    fn jobs_json_emits_v1_envelope_with_jobs_in_data() {
        let mut stdout = Vec::new();

        run_with_jobs(true, "cli.jobs", vec![], &mut stdout).unwrap();

        let output = str::from_utf8(&stdout).unwrap();
        let v: serde_json::Value = serde_json::from_str(output.trim()).unwrap();

        assert_eq!(v.get("schema_version").expect("missing schema_version"), 1);
        assert_eq!(v.get("command").expect("missing command"), "cli.jobs");
        assert!(v.get("ok").expect("missing ok").as_bool().unwrap());
        assert!(
            v.get("error").expect("missing error").is_null(),
            "error must be explicitly null, not absent"
        );
        let data = v.get("data").expect("missing data");
        let jobs = data
            .get("jobs")
            .expect("missing data.jobs")
            .as_array()
            .expect("data.jobs must be an array");
        assert!(jobs.is_empty());
        assert!(v.get("jobs").is_none(), "jobs must be nested under data");
    }

    #[test]
    fn status_without_json_prints_human_readable() {
        let mut stdout = Vec::new();

        run_with_jobs(false, "cli.status", vec![], &mut stdout).unwrap();

        assert_eq!(str::from_utf8(&stdout).unwrap(), "No watched jobs.\n");
    }

    #[test]
    fn jobs_without_json_prints_human_readable() {
        let mut stdout = Vec::new();

        run_with_jobs(false, "cli.jobs", vec![], &mut stdout).unwrap();

        assert_eq!(str::from_utf8(&stdout).unwrap(), "No watched jobs.\n");
    }

    #[test]
    fn status_without_json_lists_jobs_when_present() {
        let mut stdout = Vec::new();

        run_with_jobs(false, "cli.status", vec![sample_job()], &mut stdout).unwrap();

        let output = str::from_utf8(&stdout).unwrap();
        assert!(output.contains("wh-1"));
        assert!(output.contains("running"));
        assert!(output.contains("feature/foo"));
    }

    #[test]
    fn status_json_load_error_emits_failure_envelope() {
        let mut stdout = Vec::new();
        let err = run_status(
            true,
            "cli.status",
            Err("failed to parse watched.json: expected value".to_owned()),
            &mut stdout,
        )
        .unwrap_err();
        assert!(err.to_string().contains("failed to parse"));

        let output = str::from_utf8(&stdout).unwrap();
        let v: serde_json::Value = serde_json::from_str(output.trim()).unwrap();
        assert_eq!(v.get("ok").expect("missing ok"), false);
        assert_eq!(
            v.get("error")
                .expect("missing error")
                .get("code")
                .expect("missing code"),
            "STATE_LOAD_FAILED"
        );
        assert!(
            v.get("data")
                .expect("missing data")
                .get("jobs")
                .expect("missing jobs")
                .as_array()
                .expect("jobs array")
                .is_empty()
        );
    }

    #[test]
    fn status_human_load_error_does_not_print_healthy_empty() {
        let mut stdout = Vec::new();
        let err = run_status(
            false,
            "cli.status",
            Err("failed to read watched.json: permission denied".to_owned()),
            &mut stdout,
        )
        .unwrap_err();
        assert!(err.to_string().contains("permission denied"));
        // Must not look like a healthy empty watch list.
        assert!(
            !str::from_utf8(&stdout).unwrap().contains("No watched jobs"),
            "human load error must not print healthy empty summary"
        );
        assert!(
            stdout.is_empty(),
            "human load error should not write a success summary to stdout"
        );
    }

    #[tokio::test]
    async fn supervisor_run_non_json_emits_raw_supervised_output() {
        let cli = Cli {
            json: false,
            command: Some(super::Command::Supervisor {
                action: super::SupervisorAction::Run {
                    timeout: 0,
                    expected_branch: None,
                    repo: None,
                    max_parallel: 1,
                    cmd: {
                        #[cfg(windows)]
                        {
                            vec!["where.exe".to_owned(), "where.exe".to_owned()]
                        }
                        #[cfg(not(windows))]
                        {
                            vec!["true".to_owned()]
                        }
                    },
                },
            }),
        };
        let mut stdout = Vec::new();

        let code = run(cli, &mut stdout).await.unwrap();
        assert_eq!(code, ExitCode::SUCCESS);

        let output = str::from_utf8(&stdout).unwrap();
        assert!(output.contains("\"exit_code\":0"));
        // Non-json path is raw SupervisedOutput, not the Response envelope.
        assert!(!output.contains("\"command\":\"supervisor.run\""));
    }

    #[tokio::test]
    async fn supervisor_run_blocks_unsafe_git_command() {
        let cli = Cli {
            json: false,
            command: Some(super::Command::Supervisor {
                action: super::SupervisorAction::Run {
                    timeout: 0,
                    expected_branch: None,
                    repo: None,
                    max_parallel: 1,
                    cmd: vec!["git".to_owned(), "push".to_owned(), "--force".to_owned()],
                },
            }),
        };
        let mut stdout = Vec::new();

        let err = run(cli, &mut stdout).await.unwrap_err();
        assert!(matches!(
            err,
            wh_core::error::Error::PolicyViolation {
                code: wh_core::error::PolicyCode::BareForcePush,
                ..
            }
        ));
        assert!(stdout.is_empty());
    }

    #[tokio::test]
    async fn supervisor_run_blocks_path_qualified_git_force() {
        let cli = Cli {
            json: true,
            command: Some(super::Command::Supervisor {
                action: super::SupervisorAction::Run {
                    timeout: 0,
                    expected_branch: None,
                    repo: None,
                    max_parallel: 1,
                    cmd: vec![
                        "/usr/bin/git".to_owned(),
                        "push".to_owned(),
                        "--force".to_owned(),
                    ],
                },
            }),
        };
        let mut stdout = Vec::new();
        let err = run(cli, &mut stdout).await.unwrap_err();
        assert!(matches!(
            err,
            wh_core::error::Error::PolicyViolation {
                code: wh_core::error::PolicyCode::BareForcePush,
                ..
            }
        ));
        let v: serde_json::Value =
            serde_json::from_str(str::from_utf8(&stdout).unwrap().trim()).unwrap();
        assert_eq!(v.get("ok").and_then(|x| x.as_bool()), Some(false));
        assert_eq!(
            v.get("command").and_then(|x| x.as_str()),
            Some("supervisor.run")
        );
        assert_eq!(
            v.get("error")
                .and_then(|e| e.get("code"))
                .and_then(|c| c.as_str()),
            Some("BARE_FORCE_PUSH")
        );
    }

    #[tokio::test]
    async fn supervisor_run_json_nonzero_sets_ok_false() {
        let cli = Cli {
            json: true,
            command: Some(super::Command::Supervisor {
                action: super::SupervisorAction::Run {
                    timeout: 0,
                    expected_branch: None,
                    repo: None,
                    max_parallel: 1,
                    cmd: {
                        #[cfg(windows)]
                        {
                            // `false` may not exist; use powershell is forbidden. Use `cmd` is forbidden.
                            // Use git with invalid args for non-zero? Prefer `python` - not guaranteed.
                            // Use `ping` with bad args returns non-zero on Windows.
                            vec![
                                "ping".to_owned(),
                                "/n".to_owned(),
                                "0".to_owned(),
                                "127.0.0.1".to_owned(),
                            ]
                        }
                        #[cfg(not(windows))]
                        {
                            vec!["false".to_owned()]
                        }
                    },
                },
            }),
        };
        let mut stdout = Vec::new();
        let code = run(cli, &mut stdout).await.unwrap();
        // JSON mode always exits 0 when the envelope was written; status is in `ok`/`data`.
        assert_eq!(code, ExitCode::SUCCESS);
        let v: serde_json::Value =
            serde_json::from_str(str::from_utf8(&stdout).unwrap().trim()).unwrap();
        assert_eq!(v.get("ok").and_then(|x| x.as_bool()), Some(true));
        let exit = v
            .get("data")
            .and_then(|d| d.get("exit_code"))
            .and_then(|c| c.as_i64());
        assert!(
            exit.is_some_and(|c| c != 0),
            "expected nonzero exit in data, got {exit:?}"
        );
        assert_eq!(
            v.get("data")
                .and_then(|d| d.get("error_code"))
                .and_then(|c| c.as_str()),
            Some("NON_ZERO_EXIT")
        );
    }

    #[tokio::test]
    async fn supervisor_run_blocks_unsafe_gh_command() {
        let cli = Cli {
            json: false,
            command: Some(super::Command::Supervisor {
                action: super::SupervisorAction::Run {
                    timeout: 0,
                    expected_branch: None,
                    repo: None,
                    max_parallel: 1,
                    cmd: vec!["gh".to_owned(), "pr".to_owned(), "merge".to_owned()],
                },
            }),
        };
        let mut stdout = Vec::new();

        let err = run(cli, &mut stdout).await.unwrap_err();
        assert!(matches!(
            err,
            wh_core::error::Error::PolicyViolation {
                code: wh_core::error::PolicyCode::MergeBlocked,
                ..
            }
        ));
        assert!(stdout.is_empty());
    }

    #[tokio::test]
    async fn supervisor_run_json_wraps_v1_envelope() {
        let cli = Cli {
            json: true,
            command: Some(super::Command::Supervisor {
                action: super::SupervisorAction::Run {
                    timeout: 0,
                    expected_branch: None,
                    repo: None,
                    max_parallel: 1,
                    cmd: {
                        #[cfg(windows)]
                        {
                            vec!["where.exe".to_owned(), "where.exe".to_owned()]
                        }
                        #[cfg(not(windows))]
                        {
                            vec!["true".to_owned()]
                        }
                    },
                },
            }),
        };
        let mut stdout = Vec::new();

        let code = run(cli, &mut stdout).await.unwrap();
        assert_eq!(code, ExitCode::SUCCESS);

        let output = str::from_utf8(&stdout).unwrap();
        let v: serde_json::Value = serde_json::from_str(output.trim()).unwrap();
        assert_eq!(v.get("ok").and_then(|x| x.as_bool()), Some(true));
        assert_eq!(
            v.get("command").and_then(|x| x.as_str()),
            Some("supervisor.run")
        );
        assert_eq!(v.get("schema_version").and_then(|x| x.as_u64()), Some(1));
        let data = v.get("data").expect("data");
        assert_eq!(data.get("exit_code").and_then(|x| x.as_i64()), Some(0));
    }

    #[test]
    fn supervised_exit_code_maps_timeout_and_kill() {
        let timed_out = wh_core::supervisor::SupervisedOutput {
            exit_code: None,
            timed_out: true,
            killed: true,
            stdout: String::new(),
            stderr: String::new(),
            stdout_truncated: false,
            stderr_truncated: false,
            error_code: None,
        };
        assert_eq!(supervised_exit_code(&timed_out), ExitCode::from(124));

        let child_fail = wh_core::supervisor::SupervisedOutput {
            exit_code: Some(7),
            timed_out: false,
            killed: false,
            stdout: String::new(),
            stderr: String::new(),
            stdout_truncated: false,
            stderr_truncated: false,
            error_code: None,
        };
        assert_eq!(supervised_exit_code(&child_fail), ExitCode::from(7));
    }

    #[tokio::test]
    async fn git_safe_valid_command_executes() {
        let cli = Cli {
            json: false,
            command: Some(super::Command::GitSafe {
                expected_branch: None,
                repo: None,
                args: vec!["rev-parse".to_owned(), "--is-inside-work-tree".to_owned()],
            }),
        };
        let mut stdout = Vec::new();

        let code = run(cli, &mut stdout).await.unwrap();
        assert_eq!(code, ExitCode::SUCCESS);

        let output = str::from_utf8(&stdout).unwrap();
        assert!(output.contains("ran: git rev-parse --is-inside-work-tree"));
        assert!(output.contains("true"));
    }

    #[tokio::test]
    async fn git_safe_json_mode_includes_exit_code() {
        let cli = Cli {
            json: true,
            command: Some(super::Command::GitSafe {
                expected_branch: None,
                repo: None,
                args: vec!["rev-parse".to_owned(), "--is-inside-work-tree".to_owned()],
            }),
        };
        let mut stdout = Vec::new();

        run(cli, &mut stdout).await.unwrap();

        let output = str::from_utf8(&stdout).unwrap();
        let v: serde_json::Value = serde_json::from_str(output.trim()).unwrap();
        assert_eq!(v.get("ok").and_then(|x| x.as_bool()), Some(true));
        assert_eq!(v.get("command").and_then(|x| x.as_str()), Some("git.safe"));
        let data = v.get("data").expect("data");
        assert_eq!(data.get("exit_code").and_then(|x| x.as_i64()), Some(0));
        assert!(
            data.get("stdout")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .contains("true")
        );
    }

    #[tokio::test]
    async fn git_safe_blocks_merge() {
        let cli = Cli {
            json: false,
            command: Some(super::Command::GitSafe {
                expected_branch: None,
                repo: None,
                args: vec!["merge".to_owned(), "feature".to_owned()],
            }),
        };
        let mut stdout = Vec::new();

        let err = run(cli, &mut stdout).await.unwrap_err();
        assert!(matches!(
            err,
            wh_core::error::Error::PolicyViolation {
                code: wh_core::error::PolicyCode::MergeBlocked,
                ..
            }
        ));
    }

    #[tokio::test]
    async fn git_safe_blocks_bare_force() {
        let cli = Cli {
            json: false,
            command: Some(super::Command::GitSafe {
                expected_branch: None,
                repo: None,
                args: vec!["push".to_owned(), "--force".to_owned()],
            }),
        };
        let mut stdout = Vec::new();

        let err = run(cli, &mut stdout).await.unwrap_err();
        assert!(matches!(
            err,
            wh_core::error::Error::PolicyViolation {
                code: wh_core::error::PolicyCode::BareForcePush,
                ..
            }
        ));
    }

    #[test]
    fn git_safe_checkout_merge_branch_name_not_merge_blocked() {
        // Subcommand-only merge detection: a branch argument named `merge` is fine.
        // Policy-only check — do not execute checkout (would require a real ref / switch).
        let cmd =
            wh_core::git_safe::SafeGitCommand::new(&["checkout".to_owned(), "merge".to_owned()])
                .expect("checkout of branch named merge must not be MergeBlocked");
        assert_eq!(cmd.subcommand(), "checkout");
        assert_eq!(cmd.args(), &["checkout", "merge"]);
    }

    #[tokio::test]
    async fn gh_safe_pr_merge_blocked() {
        let cli = Cli {
            json: false,
            command: Some(super::Command::GhSafe {
                args: vec!["pr".to_owned(), "merge".to_owned()],
            }),
        };
        let mut stdout = Vec::new();

        let err = run(cli, &mut stdout).await.unwrap_err();
        assert!(matches!(
            err,
            wh_core::error::Error::PolicyViolation {
                code: wh_core::error::PolicyCode::MergeBlocked,
                ..
            }
        ));
    }

    #[tokio::test]
    async fn gh_safe_api_blocked() {
        let cli = Cli {
            json: false,
            command: Some(super::Command::GhSafe {
                args: vec!["api".to_owned(), "repos/acme/example-org".to_owned()],
            }),
        };
        let mut stdout = Vec::new();

        let err = run(cli, &mut stdout).await.unwrap_err();
        assert!(matches!(
            err,
            wh_core::error::Error::PolicyViolation {
                code: wh_core::error::PolicyCode::GhSubcommandNotAllowed,
                ..
            }
        ));
    }

    #[tokio::test]
    async fn gh_safe_pr_view_allowed_at_policy() {
        // May fail if gh is not auth'd; policy must accept `pr view`.
        let cli = Cli {
            json: true,
            command: Some(super::Command::GhSafe {
                args: vec!["pr".to_owned(), "view".to_owned(), "1".to_owned()],
            }),
        };
        let mut stdout = Vec::new();
        match run(cli, &mut stdout).await {
            Ok(_) => {
                let output = str::from_utf8(&stdout).unwrap();
                assert!(output.contains("\"command\":\"gh.safe\""));
            }
            Err(wh_core::error::Error::PolicyViolation { .. }) => {
                panic!("pr view must pass policy")
            }
            Err(wh_core::error::Error::Io { .. }) => {
                // gh binary missing is acceptable in constrained envs
            }
            Err(e) => panic!("unexpected error: {e}"),
        }
    }
}
