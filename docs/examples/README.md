# Response envelope examples

Every fixture here is captured verbatim from the `wh` binary and then
pretty-printed. If you change an envelope, re-capture rather than hand-editing —
four of the fixtures that previously lived here were fiction, and nothing caught
it because nothing compared them to real output:

| Fixture | Claimed | Reality |
| --- | --- | --- |
| `bootstrap.json` | command `cli.bootstrap` | no `bootstrap` command exists |
| `error-force-push.json` | command `git.run`, code `PolicyForcePushForbidden` | command is `git-safe`; the code is `BARE_FORCE_PUSH` |
| `error-policy.json` | same as above | same as above |
| `error-merge-forbidden.json` | command `gh.run`, code `PolicyMergeForbidden` | command is `gh-safe`; the code is `MERGE_BLOCKED` |

## Which commands emit an envelope

`worktree` subcommands, `status`, and `jobs` emit the versioned `Response<T>`
envelope under `--json`.

**`git-safe` and `gh-safe` do not.** A policy violation there is plain text on
stderr with exit code 2:

```console
$ wh --json git-safe push --force
wh: policy violation [BARE_FORCE_PUSH]: bare --force/-f is not allowed; use --force-with-lease only

$ wh --json gh-safe pr merge 1
wh: policy violation [MERGE_BLOCKED]: `gh pr merge` is not allowed
```

Do not write a fixture for an envelope those paths never produce. The bracketed
token is the stable `PolicyCode`; parse that rather than the prose.

## Regenerating

```bash
cargo build
wh --json status                     # cli.status
wh --json worktree list              # worktree.list
# error envelope, no repository mutation:
wh --json worktree create --schema-version 2 --repo <repo> <owner> <repo-name> <job> <branch>
```
