# Aggregate discoveries report format

Format specification for the multi-run discoveries report ([GitHub #16](https://github.com/rmems/worktrees-hives/issues/16)).
Implemented by `worktrees_hives.aggregate` (module ownership per the #23 map).

**Scope: format only.** How a batch schedules units is [#83](https://github.com/rmems/worktrees-hives/issues/83); the per-run findings schema is [#82](https://github.com/rmems/worktrees-hives/issues/82) (`worktrees_hives.findings`); the single-run CLI is [#80](https://github.com/rmems/worktrees-hives/issues/80). #83 invokes this renderer (or produces the input it requires).

## Inputs

One `AggregateUnit` per hypothesis run. `collect_unit(hypothesis_id, findings_json, findings_md, *, timed_out=False)` classifies a run's report pair without raising — **silence = fail, not silence = crash**:

| Unit outcome | Meaning | Aggregate failure? |
| --- | --- | --- |
| `reported` | Both files exist and validate against the #82 contract | no |
| `missing_report` | `findings.json` and/or `findings.md` absent | **yes** |
| `invalid_report` | Pair present but fails #82 validation (or `hypothesis_id` mismatch) | **yes** |
| `timeout` | Worker timed out; a valid leftover pair is kept for context but the unit still fails | **yes** |

## Markdown document

Required sections (validated by `validate_aggregate_markdown`, headings inside code fences do not count):

1. `# Aggregate discoveries` — title
2. `## Summary` — unit counts (total / reported / failures with missing–invalid–timeout breakdown) and finding counts across collected reports (discoveries / null results / errors)
3. `## Runs` — one table row per unit:

   | Hypothesis | Unit outcome | Report status | Discoveries | Null results | Errors | Per-run report |
   | --- | --- | --- | ---: | ---: | ---: | --- |

   Every row links to that run's `findings.md` path (angle-bracket destination, so paths with spaces survive). Failure outcomes render bold with an explicit `(failure)` marker; units without a parsed report show `—` for status and counts.
4. `## Failures` — one bullet per failed unit with its recorded detail, or `_None._`
5. `## Policy` — the mandatory never-merge language (`NEVER_MERGE_LINE`). Validation fails if the words "never merge" are absent.
6. `## Attribution` — **only when agent-authored**: pass `attribution="<agent> (<role>)"` to `AggregateReport`; omit for human-authored aggregates.

## JSON document

Versioned independently of the wh CLI envelope and the per-run findings schema. `schema_version` is currently `1`.

```json
{
  "schema_version": 1,
  "counts": {
    "units": 3, "reported": 1, "missing_report": 1, "invalid_report": 0,
    "timeout": 1, "failures": 2, "discoveries": 2, "null_results": 2, "errors": 2
  },
  "units": [
    {
      "hypothesis_id": "H-001",
      "findings_json": "/path/to/H-001/findings.json",
      "findings_md": "/path/to/H-001/findings.md",
      "outcome": "reported",
      "report": { "schema_version": 1, "hypothesis_id": "H-001", "...": "per #82" }
    },
    {
      "hypothesis_id": "H-002",
      "findings_json": "/path/to/H-002/findings.json",
      "findings_md": "/path/to/H-002/findings.md",
      "outcome": "missing_report",
      "detail": "missing report file(s): /path/to/H-002/findings.json"
    }
  ],
  "attribution": "Fable 5 (agent)"
}
```

`counts` is derived from `units` and embedded for consumers; parsing **re-derives it and never trusts the embedded copy**. `report` is required for `reported` units, forbidden for `missing_report`/`invalid_report`, and optional for `timeout`. Parsing fails closed (`AggregateValidationError`) on any structural violation, unknown `outcome`, unsupported `schema_version`, or non-finite numbers.

`write_aggregate_pair` prepares and validates both payloads before writing either file, so a failure never leaves a JSON-only half pair on disk.

## Policy

This aggregate report is informational only. **Never merge** and never auto-merge based on it; pull requests always require human review.
