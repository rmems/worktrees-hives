//! Borrowed identities for isolated worktree creation.
//!
//! These newtypes keep owner, repo, job, branch, start-point, and commit values
//! distinct at the Rust boundary. JSON and CLI contracts still exchange plain
//! strings.

use std::path::Path;

use crate::error::{Error, Result};

macro_rules! borrowed_identity {
    ($name:ident) => {
        #[derive(Debug, Clone, Copy, PartialEq, Eq)]
        pub struct $name<'a>(pub &'a str);

        impl<'a> $name<'a> {
            #[must_use]
            pub const fn as_str(self) -> &'a str {
                self.0
            }
        }
    };
}

borrowed_identity!(Owner);
borrowed_identity!(Repo);
borrowed_identity!(JobId);
borrowed_identity!(BranchName);
borrowed_identity!(StartPoint);
borrowed_identity!(CommitId);
borrowed_identity!(BranchRef);
borrowed_identity!(HexOidPrefix);

/// Resolve a caller-supplied commit-ish to one exact commit object.
pub(crate) fn resolve_start_commit(
    repo_root: &Path,
    start_point: StartPoint<'_>,
) -> Result<String> {
    reject_empty_start_point(start_point)?;
    enforce_leading_hex_oid(start_point, None)?;
    let commit = peel_to_commit(repo_root, start_point)?;
    reject_empty_resolved_commit(start_point, CommitId(&commit))?;
    enforce_leading_hex_oid(start_point, Some(CommitId(&commit)))?;
    Ok(commit)
}

fn reject_empty_start_point(start_point: StartPoint<'_>) -> Result<()> {
    if start_point.as_str().is_empty() {
        return Err(rev_parse_error(GitErrorText(
            "start point must not be empty".to_owned(),
        )));
    }
    Ok(())
}

fn reject_empty_resolved_commit(start_point: StartPoint<'_>, commit: CommitId<'_>) -> Result<()> {
    if commit.as_str().is_empty() {
        return Err(rev_parse_error(GitErrorText(format!(
            "start point {:?} resolved to an empty commit id",
            start_point.as_str()
        ))));
    }
    Ok(())
}

fn enforce_leading_hex_oid(
    start_point: StartPoint<'_>,
    resolved: Option<CommitId<'_>>,
) -> Result<()> {
    let Some(hex_prefix) = leading_hex_oid_prefix(start_point) else {
        return Ok(());
    };
    reject_non_full_hex_oid(hex_prefix)?;
    match resolved {
        Some(commit) => reject_hex_oid_mismatch(start_point, hex_prefix, commit),
        None => Ok(()),
    }
}

/// Leading all-hex object-id text when it is the entire start point or is
/// immediately followed by a commit-ish decoration (`~`, `^`, `@{`).
///
/// This closes abbreviated-OID smuggling such as `<abbrev>~0` while leaving
/// symbolic refs (`refs/heads/x`, `develop~1`, tags) untouched.
fn leading_hex_oid_prefix(start_point: StartPoint<'_>) -> Option<HexOidPrefix<'_>> {
    let text = start_point.as_str();
    let hex_len = text.bytes().take_while(u8::is_ascii_hexdigit).count();
    if hex_len == 0 {
        return None;
    }
    if !hex_oid_rest_is_boundary(SelectorSuffix(&text[hex_len..])) {
        return None;
    }
    Some(HexOidPrefix(&text[..hex_len]))
}

#[derive(Clone, Copy)]
struct SelectorSuffix<'a>(&'a str);

/// True when a leading hex run is a complete selector: bare, or immediately
/// followed by a commit-ish decoration. Guard clauses keep the match set
/// explicit without a compound boolean.
fn hex_oid_rest_is_boundary(suffix: SelectorSuffix<'_>) -> bool {
    let rest = suffix.0;
    if rest.is_empty() {
        return true;
    }
    if rest.starts_with('~') {
        return true;
    }
    if rest.starts_with('^') {
        return true;
    }
    rest.starts_with("@{")
}

fn reject_non_full_hex_oid(hex_prefix: HexOidPrefix<'_>) -> Result<()> {
    let len = hex_prefix.as_str().len();
    if len == 40 {
        return Ok(());
    }
    if len == 64 {
        return Ok(());
    }
    Err(rev_parse_error(GitErrorText(
        "all-hex start point must be a full 40- or 64-character object id".to_owned(),
    )))
}

fn reject_hex_oid_mismatch(
    start_point: StartPoint<'_>,
    hex_prefix: HexOidPrefix<'_>,
    commit: CommitId<'_>,
) -> Result<()> {
    if hex_oid_matches_commit(hex_prefix, commit) {
        return Ok(());
    }
    Err(rev_parse_error(GitErrorText(format!(
        "all-hex start point must equal the full canonical object id; requested \
         {:?}, resolved {:?}",
        start_point.as_str(),
        commit.as_str()
    ))))
}

fn hex_oid_matches_commit(hex_prefix: HexOidPrefix<'_>, commit: CommitId<'_>) -> bool {
    let prefix = hex_prefix.as_str();
    let commit = commit.as_str();
    if prefix.len() != commit.len() {
        return false;
    }
    commit.eq_ignore_ascii_case(prefix)
}

fn peel_to_commit(repo_root: &Path, start_point: StartPoint<'_>) -> Result<String> {
    let commitish = format!("{}^{{commit}}", start_point.as_str());
    let output = std::process::Command::new("git")
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

fn rev_parse_error(stderr: GitErrorText) -> Error {
    Error::GitCommand {
        args: vec!["rev-parse".into(), "--verify".into()],
        stderr: stderr.0,
    }
}

struct GitErrorText(String);

#[cfg(test)]
mod tests {
    use super::*;

    /// 40 hex characters: a full SHA-1 object id.
    const SHA1: &str = "0123456789abcdef0123456789abcdef01234567";
    /// 64 hex characters: a full SHA-256 object id.
    const SHA256: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    // --- leading_hex_oid_prefix: which selectors are treated as object ids ---

    #[test]
    fn bare_hex_run_is_an_oid_selector() {
        assert_eq!(
            leading_hex_oid_prefix(StartPoint(SHA1)).map(HexOidPrefix::as_str),
            Some(SHA1)
        );
    }

    #[test]
    fn abbreviated_oid_with_decoration_is_still_an_oid_selector() {
        // The smuggling vector this defense exists for: `<abbrev>~0` resolves to
        // the same commit as `<abbrev>`, so treating it as symbolic would let a
        // short id through the full-length check.
        for selector in [
            "deadbeef~0",
            "deadbeef^2",
            "deadbeef^{commit}",
            "deadbeef@{0}",
        ] {
            assert_eq!(
                leading_hex_oid_prefix(StartPoint(selector)).map(HexOidPrefix::as_str),
                Some("deadbeef"),
                "{selector} must be recognised as an abbreviated object id"
            );
        }
    }

    #[test]
    fn symbolic_refs_are_not_oid_selectors() {
        // `develop~1` begins with the hex run "de", but "velop~1" is not a
        // decoration boundary, so the whole thing stays symbolic.
        for selector in [
            "refs/heads/main",
            "main",
            "develop~1",
            "deadbeef-branch",
            "HEAD",
            "v1.2.3",
        ] {
            assert!(
                leading_hex_oid_prefix(StartPoint(selector)).is_none(),
                "{selector} must not be treated as an object id"
            );
        }
    }

    #[test]
    fn an_all_hex_branch_name_is_treated_as_an_object_id() {
        // Documents a deliberate false positive: a branch literally named
        // "beef" is indistinguishable from an abbreviated id here, so it is
        // rejected by the full-length rule rather than silently resolved.
        assert_eq!(
            leading_hex_oid_prefix(StartPoint("beef")).map(HexOidPrefix::as_str),
            Some("beef")
        );
        assert!(enforce_leading_hex_oid(StartPoint("beef"), None).is_err());
    }

    // --- hex_oid_rest_is_boundary ---

    #[test]
    fn boundary_accepts_only_empty_or_commit_ish_decoration() {
        for rest in ["", "~0", "~", "^2", "^{commit}", "@{0}", "@{upstream}"] {
            assert!(
                hex_oid_rest_is_boundary(SelectorSuffix(rest)),
                "{rest:?} should be a boundary"
            );
        }
        for rest in ["-branch", "abc", "@x", "@", "/main", ".x"] {
            assert!(
                !hex_oid_rest_is_boundary(SelectorSuffix(rest)),
                "{rest:?} should not be a boundary"
            );
        }
    }

    // --- reject_non_full_hex_oid: only 40 or 64 characters pass ---

    #[test]
    fn full_length_object_ids_are_accepted() {
        assert!(reject_non_full_hex_oid(HexOidPrefix(SHA1)).is_ok());
        assert!(reject_non_full_hex_oid(HexOidPrefix(SHA256)).is_ok());
    }

    #[test]
    fn off_by_one_and_abbreviated_lengths_are_rejected() {
        let sha1_short = &SHA1[..39];
        let sha1_long = format!("{SHA1}0");
        let sha256_short = &SHA256[..63];
        let sha256_long = format!("{SHA256}0");
        for candidate in [
            "deadbeef",
            sha1_short,
            sha1_long.as_str(),
            sha256_short,
            sha256_long.as_str(),
        ] {
            assert!(
                reject_non_full_hex_oid(HexOidPrefix(candidate)).is_err(),
                "length {} must be rejected",
                candidate.len()
            );
        }
    }

    // --- hex_oid_matches_commit: case-insensitive, length-exact ---

    #[test]
    fn object_id_comparison_ignores_case() {
        let upper = SHA1.to_ascii_uppercase();
        assert!(hex_oid_matches_commit(
            HexOidPrefix(upper.as_str()),
            CommitId(SHA1)
        ));
    }

    #[test]
    fn object_id_comparison_requires_equal_length_and_value() {
        assert!(!hex_oid_matches_commit(
            HexOidPrefix(&SHA1[..39]),
            CommitId(SHA1)
        ));
        let other = format!("f{}", &SHA1[1..]);
        assert!(!hex_oid_matches_commit(
            HexOidPrefix(SHA1),
            CommitId(other.as_str())
        ));
    }

    // --- enforce_leading_hex_oid: the composed rule, without touching git ---

    #[test]
    fn full_object_id_matching_the_resolved_commit_is_accepted() {
        assert!(enforce_leading_hex_oid(StartPoint(SHA1), Some(CommitId(SHA1))).is_ok());
    }

    #[test]
    fn full_object_id_disagreeing_with_the_resolved_commit_is_rejected() {
        let resolved = format!("f{}", &SHA1[1..]);
        let err = enforce_leading_hex_oid(StartPoint(SHA1), Some(CommitId(resolved.as_str())))
            .expect_err("a resolved commit that differs from the request must fail");
        match err {
            Error::GitCommand { stderr, .. } => assert!(
                stderr.contains("must equal the full canonical object id"),
                "unexpected stderr: {stderr}"
            ),
            other => panic!("expected GitCommand error, got {other:?}"),
        }
    }

    #[test]
    fn abbreviated_object_id_is_rejected_before_resolution() {
        // `None` models the pre-resolution call in `resolve_start_commit`: the
        // short id must be refused before git is ever consulted.
        let err = enforce_leading_hex_oid(StartPoint("deadbeef~0"), None)
            .expect_err("abbreviated object id must be refused");
        match err {
            Error::GitCommand { stderr, .. } => assert!(
                stderr.contains("full 40- or 64-character object id"),
                "unexpected stderr: {stderr}"
            ),
            other => panic!("expected GitCommand error, got {other:?}"),
        }
    }

    #[test]
    fn symbolic_start_points_bypass_the_object_id_rules_entirely() {
        assert!(enforce_leading_hex_oid(StartPoint("refs/heads/main"), None).is_ok());
        assert!(
            enforce_leading_hex_oid(StartPoint("develop~1"), Some(CommitId(SHA1))).is_ok(),
            "a symbolic selector must not be compared against the resolved id"
        );
    }

    // --- empty-input guards ---

    #[test]
    fn empty_start_point_is_rejected() {
        assert!(reject_empty_start_point(StartPoint("")).is_err());
        assert!(reject_empty_start_point(StartPoint("HEAD")).is_ok());
    }

    #[test]
    fn empty_resolved_commit_is_rejected() {
        assert!(reject_empty_resolved_commit(StartPoint("HEAD"), CommitId("")).is_err());
        assert!(reject_empty_resolved_commit(StartPoint("HEAD"), CommitId(SHA1)).is_ok());
    }
}
